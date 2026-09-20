"""B99（实施68 P1-9 残留 ⑤）：Messenger「临时封锁」→ ban_signal 账号级暂停契约。

事故（0825 _485）：E2EE PIN 态整号瘫 → 上游重试风暴 → FB 弹「你被暂时阻止」，
而 messenger 面此前**未接** ban_signal——号被平台风控了，系统还在往枪口上送。

链路：边车检出封锁文案 → 自冻结出站 + POST session-status(status="blocked") →
本路由 ①健康表登记（blocked ∈ UNHEALTHY，横幅/看门狗可见）②ban_signal
``apply_action(kind=pause, ttl=2h)`` 写账号级 kill_switch（TTL 自动恢复，
**绝不** auto_ban 永久冻结）③「进入不健康」告警照发。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_account_routes import register_account_routes


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch, tmp_path):
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


def _client():
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    return TestClient(app)


def test_blocked_is_unhealthy_status():
    from src.integrations.platform_session_health import (
        UNHEALTHY_STATUSES, get_platform_session_health,
    )
    assert "blocked" in UNHEALTHY_STATUSES
    s = get_platform_session_health()
    s.record("messenger", "100", "authorized")
    t = s.record("messenger", "100", "blocked", detail="fb_temporarily_blocked")
    assert t["went_unhealthy"] is True
    assert s.is_unhealthy("messenger", "100") is True


def test_blocked_push_applies_account_pause(monkeypatch):
    """status=blocked → apply_action(kind=pause) 写账号级 kill_switch（TTL>0）。"""
    calls = []

    class _FakeKS:
        def set(self, scope, **kw):
            calls.append((scope, kw))

    class _FakeReg:
        def get(self, *a, **k):
            return None

        def upsert(self, *a, **k):
            pass

    monkeypatch.setattr("src.ops.kill_switch.get_kill_switch", lambda: _FakeKS())
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: _FakeReg())

    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "61584011289581",
        "status": "blocked", "detail": "fb_temporarily_blocked",
    })
    assert r.status_code == 200
    assert len(calls) == 1
    scope, kw = calls[0]
    assert scope == "account:messenger:61584011289581"
    # pause 语义（TTL 自动恢复），绝不 auto_ban 永久冻结
    assert str(kw.get("reason") or "").startswith("auto_pause:platform_blocked")
    assert float(kw.get("ttl_sec") or 0) == pytest.approx(7200.0)


def test_blocked_without_account_id_no_freeze(monkeypatch):
    """login_id-only 的封锁上报：登记健康但不冻结（scope 需要真实 account_id）。"""
    calls = []

    class _FakeKS:
        def set(self, scope, **kw):
            calls.append(scope)

    monkeypatch.setattr("src.ops.kill_switch.get_kill_switch", lambda: _FakeKS())
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "login_id": "lg-abc",
        "status": "blocked", "detail": "fb_temporarily_blocked",
    })
    assert r.status_code == 200
    assert calls == []


def test_blocked_emits_unhealthy_alert():
    from src.integrations.shared.event_bus import get_event_bus
    c = _client()
    c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100",
        "status": "authorized",
    })
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100",
        "status": "blocked", "detail": "fb_temporarily_blocked",
    })
    assert r.status_code == 200
    assert r.json().get("went_unhealthy") is True
    evts = [e for e in get_event_bus().recent_events(50)
            if e["type"] == "platform_session_alert"]
    assert evts, "封锁进入不健康必须发 platform_session_alert"
    assert evts[-1]["data"]["status"] == "blocked"
