"""WhatsApp(Baileys) 协议 worker 已读回执 + 打字/录音状态契约（下一阶段拟人链扩展）。

锁定：
  - mark_read → POST /accounts/{id}/read；ok&marked 才 True
  - send_chat_action typing→composing / record_audio→recording
  - session 不健康 → 快速跳过（不打死会话），返回 False 且不发 HTTP
"""
from __future__ import annotations

import asyncio

import pytest

from src.integrations.account_orchestrator import WhatsAppProtocolWorker


def _worker(monkeypatch, *, unhealthy=False, base="http://svc"):
    w = WhatsAppProtocolWorker({"account_id": "100"}, {})
    monkeypatch.setattr(w, "_base", lambda: base)
    monkeypatch.setattr(w, "_session_unhealthy", lambda: unhealthy)
    return w


# ── mark_read ────────────────────────────────────────────

def test_mark_read_true_when_marked(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wa
    calls = []

    async def _fake(url, payload, timeout=20.0):
        calls.append((url, payload))
        return {"ok": True, "marked": True}

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch)
    assert asyncio.run(w.mark_read("639111")) is True
    assert calls[0][0] == "http://svc/accounts/100/read"
    assert calls[0][1] == {"jid": "639111"}


def test_mark_read_false_when_nothing_to_mark(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wa

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "marked": False}   # 无未读可标

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch)
    assert asyncio.run(w.mark_read("639111")) is False


def test_mark_read_skips_when_session_unhealthy(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wa
    called = []

    async def _fake(url, payload, timeout=20.0):
        called.append(url)
        return {"ok": True, "marked": True}

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch, unhealthy=True)
    assert asyncio.run(w.mark_read("639111")) is False
    assert called == []          # 不健康 → 根本不打 HTTP


# ── send_chat_action ─────────────────────────────────────

def test_typing_maps_to_composing(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wa
    calls = []

    async def _fake(url, payload, timeout=20.0):
        calls.append(payload)
        return {"ok": True, "state": payload.get("state")}

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch)
    assert asyncio.run(w.send_chat_action("639111", "typing")) is True
    assert calls[0] == {"jid": "639111", "state": "composing"}


def test_record_audio_maps_to_recording(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wa
    calls = []

    async def _fake(url, payload, timeout=20.0):
        calls.append(payload)
        return {"ok": True}

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch)
    assert asyncio.run(w.send_chat_action("639111", "record_audio")) is True
    assert calls[0]["state"] == "recording"


def test_typing_skips_when_session_unhealthy(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wa
    called = []

    async def _fake(url, payload, timeout=20.0):
        called.append(url)
        return {"ok": True}

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch, unhealthy=True)
    assert asyncio.run(w.send_chat_action("639111")) is False
    assert called == []


# ── send_media：media_type 归一化必传（工单 #143，2026-09-02）─────────────
# 相册链的 "photo" 曾裸传边车 → 不在 image/voice/video/sticker 白名单 →
# 落 document 分支，对方端图片显示成点不开的「文档」。

def _send_media_payload(monkeypatch, media_type, path="D:/x/pic.jpg"):
    import src.integrations.whatsapp_baileys_login as wa
    calls = []

    async def _fake(url, payload, timeout=20.0):
        calls.append((url, payload))
        return {"ok": True, "message_id": "MID1"}

    monkeypatch.setattr(wa, "_post_json", _fake)
    w = _worker(monkeypatch)
    res = asyncio.run(w.send_media(
        "639111", media_path=path, media_type=media_type, caption="hi"))
    assert res["delivered"] is True
    assert len(calls) == 1
    assert calls[0][0] == "http://svc/accounts/100/send-media"
    return calls[0][1]


def test_send_media_normalizes_photo_to_image(monkeypatch):
    payload = _send_media_payload(monkeypatch, "photo")
    assert payload["media_type"] == "image"


def test_send_media_missing_type_falls_back_to_ext(monkeypatch):
    assert _send_media_payload(monkeypatch, "")["media_type"] == "image"
    assert _send_media_payload(
        monkeypatch, "", path="D:/x/v.mp4")["media_type"] == "video"
    assert _send_media_payload(
        monkeypatch, "", path="D:/x/doc.pdf")["media_type"] == "document"


def test_send_media_whitelist_types_pass_through(monkeypatch):
    # sticker/image/video 原值直达边车（WA 有原生贴纸分支，绝不折叠成 image）
    for mt in ("image", "video", "sticker", "document"):
        assert _send_media_payload(monkeypatch, mt)["media_type"] == mt
