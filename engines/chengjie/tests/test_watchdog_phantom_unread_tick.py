"""幽灵未读巡检（#159）真调用测试：_check_phantom_unread 真跑起来——超阈值告警、
间隔内不重复、清零报喜一次；关闭/无 store/取数失败静默跳过。

watchdog_check_audit 的「无真调用测试巡检」棘轮要求每个 _check_* 至少有一条真调用。
"""
from __future__ import annotations

import types

import pytest

from src.inbox.health_watchdog import HealthWatchdog


class _CM:
    def __init__(self, config):
        self.config = config


def _wd(cfg=None, store=True):
    state = types.SimpleNamespace(inbox_store=object() if store else None)
    app = types.SimpleNamespace(state=state)
    return HealthWatchdog(app=app, config_manager=_CM(cfg or {}), interval_sec=60)


@pytest.fixture
def bus(monkeypatch):
    published = []
    from src.integrations.shared import event_bus as eb

    class _Bus:
        def publish(self, t, d):
            published.append((t, d))

    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    return published


@pytest.fixture
def report(monkeypatch):
    rep = {"phantom": 7, "badge": 3, "store": 10, "by_account": {"tg:a": 6, "line:b": 1}}
    from src.inbox import unread_aggregate as ua
    monkeypatch.setattr(ua, "phantom_unread_report", lambda store: dict(rep))
    return rep


def test_phantom_over_threshold_alerts_then_throttles_then_recovers(bus, report):
    wd = _wd()
    wd._check_phantom_unread(now=1_000_000.0)
    alerts = [d for t, d in bus if t == "phantom_unread_alert"]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["phantom"] == 7 and a["reminder"] is False
    assert a["worst_accounts"][0] == {"account": "tg:a", "phantom": 6}
    assert wd.total_phantom_unread_alerts == 1

    # 间隔内（默认 360 分钟）不重复
    wd._check_phantom_unread(now=1_000_000.0 + 60)
    assert len([1 for t, _ in bus if t == "phantom_unread_alert"]) == 1

    # 过了间隔 → 提醒一次，标 reminder
    wd._check_phantom_unread(now=1_000_000.0 + 360 * 60 + 1)
    alerts = [d for t, d in bus if t == "phantom_unread_alert"]
    assert len(alerts) == 2 and alerts[1]["reminder"] is True

    # 清零 → 报喜一次，状态复位
    report["phantom"] = 0
    wd._check_phantom_unread(now=1_000_000.0 + 400 * 60)
    alerts = [d for t, d in bus if t == "phantom_unread_alert"]
    assert alerts[-1] == {"recovered": True, "rate_key": "phantom_unread:recovered"}
    wd._check_phantom_unread(now=1_000_000.0 + 401 * 60)
    assert len([1 for t, _ in bus if t == "phantom_unread_alert"]) == 3


def test_phantom_below_threshold_is_quiet(bus, report):
    report["phantom"] = 4      # 默认阈值 5
    wd = _wd()
    wd._check_phantom_unread(now=1.0)
    assert bus == [] and wd.total_phantom_unread_alerts == 0


def test_phantom_disabled_or_no_store_or_fetch_error_skips(bus, report, monkeypatch):
    _wd({"health_watchdog": {"phantom_unread_remind": {"enabled": False}}})._check_phantom_unread(now=1.0)
    _wd(store=False)._check_phantom_unread(now=1.0)
    from src.inbox import unread_aggregate as ua

    def _boom(store):
        raise RuntimeError("db gone")

    monkeypatch.setattr(ua, "phantom_unread_report", _boom)
    _wd()._check_phantom_unread(now=1.0)
    assert bus == []
