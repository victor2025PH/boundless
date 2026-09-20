"""R9b Web：/api/crisis-events 列表 + /handle 标记处置（鉴权 + 接线）。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from starlette.testclient import TestClient

from src.utils.audit_store import AuditStore
from src.utils.config_manager import ConfigManager
from src.web.admin import create_app


async def _load_cm(tmp_path: Path) -> ConfigManager:
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump(
            {
                "strategies": {
                    "standard": {
                        "temperature": 0.7,
                        "max_tokens": 800,
                        "context_rounds": 3,
                        "enabled": True,
                    }
                },
                "intent_strategy_map": {"default": "standard"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def crisis_client(tmp_path):
    from src.utils.crisis_event_store import CrisisEventStore

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    store = CrisisEventStore(tmp_path / "crisis.db")
    store.record(user_id="u1", level="severe", category="self_harm",
                 streak=2, escalated=True, excerpt="我不想活了")
    store.record(user_id="u2", level="elevated", excerpt="好绝望")

    tc = MagicMock()
    sm = MagicMock()
    sm.crisis_list_for_admin.side_effect = lambda **kw: store.list_recent(
        limit=kw.get("limit", 50),
        only_unhandled=kw.get("only_unhandled", False),
        user_prefix=kw.get("user_prefix", ""),
    )
    sm.crisis_count_for_admin.side_effect = lambda **kw: store.count(
        only_unhandled=kw.get("only_unhandled", False)
    )
    sm.crisis_mark_handled_for_admin.side_effect = lambda eid, **kw: store.mark_handled(
        eid, handled_by=kw.get("handled_by", ""), note=kw.get("note", "")
    )
    sm._crisis_store = store   # #185：count_since 空态窗口口径
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        from src.utils.web_user_store import ROLE_MASTER, WebUserStore

        wstore = WebUserStore(tmp_path / "web_users.db")
        if wstore.user_count() == 0:
            wstore.create_user("admin", "test-token-123", ROLE_MASTER)
        client.get("/login")
        client.post(
            "/login",
            data={"username": "admin", "password": "test-token-123"},
            follow_redirects=True,
        )
        client.headers.update({"Authorization": "Bearer test-token-123"})
        yield client, store


# ── #185 三档空态 + 一键开启（2026-09-05） ──────────────────────────────────

def test_audit_page_state_pure():
    from src.web.routes.crisis_audit_routes import audit_page_state, wellbeing_switches

    assert audit_page_state(audit_on=False, count_in_window=0, total_count=0) == "audit_off"
    # 留痕关了但库里还有历史 → 历史照常可见（has_events）；「现在没人在记」由页面红条
    # 按 audit_on=false 单独亮，不靠空态档
    assert audit_page_state(audit_on=False, count_in_window=0, total_count=9) == "has_events"
    assert audit_page_state(audit_on=True, count_in_window=0, total_count=0) == "audit_on_empty"
    # 表里有行就是 has_events（历史可见）；audit_on_empty 只属「留痕开着且全表为空」
    assert audit_page_state(audit_on=True, count_in_window=0, total_count=3) == "has_events"
    assert audit_page_state(audit_on=True, count_in_window=1, total_count=3) == "has_events"

    off = wellbeing_switches({"companion": {"wellbeing": {}}})
    assert off == {"wellbeing_enabled": True, "audit_on": False, "escalation_on": False}
    on = wellbeing_switches({"companion": {"wellbeing": {
        "crisis_audit": True, "crisis_escalation": True}}})
    assert on["audit_on"] and on["escalation_on"]
    # wellbeing 总闸关 → 留痕/升级视同关（页面亮红条，不装保护着）
    dis = wellbeing_switches({"companion": {"wellbeing": {
        "enabled": False, "crisis_audit": True, "crisis_escalation": True}}})
    assert dis["wellbeing_enabled"] is False
    assert dis["audit_on"] is False and dis["escalation_on"] is False
    assert wellbeing_switches(None)["audit_on"] is False


def test_list_reports_switches_when_flag_missing(crisis_client):
    """默认配置没配 crisis_audit → audit_on=false（页面红条据此亮）；历史事件照常可见。"""
    client, _ = crisis_client
    d = client.get("/api/crisis-events").json()
    assert d["audit_on"] is False and d["escalation_on"] is False
    assert d["wellbeing_enabled"] is True
    assert d["window_days"] >= 7
    assert d["total"] == 2
    assert d["state"] == "has_events"


def test_list_reports_audit_off_when_flag_missing_and_empty(tmp_path):
    """空库 + 留痕未开 → state=audit_off（页面绝不渲染「好消息」）。"""
    from src.utils.crisis_event_store import CrisisEventStore

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    store = CrisisEventStore(tmp_path / "crisis.db")
    tc = MagicMock()
    sm = MagicMock()
    sm.crisis_list_for_admin.side_effect = lambda **kw: store.list_recent(limit=kw.get("limit", 50))
    sm.crisis_count_for_admin.side_effect = lambda **kw: store.count(
        only_unhandled=kw.get("only_unhandled", False))
    sm._crisis_store = store
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        d = client.get("/api/crisis-events").json()
        assert d["state"] == "audit_off"
        assert d["count"] == 0 and d["total"] == 0
        # 开了留痕之后：空库 → audit_on_empty（这才是「好消息」档）
        cm.set_overlay_flag("companion.wellbeing.crisis_audit", True)
        d2 = client.get("/api/crisis-events").json()
        assert d2["state"] == "audit_on_empty"
        assert d2["audit_on"] is True


def test_enable_writes_overlay_preserving_comments_and_flips_state(crisis_client, tmp_path):
    client, _ = crisis_client
    overlay = tmp_path / "config.local.yaml"
    overlay.write_text(
        "# 运维手写注释：这行不能被剃掉\n"
        "companion:\n"
        "  selfie:\n"
        "    enabled: false   # 行尾注释也要活着\n",
        encoding="utf-8",
    )
    r = client.post("/api/crisis-events/enable", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["applied"] == [
        "companion.wellbeing.crisis_audit", "companion.wellbeing.crisis_escalation",
    ]
    assert d["audit_on"] is True and d["escalation_on"] is True
    text = overlay.read_text(encoding="utf-8")
    assert "运维手写注释" in text, "overlay 注释必须保留（ruamel round-trip）"
    assert "行尾注释也要活着" in text
    data = yaml.safe_load(text)
    assert data["companion"]["wellbeing"]["crisis_audit"] is True
    assert data["companion"]["wellbeing"]["crisis_escalation"] is True
    assert data["companion"]["selfie"]["enabled"] is False, "旁边的键不得被改"
    # 列表接口即时反映：留痕开了、窗口内有 2 条 → has_events
    d2 = client.get("/api/crisis-events").json()
    assert d2["state"] == "has_events"
    assert d2["audit_on"] is True


def test_enable_audit_only_when_escalation_false(crisis_client, tmp_path):
    client, _ = crisis_client
    r = client.post("/api/crisis-events/enable", json={"escalation": False})
    assert r.status_code == 200
    d = r.json()
    assert d["applied"] == ["companion.wellbeing.crisis_audit"]
    assert d["audit_on"] is True and d["escalation_on"] is False
    data = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert "crisis_escalation" not in data["companion"]["wellbeing"]


def test_enable_requires_write_permission(tmp_path):
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=None)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post("/api/crisis-events/enable", json={},
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code in (401, 403)
    assert not (tmp_path / "config.local.yaml").exists()


def test_enable_audited(crisis_client, tmp_path):
    client, _ = crisis_client
    client.post("/api/crisis-events/enable", json={})
    audit = AuditStore(db_path=tmp_path / "audit.db")
    rows = audit.query(limit=20, action="crisis_audit_enable")
    acts = [str(r.get("action") or "") for r in rows]
    assert "crisis_audit_enable" in acts


def test_list_crisis_events(crisis_client):
    client, _ = crisis_client
    r = client.get("/api/crisis-events")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 2
    assert d["unhandled_total"] == 2


def test_list_only_unhandled_filter(crisis_client):
    client, store = crisis_client
    # 处理掉一条
    first_id = store.list_recent()[-1]["id"]
    store.mark_handled(first_id, handled_by="x")
    r = client.get("/api/crisis-events?only_unhandled=true")
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_handle_marks_event(crisis_client):
    client, store = crisis_client
    eid = store.list_recent()[0]["id"]
    r = client.post(f"/api/crisis-events/{eid}/handle", json={"note": "已电话联系"})
    assert r.status_code == 200
    assert r.json()["handled"] == eid
    row = [x for x in store.list_recent() if x["id"] == eid][0]
    assert row["handled"] is True
    assert row["note"] == "已电话联系"


def test_handle_missing_event_404(crisis_client):
    client, _ = crisis_client
    r = client.post("/api/crisis-events/99999/handle", json={})
    assert r.status_code == 404


def test_list_requires_auth(tmp_path):
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=None)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/api/crisis-events", headers={"Authorization": "Bearer wrong"})
        assert r.status_code in (401, 403)


def test_crisis_audit_page_loads(crisis_client):
    client, _ = crisis_client
    r = client.get("/crisis-audit")
    assert r.status_code == 200
    assert 'id="ca-body"' in r.text
    assert "/api/crisis-events" in r.text


def test_alert_status_crisis_unhandled(crisis_client):
    client, _ = crisis_client
    r = client.get("/api/alert-status")
    assert r.status_code == 200
    alerts = r.json().get("alerts") or []
    crisis = [a for a in alerts if a.get("type") == "crisis"]
    assert len(crisis) == 1
    assert crisis[0]["level"] == "critical"
    assert "/crisis-audit" in crisis[0].get("action_url", "")
