# -*- coding: utf-8 -*-
"""page_auth 接受 Bearer 主令牌（P1 2026-08-09）。

背景：`_api_auth` 一直接受 ``Authorization: Bearer <auth_token>``（恒时比较），
但 send / send-media / send-voice 这三个 **JSON** 端点挂的是页面鉴权
``_page_auth`` → 脚本/集成用主令牌调用会被 303 到登录页 HTML。
.198/.104 排障实测：为发一条验证消息，只能模拟表单登录拿 session+CSRF——
巡检自动化的死路。修复＝_page_auth 前置与 _api_auth 同口径的 Bearer 短路。

守三条：
1. 正确 Bearer → 过鉴权、到达 handler（空 body 得 400 参数错，而非 303/登录 HTML）；
2. 错误 Bearer → 不放行（303 跳登录，与旧行为一致）；
3. 空 token 部署 → Bearer 头不可能放行（比较分支根本不进）。
"""
from __future__ import annotations

import asyncio

import yaml
from starlette.testclient import TestClient

from src.utils.audit_store import AuditStore
from src.utils.config_manager import ConfigManager
from src.web.admin import create_app


def _make_app(tmp_path, auth_token):
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": auth_token,
            "session_max_age": 3600,
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    asyncio.run(cm.load())
    audit = AuditStore(db_path=tmp_path / "audit.db")
    return create_app(cm, audit_store=audit, boot_ts=0, telegram_client=None,
                      event_tracker=None, log_buffer=None)


def test_send_endpoint_accepts_bearer_master(tmp_path):
    """正确 Bearer：过 page_auth、倒在参数校验（400），绝不再 303 登录页。"""
    app = _make_app(tmp_path, auth_token="test-token-123")
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post("/api/unified-inbox/send", json={},
                   headers={"Authorization": "Bearer test-token-123"},
                   follow_redirects=False)
    assert r.status_code == 400, (
        f"应过鉴权后倒在空参数校验（400），得到 {r.status_code}")


def test_send_endpoint_rejects_wrong_bearer(tmp_path):
    app = _make_app(tmp_path, auth_token="test-token-123")
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post("/api/unified-inbox/send", json={},
                   headers={"Authorization": "Bearer wrong-token"},
                   follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403), (
        f"错误 Bearer 不得放行，得到 {r.status_code}")


def test_empty_token_deploy_never_matches_bearer(tmp_path):
    """空 token 部署：任何 Bearer 头都不可能经该分支放行（token 空=分支不进）。"""
    app = _make_app(tmp_path, auth_token="")
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post("/api/unified-inbox/send", json={},
                   headers={"Authorization": "Bearer "},
                   follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403), (
        f"空 token 部署不得被 Bearer 放行，得到 {r.status_code}")
