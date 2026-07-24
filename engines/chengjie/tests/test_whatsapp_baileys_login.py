"""M3：WhatsApp Baileys 协议登录 Python 桥接 单测（不联网）。"""

from __future__ import annotations

import asyncio
import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations import whatsapp_baileys_login as wab
from src.integrations.account_registry import AccountRegistry


def test_service_base_url_default_and_override():
    assert wab.service_base_url({}) == "http://127.0.0.1:8790"
    cfg = {"platform_login": {"whatsapp": {"baileys_url": "http://h:9/"}}}
    assert wab.service_base_url(cfg) == "http://h:9"


def test_protocol_enabled_flag():
    assert wab.protocol_enabled({}) is False
    assert wab.protocol_enabled(
        {"platform_login": {"whatsapp": {"protocol_enabled": True}}}) is True


def test_normalize_status():
    assert wab._normalize_status("open") == "authorized"
    assert wab._normalize_status("connected") == "authorized"
    assert wab._normalize_status("scanned") == "scanned"
    assert wab._normalize_status("timeout") == "expired"
    assert wab._normalize_status("logged_out") == "failed"
    assert wab._normalize_status("whatever") == "pending"


def test_maybe_register_gating():
    wab._registered = False
    pl._PROVIDERS.pop(pl._pkey("whatsapp", "protocol"), None)
    assert wab.maybe_register({}) is False
    assert pl.mode_available("whatsapp", "protocol") is False
    try:
        assert wab.maybe_register(
            {"platform_login": {"whatsapp": {"protocol_enabled": True}}}) is True
        assert pl.mode_available("whatsapp", "protocol") is True
    finally:
        wab._registered = False
        pl._PROVIDERS.pop(pl._pkey("whatsapp", "protocol"), None)


def test_provider_flow_authorized(monkeypatch):
    # 伪造 Node 微服务的 HTTP 响应
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "wa_abc", "qr_image": "data:image/png;base64,xxx"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pending"}
        return {"status": "open", "account_id": "8613800000000"}

    monkeypatch.setattr(wab, "_post_json", fake_post)
    monkeypatch.setattr(wab, "_get_json", fake_get)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "wa.db"))
    monkeypatch.setattr(wab, "get_account_registry", lambda: reg)

    async def run():
        provider = wab.make_provider(
            {"platform_login": {"whatsapp": {"baileys_url": "http://x"}}})
        info = await provider(None, "whatsapp", "protocol", "")
        assert info["qr_image"].startswith("data:image/png")
        poll = info["poll"]
        r1 = await poll(None)
        assert r1["status"] == "pending"
        r2 = await poll(None)
        assert r2["status"] == "authorized"
        assert r2["account_id"] == "8613800000000"

    asyncio.run(run())
    # 登录成功应已写入注册表
    g = reg.get("whatsapp", "8613800000000")
    assert g and g["mode"] == "protocol" and g["status"] == "online"


def test_provider_start_service_down(monkeypatch):
    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(wab, "_post_json", boom)

    async def run():
        provider = wab.make_provider({})
        info = await provider(None, "whatsapp", "protocol", "")
        assert "instruction" in info
        assert "poll" not in info  # 服务不可达 → 仅返回提示，不进入轮询

    asyncio.run(run())


def test_provider_poll_forwards_self_profile(monkeypatch):
    """P1 身份化：Node status 回传 pushname/avatar_url → poll 转发给 enrich_from_fields。

    （Node 侧已实装这两个字段；此处钉死 Python 桥接的转发契约，防再度断链。）
    """
    import src.integrations.account_self_profile as sp

    async def fake_post(url, payload, timeout=20.0):
        return {"login_id": "wa_abc", "qr_image": "data:image/png;base64,xxx"}

    async def fake_get(url, timeout=20.0):
        return {"status": "open", "account_id": "8613800000000",
                "pushname": "小雨", "avatar_url": "https://pps.whatsapp.net/p.jpg"}

    enriched = []

    async def fake_enrich(platform, account_id, **kw):
        enriched.append((platform, account_id, kw.get("name"), kw.get("avatar_url")))
        return {"self_name": kw.get("name", "")}

    monkeypatch.setattr(wab, "_post_json", fake_post)
    monkeypatch.setattr(wab, "_get_json", fake_get)
    monkeypatch.setattr(sp, "enrich_from_fields", fake_enrich)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "wa.db"))
    monkeypatch.setattr(wab, "get_account_registry", lambda: reg)

    async def run():
        provider = wab.make_provider(
            {"platform_login": {"whatsapp": {"baileys_url": "http://x"}}})
        info = await provider(None, "whatsapp", "protocol", "")
        r = await info["poll"](None)
        assert r["status"] == "authorized"

    asyncio.run(run())
    assert enriched == [
        ("whatsapp", "8613800000000", "小雨", "https://pps.whatsapp.net/p.jpg")]


# ── 占位会话拉历史（消除 no_anchor 死角）────────────────────────────────────
# 背景：Node 同步会话列表只建"零消息"占位会话；用户点开想拉历史时 store 里没有
# 带 platform_msg_id 的锚点，旧逻辑直接返回 no_anchor → 会话永远是空的。
# 新契约：无锚点时后端改发「空 oldest_id」请求给 Node（Node 用缓存的 lastMsgKey
# 兜底）；Node 明确回 no_cached_anchor 才对前端返回 no_history_available。


def _history_client(monkeypatch, tmp_path, fake_post):
    """挂好 account routes + 独立 InboxStore 的 TestClient（WhatsApp 协议已启用）。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.inbox.store import InboxStore
    from src.web.routes.unified_inbox_account_routes import register_account_routes

    monkeypatch.setattr(wab, "_post_json", fake_post)
    app = FastAPI()
    register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={
            "platform_login": {"whatsapp": {
                "protocol_enabled": True, "baileys_url": "http://node:1"}}}))
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app), app.state.inbox_store


def test_history_placeholder_falls_back_to_empty_oldest_id(monkeypatch, tmp_path):
    """占位会话无 store 锚点 → 不再 no_anchor 失败，改发空 oldest_id 请求给 Node。"""
    calls = []

    async def fake_post(url, payload, timeout=20.0):
        calls.append((url, payload))
        return {"ok": True, "request_id": "req_1"}

    c, store = _history_client(monkeypatch, tmp_path, fake_post)
    # 模拟 Node 会话列表同步建的占位会话：有会话行、零消息
    store.upsert_protocol_chats("whatsapp", "639555000111", [
        {"jid": "8613800138000", "name": "占位客户", "ts": 100, "unread": 3}])
    r = c.post("/api/platforms/whatsapp/639555000111/history",
               json={"chat_key": "8613800138000", "count": 25})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "requested": 25}
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://node:1/accounts/639555000111/history"
    # 关键契约：oldest_id 为空 → Node 侧回落用缓存的 lastMsgKey 当锚点
    assert payload["oldest_id"] == ""
    assert payload["jid"] == "8613800138000@s.whatsapp.net"
    assert payload["count"] == 25


def test_history_placeholder_no_cached_anchor_maps_to_no_history_available(
        monkeypatch, tmp_path):
    """Node 连缓存锚点也没有（刚重启/从未见过消息）→ 前端得到明确的 no_history_available。"""
    async def fake_post(url, payload, timeout=20.0):
        assert payload["oldest_id"] == ""  # 无锚点路径必须发空 oldest_id
        return {"ok": False, "error": "no_cached_anchor"}

    c, store = _history_client(monkeypatch, tmp_path, fake_post)
    store.upsert_protocol_chats("whatsapp", "639555000111", [
        {"jid": "8613800138000", "name": "占位客户", "ts": 100, "unread": 0}])
    r = c.post("/api/platforms/whatsapp/639555000111/history",
               json={"chat_key": "8613800138000"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "no_history_available"}


def test_history_with_store_anchor_keeps_old_behavior(monkeypatch, tmp_path):
    """回归钉：store 有锚点的会话仍用最旧消息的 platform_msg_id（原路径零变化）。"""
    from src.inbox.models import InboxMessage

    calls = []

    async def fake_post(url, payload, timeout=20.0):
        calls.append(payload)
        return {"ok": True, "request_id": "req_2"}

    c, store = _history_client(monkeypatch, tmp_path, fake_post)
    cid = "whatsapp:639555000111:8613800138000"
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="WAMID_OLDEST",
        direction="in", text="hello", ts=1780000000.0))
    r = c.post("/api/platforms/whatsapp/639555000111/history",
               json={"chat_key": "8613800138000", "count": 10})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "requested": 10}
    assert calls and calls[0]["oldest_id"] == "WAMID_OLDEST"
    assert calls[0]["oldest_ts"] == 1780000000
    assert calls[0]["from_me"] is False
