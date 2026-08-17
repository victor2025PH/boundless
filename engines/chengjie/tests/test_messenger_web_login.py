"""M5：Messenger 网页模式（Playwright 微服务）Python 桥接 单测（不联网）。"""

from __future__ import annotations

import asyncio
import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations import messenger_web_login as mgw
from src.integrations.account_registry import AccountRegistry


def test_service_base_url_default_and_override():
    assert mgw.service_base_url({}) == "http://127.0.0.1:8791"
    cfg = {"platform_login": {"messenger": {"web_url": "http://h:9/"}}}
    assert mgw.service_base_url(cfg) == "http://h:9"


def test_web_enabled_flag():
    assert mgw.web_enabled({}) is False
    assert mgw.web_enabled(
        {"platform_login": {"messenger": {"web_enabled": True}}}) is True


def test_normalize_status():
    assert mgw._normalize_status("open") == "authorized"
    assert mgw._normalize_status("connected") == "authorized"
    assert mgw._normalize_status("scanned") == "scanned"
    assert mgw._normalize_status("timeout") == "expired"
    assert mgw._normalize_status("logged_out") == "failed"
    assert mgw._normalize_status("whatever") == "pending"


def test_maybe_register_gating():
    mgw._registered = False
    pl._PROVIDERS.pop(pl._pkey("messenger", "web"), None)
    assert mgw.maybe_register({}) is False
    assert pl.mode_available("messenger", "web") is False
    try:
        assert mgw.maybe_register(
            {"platform_login": {"messenger": {"web_enabled": True}}}) is True
        assert pl.mode_available("messenger", "web") is True
    finally:
        mgw._registered = False
        pl._PROVIDERS.pop(pl._pkey("messenger", "web"), None)


def test_interactive_login_flag_default_off():
    # 安全底线：交互登录默认关（含桌面壳），只认显式配置。
    assert mgw.interactive_login_enabled({}) is False
    assert mgw.interactive_login_enabled(
        {"platform_login": {"messenger": {"interactive_login": True}}}) is True


def test_provider_relay_gated_by_flag(monkeypatch):
    """表单中继钩子必须被 interactive_login 门控：关 → interactive False + 钩子 None。"""
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "msg_relay", "qr_image": "", "status": "pending"}
        return {"ok": True}
    monkeypatch.setattr(mgw, "_post_json", fake_post)

    async def run(cfg):
        provider = mgw.make_provider(cfg)
        return await provider(None, "messenger", "web", "")

    off = asyncio.run(run({"platform_login": {"messenger": {"web_url": "http://x"}}}))
    assert off.get("interactive") is False
    assert off.get("relay_step") is None
    assert off.get("relay_submit") is None

    on = asyncio.run(run({"platform_login": {"messenger": {
        "web_url": "http://x", "interactive_login": True}}}))
    assert on.get("interactive") is True
    assert callable(on.get("relay_step"))
    assert callable(on.get("relay_submit"))


def test_provider_relay_step_and_submit_call_sidecar(monkeypatch):
    """开启交互登录时，relay_step/relay_submit 打到正确的 sidecar 端点、透传 step+values。"""
    seen = {"get": None, "post": None}

    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "msg_relay", "qr_image": "", "status": "pending"}
        seen["post"] = (url, payload)
        return {"ok": True, "submitted": True, "step": "credentials"}

    async def fake_get(url, timeout=20.0):
        seen["get"] = url
        return {"status": "pending", "step": "credentials",
                "fields": ["email", "password"], "booting": False}

    monkeypatch.setattr(mgw, "_post_json", fake_post)
    monkeypatch.setattr(mgw, "_get_json", fake_get)

    async def run():
        provider = mgw.make_provider({"platform_login": {"messenger": {
            "web_url": "http://x", "interactive_login": True}}})
        info = await provider(None, "messenger", "web", "")
        step = await info["relay_step"](None)
        assert step["step"] == "credentials"
        assert step["fields"] == ["email", "password"]
        sub = await info["relay_submit"](None, "credentials",
                                         {"email": "a@b.com", "password": "pw"})
        assert sub["ok"] is True and sub["submitted"] is True

    asyncio.run(run())
    assert seen["get"].endswith("/login/msg_relay/relay-step")
    assert seen["post"][0].endswith("/login/msg_relay/relay-submit")
    assert seen["post"][1]["step"] == "credentials"
    assert seen["post"][1]["values"]["email"] == "a@b.com"


def test_start_payload_carries_interactive_when_enabled(monkeypatch):
    """交互登录开启 → /login/start 带 interactive:true（边车据此走无窗口模式）；
    关闭 → 不带该字段（边车维持既有 headed 弹窗，零回归）。"""
    seen = {"start_payload": None}

    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            seen["start_payload"] = dict(payload)
            return {"login_id": "msg_x", "qr_image": "", "status": "pending"}
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", fake_post)

    async def run(cfg):
        provider = mgw.make_provider(cfg)
        await provider(None, "messenger", "web", "acc1")
        return seen["start_payload"]

    p_on = asyncio.run(run({"platform_login": {"messenger": {
        "web_url": "http://x", "interactive_login": True}}}))
    assert p_on.get("interactive") is True

    seen["start_payload"] = None
    p_off = asyncio.run(run({"platform_login": {"messenger": {"web_url": "http://x"}}}))
    assert "interactive" not in p_off


def test_provider_relay_step_soft_fallback(monkeypatch):
    """relay-step 探针异常 → 软回落 wait（绝不抛，不阻断登录链）。"""
    async def fake_post(url, payload, timeout=20.0):
        return {"login_id": "msg_relay", "qr_image": "", "status": "pending"}

    async def boom(url, timeout=20.0):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(mgw, "_post_json", fake_post)
    monkeypatch.setattr(mgw, "_get_json", boom)

    async def run():
        provider = mgw.make_provider({"platform_login": {"messenger": {
            "web_url": "http://x", "interactive_login": True}}})
        info = await provider(None, "messenger", "web", "")
        step = await info["relay_step"](None)
        assert step["step"] == "wait"

    asyncio.run(run())


def test_provider_flow_authorized(monkeypatch):
    # 伪造 Node/Playwright 微服务的 HTTP 响应
    async def fake_post(url, payload, timeout=20.0):
        if url.endswith("/login/start"):
            return {"login_id": "msg_abc", "qr_image": "data:image/png;base64,xxx",
                    "status": "pending"}
        return {"ok": True}

    calls = {"n": 0}

    async def fake_get(url, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "pending"}
        return {"status": "open", "account_id": "100012345678",
                "name": "Alice", "avatar_url": "http://x/a.jpg"}

    monkeypatch.setattr(mgw, "_post_json", fake_post)
    monkeypatch.setattr(mgw, "_get_json", fake_get)

    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "mg.db"))
    monkeypatch.setattr(mgw, "get_account_registry", lambda: reg)

    async def run():
        provider = mgw.make_provider(
            {"platform_login": {"messenger": {"web_url": "http://x"}}})
        info = await provider(None, "messenger", "web", "")
        assert info["qr_image"].startswith("data:image/png")
        poll = info["poll"]
        r1 = await poll(None)
        assert r1["status"] == "pending"
        r2 = await poll(None)
        assert r2["status"] == "authorized"
        assert r2["account_id"] == "100012345678"

    asyncio.run(run())
    # 登录成功应已写入注册表
    g = reg.get("messenger", "100012345678")
    assert g and g["mode"] == "web" and g["status"] == "online"


def test_provider_start_service_down(monkeypatch):
    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mgw, "_post_json", boom)

    async def run():
        provider = mgw.make_provider({})
        info = await provider(None, "messenger", "web", "")
        assert "instruction" in info
        assert "poll" not in info  # 服务不可达 → 仅返回提示，不进入轮询
        # reason_code 是上游把会话立刻置为 failed 的唯一依据（unified_inbox_login_routes
        # 的 `prov_reason and poll_fn is None` 分支）。漏掉它 → 会话按 pending 挂到 TTL
        # 耗尽，坐席对着转圈干等三分钟才等来超时，而真相只是服务没启动。
        assert info.get("reason_code") == "service_down"

    asyncio.run(run())


def test_start_route_returns_failed_when_service_down(monkeypatch):
    """端到端接缝：sidecar 不可达 → login/start 当场返回终态 + 原因码。

    provider 与路由各自正确、接缝却漏信息，是「坐席对着转圈干等三分钟」的现场：
    没有 reason_code 时会话按 pending 挂起，前端一路轮询到 TTL 耗尽才显示超时。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.integrations import platform_login as pl
    from src.web.routes import unified_inbox_login_routes as lr

    async def boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mgw, "_post_json", boom)
    # 直接塞注册表（monkeypatch 自动还原）：路由内的 _ensure_login_providers 是闭包，
    # 但 config_manager=None → 各 maybe_register 均因未启用而跳过，不会覆盖这里塞的 provider。
    monkeypatch.setitem(pl._PROVIDERS, "messenger:web", mgw.make_provider({}))
    # config_manager=None → platform_login.enabled 取缺省 True，无需另行放行
    monkeypatch.setattr(lr, "mode_available", lambda platform, mode: True)
    monkeypatch.setattr(lr, "status_via_adapters", lambda request, adapters: {})

    app = FastAPI()
    lr.register_platform_login_routes(
        app, api_auth=lambda request: None, config_manager=None)
    d = TestClient(app).post("/api/platforms/messenger/login/start",
                             json={"mode": "web"}).json()
    assert d.get("ok") is True
    assert d.get("status") == "failed"
    assert d.get("reason_code") == "service_down"
