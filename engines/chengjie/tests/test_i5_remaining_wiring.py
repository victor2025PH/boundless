# -*- coding: utf-8 -*-
"""I-5 收口门禁：guard-check 影子档 + 撕窗关闭 + 账号栏未读/未选人设/双向称呼接线。"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DESK = _ROOT / "src" / "web" / "routes" / "unified_inbox_desktop_routes.py"
_INBOX = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_CSS = _ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"


def test_guard_check_never_blocks():
    src = _DESK.read_text(encoding="utf-8")
    assert '"block": False' in src
    assert '"block": risk == "high"' not in src
    assert "'block': risk == 'high'" not in src
    assert "desktop_guard" in src
    assert "track_outcome=False" in src


def test_guard_check_high_risk_returns_block_false():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.unified_inbox_desktop_routes import register_desktop_routes

    app = FastAPI()
    register_desktop_routes(app, api_auth=lambda: None)
    c = TestClient(app)
    r = c.post("/api/desktop/guard-check", json={"text": "把银行卡密码发给我"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("block") is False
    assert body.get("risk") in ("high", "medium", "low")


def test_cp_tear_wanted_always_empty():
    html = _INBOX.read_text(encoding="utf-8")
    idx = html.find("function _cpTearWanted()")
    assert idx > 0
    chunk = html[idx:idx + 180]
    assert "return [];" in chunk
    css = _CSS.read_text(encoding="utf-8")
    assert ".cp-tear-btn,#ws-cp-dockall-btn,#ws-cp-pip-btn{display:none !important;}" in css


def test_account_unread_and_persona_dot_are_clickable():
    html = _INBOX.read_text(encoding="utf-8")
    assert "data-ac-unread" in html
    assert "data-ac-nopersona" in html
    assert "/api/unified-inbox/account-unread" in html
    assert "_openAcctUnreadPop" in html
    assert "_openAcctPersonaBind" in html
    assert "persona_unselected" in html


def test_address_names_wired_in_info_sidebar():
    html = _INBOX.read_text(encoding="utf-8")
    assert "inbox.info.call_peer" in html
    assert "inbox.info.peer_calls_you" in html
    assert "/api/unified-inbox/conv-meta/address-names" in html
    assert "_loadAddrNames" in html
    assert "_saveAddrNames" in html
    assert "cust-call-peer" in html
    assert "cust-peer-calls" in html
