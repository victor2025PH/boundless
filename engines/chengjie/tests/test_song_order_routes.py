# -*- coding: utf-8 -*-
"""专属歌订单路由契约（实施58 P2）：列表/试听/人审放行=就地投递/打回。

不变量：
- approve 只对 review 态生效（并发双窗口一方 409）；
- 投递复用编排器 send_media 语音口 + 收件箱镜像 + 唱歌账本记账；
- 发送失败订单**留在 review**（可重试），绝不静默标 delivered。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.companion.song_orders as song_orders  # noqa: E402
from src.companion.song_orders import SongOrderStore  # noqa: E402
from src.web.routes.song_order_routes import (  # noqa: E402
    register_song_order_routes,
)


class _FakeOrch:
    def __init__(self, delivered=True):
        self.delivered = delivered
        self.calls = []

    def owns_media(self, platform, account_id):
        return account_id != "unmanaged"

    async def send_media(self, platform, account_id, chat_key, **kw):
        self.calls.append((platform, account_id, chat_key, kw))
        return {"delivered": self.delivered}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = SongOrderStore(path=tmp_path / "orders.db")
    monkeypatch.setattr(song_orders, "_STORE", store)
    orch = _FakeOrch()
    import src.integrations.account_orchestrator as ao
    monkeypatch.setattr(ao, "get_orchestrator", lambda cfg: orch)
    import src.integrations.protocol_bridge as pb

    def _fake_save(platform, account_id, name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return str(p), f"/static/outbound/{name}", {}

    monkeypatch.setattr(pb, "save_outbound_media", _fake_save)

    app = FastAPI()

    def _auth(request: Request):
        return True

    tdir = tmp_path / "templates"
    tdir.mkdir(exist_ok=True)
    import json as _json
    (tdir / "manifest.json").write_text(_json.dumps({"templates": [
        {"id": "origin_moon", "title": "月光", "file": "m.wav",
         "lyrics": "月亮爬上了窗台", "enabled": True}]}, ensure_ascii=False),
        encoding="utf-8")

    class _Mgr:
        config = {"companion": {"singing": {"templates_dir": str(tdir)}}}

    register_song_order_routes(app, api_auth=_auth, config_manager=_Mgr())
    client = TestClient(app)
    return client, store, orch, tmp_path


def _mk_review(store, tmp_path, oid_kw=None):
    oid = store.create_order(
        platform="telegram", account_id="a1", chat_key="c1",
        persona_id="chen_meiling", voice_key="warm_f", peer_name="阿泽",
        request_text="写首歌", facts=["去了海边"], **(oid_kw or {}))
    store.claim_next_pending()
    audio = tmp_path / f"order_{oid}.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 50000)
    store.set_review(oid, lyrics="月亮爬上小窗台\n想起你说的海边",
                     take={"seed": 7, "sim": 0.88, "dur": 19.0},
                     audio_path=str(audio))
    return oid


def test_list_and_counts(env):
    client, store, _, tmp = env
    oid = _mk_review(store, tmp)
    d = client.get("/api/singing/orders").json()
    assert d["ok"] and d["counts"].get("review") == 1
    row = d["orders"][0]
    assert row["id"] == oid and row["has_audio"] and row["take"]["seed"] == 7
    d2 = client.get("/api/singing/orders", params={"status": "pending"}).json()
    assert d2["orders"] == []


def test_audio_stream_and_missing(env):
    client, store, _, tmp = env
    oid = _mk_review(store, tmp)
    r = client.get(f"/api/singing/orders/{oid}/audio")
    assert r.status_code == 200 and r.content[:4] == b"OggS"
    assert client.get("/api/singing/orders/999/audio").status_code == 404


def test_approve_delivers_and_records(env):
    client, store, orch, tmp = env
    oid = _mk_review(store, tmp)
    r = client.post(f"/api/singing/orders/{oid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "delivered"
    assert store.get(oid)["status"] == "delivered"
    # 编排器语音口被调用 + 镜像文本带《专属》与首句
    platform, acct, chat, kw = orch.calls[0]
    assert (platform, acct, chat) == ("telegram", "a1", "c1")
    assert kw["media_type"] == "voice"
    assert "专属" in kw["inbox_text"] and "月亮爬上小窗台" in kw["inbox_text"]
    # 二次 approve（并发窗口语义）→ 409
    assert client.post(f"/api/singing/orders/{oid}/approve").status_code == 409


def test_approve_send_failure_keeps_review(env):
    client, store, orch, tmp = env
    orch.delivered = False
    oid = _mk_review(store, tmp)
    r = client.post(f"/api/singing/orders/{oid}/approve")
    assert r.status_code == 502
    assert store.get(oid)["status"] == "review"      # 可重试，绝不假 delivered


def test_approve_unmanaged_account_409(env):
    client, store, orch, tmp = env
    oid = store.create_order(
        platform="telegram", account_id="unmanaged", chat_key="c9",
        persona_id="p", peer_name="", request_text="写歌", facts=[])
    store.claim_next_pending()
    audio = tmp / "u.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 50000)
    store.set_review(oid, lyrics="x", take={}, audio_path=str(audio))
    assert client.post(f"/api/singing/orders/{oid}/approve").status_code == 409
    assert store.get(oid)["status"] == "review"


def test_reject_flow(env):
    client, store, _, tmp = env
    oid = _mk_review(store, tmp)
    r = client.post(f"/api/singing/orders/{oid}/reject",
                    json={"reason": "不够像"})
    assert r.status_code == 200
    row = store.get(oid)
    assert row["status"] == "rejected" and row["fail_reason"] == "不够像"
    assert client.post(
        f"/api/singing/orders/{oid}/reject", json={}).status_code == 409


def test_approve_carries_framing_caption(env):
    """止损话术：送达语音带「唱歌嗓≠说话嗓」柔性铺垫配文（P3-1）。"""
    client, store, orch, tmp = env
    oid = _mk_review(store, tmp)
    assert client.post(f"/api/singing/orders/{oid}/approve").status_code == 200
    from src.companion.song_stock import _FRAMING_LINES
    assert orch.calls[0][3]["caption"] in _FRAMING_LINES


def test_fail_human_mapping_in_list(env):
    client, store, _, tmp = env
    oid = store.create_order(
        platform="telegram", account_id="a1", chat_key="cf",
        persona_id="p", peer_name="", request_text="写歌", facts=[])
    store.claim_next_pending()
    store.set_failed(oid, "lyrics_rejected", take={"attempts": [{"n": 1}]})
    rows = client.get("/api/singing/orders").json()["orders"]
    row = next(r for r in rows if r["id"] == oid)
    assert row["fail_reason"] == "lyrics_rejected"
    assert "attempts" not in row["fail_human"]      # 人话，不是 JSON
    assert row["fail_human"] and row["fail_human"] != "lyrics_rejected"


def test_retry_endpoint(env):
    client, store, _, tmp = env
    oid = store.create_order(
        platform="telegram", account_id="a1", chat_key="cr",
        persona_id="p", peer_name="", request_text="写歌", facts=[])
    store.claim_next_pending()
    store.set_failed(oid, "render_failed")
    assert client.post(f"/api/singing/orders/{oid}/retry").status_code == 200
    assert store.get(oid)["status"] == "pending"
    # 非 failed 态重试 → 409
    assert client.post(f"/api/singing/orders/{oid}/retry").status_code == 409


def test_delete_endpoint_final_only_and_cleans_audio(env):
    client, store, _, tmp = env
    oid = _mk_review(store, tmp)
    # review=活单拒删
    assert client.post(f"/api/singing/orders/{oid}/delete").status_code == 409
    store.resolve_review(oid, to_status="rejected")
    audio = Path(store.get(oid)["audio_path"])
    assert audio.is_file()
    assert client.post(f"/api/singing/orders/{oid}/delete").status_code == 200
    assert store.get(oid) is None and not audio.exists()


def test_supply_request_and_status(env):
    client, store, _, tmp = env
    # 未知模板 404 / 坏 id 400
    assert client.post("/api/singing/supply-request",
                       json={"template_id": "nope"}).status_code == 404
    assert client.post("/api/singing/supply-request",
                       json={"template_id": "../x"}).status_code == 400
    r = client.post("/api/singing/supply-request",
                    json={"template_id": "origin_moon"})
    assert r.status_code == 200 and r.json()["queued"] == 1
    # 幂等：重复申请不加行
    r2 = client.post("/api/singing/supply-request",
                     json={"template_id": "origin_moon"})
    assert r2.json()["queued"] == 1
    st = client.get("/api/singing/supply-status").json()
    assert st["ok"] and st["nightly_at"] == "05:10"
    assert st["pending_requests"][0]["template_id"] == "origin_moon"
