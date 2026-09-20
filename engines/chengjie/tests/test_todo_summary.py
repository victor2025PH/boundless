"""/api/todo-summary 聚合端点契约（仪表盘待办条单源，2026-08-02）。

不变量：
- 任一子系统不可用 → 对应键为 null（前端隐藏徽标），端点绝不 500；
- 只出计数不出内容（无消息文本 / 无用户标识字段）；
- 需要登录/令牌（未认证不得 200）。
"""

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from src.utils.audit_store import AuditStore
from src.utils.config_manager import ConfigManager
from src.web.admin import create_app


@pytest.fixture()
def app_dir(tmp_path):
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "general",
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hello"]}, allow_unicode=True), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}, allow_unicode=True), encoding="utf-8")
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump({"strategies": {}, "intent_strategy_map": {}},
                  allow_unicode=True), encoding="utf-8")
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture()
def client(app_dir):
    cm = ConfigManager(str(app_dir / "config.yaml"))
    asyncio.run(cm.load())
    audit = AuditStore(db_path=app_dir / "audit.db")
    app = create_app(cm, audit_store=audit, boot_ts=0)
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"Authorization": "Bearer test-token-123"})
        yield c


_KEYS = ("drafts_pending", "learner_pending", "crisis_unhandled",
         "cases_open", "sla_overdue", "sla_max_wait_min")


def test_todo_summary_shape_and_graceful_nulls(client):
    """最小 app（无 draft_service/sla_watcher/skill_manager）下端点仍 200，
    不可用子系统一律 null，可用的（learner 惰性构造）给非负整数。"""
    resp = client.get("/api/todo-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    for k in _KEYS:
        assert k in data, f"missing key {k}"
        v = data[k]
        assert v is None or (isinstance(v, int) and v >= 0), f"{k}={v!r}"
    # 无相应子系统的最小 app：这些必须是 null 而不是 0（口径：不知道≠没有）
    assert data["drafts_pending"] is None
    assert data["sla_overdue"] is None
    # 只出计数不出内容
    for forbidden in ("drafts", "samples", "items", "cases"):
        assert forbidden not in data


def test_todo_summary_counts_wired_subsystems(client):
    """挂上 draft_service / sla_watcher 假实现后返回真实计数（与权威接口同口径）。"""
    app = client.app

    class _FakeDraftSvc:
        def stats(self):
            return {"total_pending": 7, "by_platform": {}}

    class _FakeSla:
        def status_snapshot(self):
            return {"breaching_now": 2, "max_wait_min": 90,
                    "escalating_now": 1, "unclaimed_escalating": 0}

    app.state.draft_service = _FakeDraftSvc()
    app.state.sla_watcher = _FakeSla()
    try:
        data = client.get("/api/todo-summary").json()
        assert data["drafts_pending"] == 7
        assert data["sla_overdue"] == 2
        assert data["sla_max_wait_min"] == 90
    finally:
        app.state.draft_service = None
        app.state.sla_watcher = None


def test_todo_summary_requires_auth(app_dir):
    cm = ConfigManager(str(app_dir / "config.yaml"))
    asyncio.run(cm.load())
    audit = AuditStore(db_path=app_dir / "audit.db")
    app = create_app(cm, audit_store=audit, boot_ts=0)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/todo-summary")
        assert resp.status_code != 200
