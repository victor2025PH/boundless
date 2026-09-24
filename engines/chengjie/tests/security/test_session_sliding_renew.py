"""会话 cookie 滑动续签（登录 2 小时后固定失效的修复）。

新版 Starlette 的 SessionMiddleware 仅在 session 被修改时才重发 Set-Cookie，
于是 session_max_age 从「空闲超时」退化成「登录后固定时长」：桌面工作台开着
一直在用，2 小时一到所有 API 401、反复弹「登录已失效」。修复＝鉴权通过时按
节流往 session 写续签时间戳 _rn，强制 cookie 重签。
"""
from __future__ import annotations

import asyncio
import base64
import json

import yaml
from starlette.testclient import TestClient

from src.utils.audit_store import AuditStore
from src.utils.config_manager import ConfigManager
from src.web.admin import create_app

_TOKEN = "test-token-123"


def _make_app(tmp_path):
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": _TOKEN,
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


def _session_payload(client) -> dict:
    raw = client.cookies.get("session")
    assert raw, "未拿到 session cookie"
    b64 = raw.strip('"').split(".")[0]
    return json.loads(base64.b64decode(b64 + "=" * (-len(b64) % 4)))


def test_authenticated_api_call_writes_renew_stamp(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/login", data={"auth_token": _TOKEN}, follow_redirects=False)
        assert r.status_code == 303
        assert "_rn" not in _session_payload(c)
        r = c.get("/api/sessions")
        assert r.status_code == 200
        payload = _session_payload(c)
    assert isinstance(payload.get("_rn"), int) and payload["_rn"] > 0, (
        "鉴权通过的请求必须写续签时间戳，否则新版 Starlette 不重发 cookie、会话按登录时刻固定过期")


def test_renew_stamp_is_throttled(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        c.post("/login", data={"auth_token": _TOKEN}, follow_redirects=False)
        c.get("/api/sessions")
        first = _session_payload(c)["_rn"]
        c.get("/api/sessions")
        second = _session_payload(c)["_rn"]
    assert first == second, "续签需节流，不应每个请求都改 session"
