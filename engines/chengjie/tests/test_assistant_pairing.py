# -*- coding: utf-8 -*-
"""小智手机扫码配对门禁（实施58 P4，2026-08-23）。

重点＝安全语义：token 单次核销+TTL、身份随 token 传递、注册表踢下线立即
失效、闲置过期、有界存储；另钉路由文件与前端组件的版本戳同步。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.assistant import pairing as pr

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean():
    pr._PAIR_TOKENS.clear()
    pr._PAIR_USED.clear()
    pr._MOBILE_SESSIONS.clear()
    yield
    pr._PAIR_TOKENS.clear()
    pr._PAIR_USED.clear()
    pr._MOBILE_SESSIONS.clear()


def test_pair_token_roundtrip_single_use():
    tok = pr.issue_pair_token("u1", "boss", "master")
    ent = pr.consume_pair_token(tok)
    assert ent and ent["uid"] == "u1" and ent["role"] == "master"
    # 短窗回放：预取核销后用户点开仍能拿到同一身份
    replay = pr.consume_pair_token(tok)
    assert replay and replay["uid"] == "u1"


def test_pair_token_replay_expires(monkeypatch):
    tok = pr.issue_pair_token("u1", "boss", "master")
    assert pr.consume_pair_token(tok)
    t0 = pr._time()
    monkeypatch.setattr(pr, "_time", lambda: t0 + pr.PAIR_REPLAY_SEC + 3)
    assert pr.consume_pair_token(tok) is None


def test_peek_and_attach_msid():
    tok = pr.issue_pair_token("u1", "boss", "master")
    assert pr.peek_pair_token(tok) is True
    ent = pr.consume_pair_token(tok)
    assert ent and pr.peek_pair_token(tok) is True
    pr.attach_msid(tok, "xzm-abc")
    again = pr.consume_pair_token(tok)
    assert again and again.get("msid") == "xzm-abc"
    assert pr.peek_pair_token("nope") is False


def test_pair_token_ttl(monkeypatch):
    tok = pr.issue_pair_token("u1", "boss", "master")
    t0 = pr._time()
    monkeypatch.setattr(pr, "_time", lambda: t0 + pr.PAIR_TTL_SEC + 3)
    assert pr.consume_pair_token(tok) is None


def test_mobile_register_ok_revoke():
    msid = pr.register_mobile("u1", "boss", "master", ua="iPhone Safari")
    assert pr.mobile_ok(msid) is True
    rows = pr.list_mobile("u1")
    assert len(rows) == 1 and rows[0]["msid"] == msid
    assert pr.list_mobile("other") == []
    assert pr.revoke_mobile(msid) is True
    assert pr.mobile_ok(msid) is False  # 踢下线立即失效
    assert pr.revoke_mobile(msid) is False


def test_mobile_idle_ttl_and_touch(monkeypatch):
    msid = pr.register_mobile("u1", "boss", "master")
    t0 = pr._time()
    # 触摸续活：过半个 TTL 时访问一次
    monkeypatch.setattr(pr, "_time",
                        lambda: t0 + pr.MOBILE_IDLE_TTL_SEC * 0.6)
    assert pr.mobile_ok(msid) is True
    # 再过 0.6 个 TTL（距上次触摸 <TTL）仍活
    monkeypatch.setattr(pr, "_time",
                        lambda: t0 + pr.MOBILE_IDLE_TTL_SEC * 1.2)
    assert pr.mobile_ok(msid) is True
    # 距上次触摸超 TTL → 过期
    monkeypatch.setattr(pr, "_time",
                        lambda: t0 + pr.MOBILE_IDLE_TTL_SEC * 2.5)
    assert pr.mobile_ok(msid) is False


def test_stores_bounded():
    for i in range(pr._MAX_ENTRIES + 10):
        pr.issue_pair_token(f"u{i}", "x", "admin")
        pr.register_mobile(f"u{i}", "x", "admin")
    assert len(pr._PAIR_TOKENS) <= pr._MAX_ENTRIES
    assert len(pr._MOBILE_SESSIONS) <= pr._MAX_ENTRIES


def test_pair_routes_ver_synced_with_agent_component():
    """手机页内嵌的 assistant-agent.js ?v= 必须与组件 VER 同步——
    否则扫码页永远跑旧缓存（前端批次双戳纪律的第三个消费点）。"""
    routes = (ROOT / "src" / "web" / "routes" /
              "assistant_pair_routes.py").read_text(encoding="utf-8")
    js = (ROOT / "shared" / "assistant" /
          "assistant-agent.js").read_text(encoding="utf-8")
    ver_route = re.search(r"_AGENT_JS_VER = \"([0-9a-z]+)\"", routes)
    ver_js = re.search(r"var VER = '([0-9a-z]+)'", js)
    assert ver_route and ver_js
    assert ver_route.group(1) == ver_js.group(1), (
        "assistant_pair_routes._AGENT_JS_VER 与组件 VER 不同步"
    )


def test_prefetch_header_detect():
    from src.web.routes.assistant_pair_routes import is_prefetch_request

    assert is_prefetch_request({"purpose": "prefetch"}) is True
    assert is_prefetch_request({"Sec-Purpose": "prefetch;prerender"}) is True
    assert is_prefetch_request({"X-Purpose": "preview"}) is True
    assert is_prefetch_request({"user-agent": "iPhone"}) is False


def test_lan_reachable_loopback():
    from src.web.routes.assistant_pair_routes import _lan_reachable

    assert _lan_reachable("127.0.0.1", 18799) is True
    assert _lan_reachable("localhost", 1) is True
    assert _lan_reachable("203.0.113.1", 1, timeout=0.05) is False


def _pair_client(monkeypatch):
    import types
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    from src.web.routes.assistant_pair_routes import register_assistant_pair_routes

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t", same_site="strict")

    @app.get("/_test/login")
    async def _login(request: Request):
        request.session["user_id"] = "u1"
        request.session["username"] = "boss"
        request.session["role"] = "master"
        request.session["display_name"] = "boss"
        return {"ok": True}

    cfg = {"assistant": {"enabled": True}}
    ctx = types.SimpleNamespace(
        api_auth=lambda r: None,
        config_manager=types.SimpleNamespace(config=cfg),
    )
    register_assistant_pair_routes(app, ctx)
    c = TestClient(app, follow_redirects=False)
    c.get("/_test/login")
    return c


def test_pair_issue_includes_lan_ok(monkeypatch):
    c = _pair_client(monkeypatch)
    r = c.post("/api/assistant/pair")
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert "lan_ok" in j and "url" in j
    assert "/xz?pair=xzp-" in j["url"]
    # #57 诊断字段（2026-08-30）：bind_host/lan_reason 常在（空串=不分型），
    # 前端据此把「后端只绑回环（升级可解）」与「防火墙拦入站（一键放行）」分开指路
    assert "bind_host" in j and "lan_reason" in j


def test_pair_issue_flags_loopback_bind(monkeypatch):
    """hairpin 不通 + web_admin.host 是回环 → lan_reason=loopback_bind（#57 根因分型）。"""
    import types
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    from src.web.routes import assistant_pair_routes as apr

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t", same_site="strict")

    @app.get("/_test/login")
    async def _login(request: Request):
        request.session["user_id"] = "u1"
        request.session["username"] = "boss"
        request.session["role"] = "master"
        return {"ok": True}

    cfg = {"assistant": {"enabled": True},
           "web_admin": {"host": "127.0.0.1"}}
    ctx = types.SimpleNamespace(
        api_auth=lambda r: None,
        config_manager=types.SimpleNamespace(config=cfg),
    )
    apr.register_assistant_pair_routes(app, ctx)
    # 强制「探到 LAN IP 但 hairpin 不通」的形态（真机上=只绑回环的实锤症状）
    monkeypatch.setattr(apr, "_lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(apr, "_lan_reachable", lambda h, p, timeout=0.4: False)
    c = TestClient(app, follow_redirects=False)
    c.get("/_test/login")
    j = c.post("/api/assistant/pair").json()
    assert j["ok"] is True and j["lan_ok"] is False
    assert j["bind_host"] == "127.0.0.1"
    assert j["lan_reason"] == "loopback_bind"
    # 绑定已放开（0.0.0.0）时同样不通 → 不分型（前端走通用红字+防火墙出口）
    cfg["web_admin"]["host"] = "0.0.0.0"
    j2 = c.post("/api/assistant/pair").json()
    assert j2["lan_ok"] is False and j2["lan_reason"] == ""


def test_xz_pair_lands_without_redirect(monkeypatch):
    c = _pair_client(monkeypatch)
    tok = pr.issue_pair_token("u1", "boss", "master")
    r = c.get("/xz?pair=" + tok)
    assert r.status_code == 200
    assert "XZAgent" in r.text
    assert r.headers.get("location") is None
    # 二次打开（预取后再点）仍 200，不 410
    r2 = c.get("/xz?pair=" + tok)
    assert r2.status_code == 200


def test_xz_prefetch_does_not_consume(monkeypatch):
    c = _pair_client(monkeypatch)
    tok = pr.issue_pair_token("u1", "boss", "master")
    r = c.get("/xz?pair=" + tok, headers={"Purpose": "prefetch"})
    assert r.status_code == 200
    assert "XZAgent" not in r.text  # 预取页不是操控页
    assert pr.peek_pair_token(tok) is True
    r2 = c.get("/xz?pair=" + tok)
    assert r2.status_code == 200
    assert "XZAgent" in r2.text
