"""S1 回归门禁：空 auth_token 时 API 不再 fail-open。

历史缺陷：`_api_auth` 内 `if not token: return` 使得未配 auth_token（默认空串）
的部署下，所有 /api/* 对未认证请求裸奔（而页面走登录保护，形成 split-brain）。
本测试固定「空 token + 未认证 → 401」，防回潮。
"""
import asyncio

import pytest
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
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    asyncio.run(cm.load())
    audit = AuditStore(db_path=tmp_path / "audit.db")
    return create_app(cm, audit_store=audit, boot_ts=0, telegram_client=None,
                      event_tracker=None, log_buffer=None)


@pytest.mark.parametrize("path", ["/api/audit", "/api/notifications", "/api/analytics"])
def test_empty_token_unauthenticated_api_rejected(tmp_path, path):
    """空 auth_token + 未认证请求（TestClient 默认 host=testclient，非本机）→ 必须非 200。"""
    app = _make_app(tmp_path, auth_token="")
    with TestClient(app, raise_server_exceptions=True) as c:
        # follow_redirects=False：页面级鉴权会 303 跳 /login，不跟随以免被登录页 200 掩盖。
        r = c.get(path, follow_redirects=False)
    # 关键不变量：绝不能 200 裸奔；应为 401（API 鉴权）或 302/303（页面鉴权跳登录）。
    assert r.status_code != 200, f"{path} 在空 token 未认证下不应放行（得到 200）"
    assert r.status_code in (401, 302, 303), f"{path} 期望 401/302/303，得到 {r.status_code}"


def test_empty_token_write_api_rejected(tmp_path):
    """空 auth_token + 未认证写请求 → 必须被拦（401 鉴权 或 403 CSRF），绝不 200。"""
    app = _make_app(tmp_path, auth_token="")
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post("/api/data-purge", json={})
    assert r.status_code in (401, 403, 404), f"写接口期望被拦，得到 {r.status_code}"


def test_configured_token_bearer_still_works(tmp_path):
    """配置了 auth_token 时，带正确 Bearer 的请求仍应通过鉴权（不误伤正常路径）。"""
    app = _make_app(tmp_path, auth_token="test-token-123")
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get("/api/notifications", headers={"Authorization": "Bearer test-token-123"})
    assert r.status_code == 200, f"带正确 Bearer 应放行，得到 {r.status_code}"
