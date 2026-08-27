# -*- coding: utf-8 -*-
"""P0-A3 2026-08-19：能力闸 403 必须带机器可读响应头 X-Deny-Reason。

前端 apiFetch choke point（_api_fetch.html::_capToast）靠这个头把「撞权限闸」
从死胡同错误文案升级为统一的「联系管理员开通」指路提示。两个 _deny_capability
副本（send / translate 路由族）+ 前端消费点由本门禁钉住——任何一端丢了接线先红。
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_both_deny_capability_copies_carry_header():
    for rel in ("src/web/routes/unified_inbox_send_routes.py",
                "src/web/routes/unified_inbox_translate_routes.py"):
        body = _src(rel)
        seg = body.split("def _deny_capability", 1)[1][:800]
        assert 'X-Deny-Reason' in seg and '"capability"' in seg, f"{rel} 丢失拒绝原因响应头"


def test_frontend_choke_point_consumes_header():
    body = _src("src/web/templates/_api_fetch.html")
    assert "X-Deny-Reason" in body and "_capToast" in body
    assert "ws.cap.denied_msg" in body           # i18n 键接线（双语在 inbox_workspace pack）


def test_fastapi_httpexception_supports_headers():
    """依赖契约自证：HTTPException(headers=) 真进响应（FastAPI 升级破坏此语义时先红）。"""
    from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: F401
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.post("/x")
    async def _x():
        raise HTTPException(403, "denied", headers={"X-Deny-Reason": "capability"})

    r = TestClient(app).post("/x")
    assert r.status_code == 403
    assert r.headers.get("X-Deny-Reason") == "capability"
