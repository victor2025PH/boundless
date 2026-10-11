# -*- coding: utf-8 -*-
"""幽灵未读巡检必须真跑起来（#159）。

源码里写 ``assert "_check_phantom_unread" in src`` 抓不到运行时缺导入。
本文件按 HealthWatchdog._check_phantom_unread 的真实分支调用它：
差额过阈值发 phantom_unread_alert，低于阈值不吵，清零补一条 recovered。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, report, cfg_extra=None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(
        "src.inbox.unread_aggregate.phantom_unread_report",
        lambda store: report,
    )
    conf: Dict[str, Any] = {
        "health_watchdog": {"phantom_unread_remind": {"enabled": True, "min_phantom": 5}},
    }
    if cfg_extra:
        conf["health_watchdog"]["phantom_unread_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(inbox_store=SimpleNamespace())
    w._config_manager = SimpleNamespace(config=conf)
    w._pu_alerted = False
    w._pu_last_remind = 0.0
    w.total_phantom_unread_alerts = 0
    return w, bus


def test_phantom_unread_over_threshold_alerts(monkeypatch):
    """差额过 min_phantom 必须发出事件并累加计数——这是巡检还活着的正面信号。"""
    w, bus = _wd(monkeypatch, {
        "phantom": 8, "badge": 2, "store": 10,
        "by_account": {"telegram:a": 8, "line:b": 1},
    })
    w._check_phantom_unread(now=1_000.0)
    assert len(bus.events) == 1
    name, payload = bus.events[0]
    assert name == "phantom_unread_alert"
    assert payload["phantom"] == 8
    assert payload["badge_total"] == 2
    assert payload["store_total"] == 10
    assert payload["reminder"] is False
    assert payload["worst_accounts"][0]["account"] == "telegram:a"
    assert w.total_phantom_unread_alerts == 1
    assert w._pu_alerted is True


def test_phantom_unread_under_threshold_is_quiet(monkeypatch):
    w, bus = _wd(monkeypatch, {"phantom": 2, "badge": 3, "store": 5, "by_account": {}})
    w._check_phantom_unread(now=1_000.0)
    assert bus.events == []
    assert w.total_phantom_unread_alerts == 0


def test_phantom_unread_recovery_notifies_once(monkeypatch):
    """报过警之后差额清零，补一条 recovered，并且只补一次。"""
    w, bus = _wd(monkeypatch, {"phantom": 0, "badge": 0, "store": 0, "by_account": {}})
    w._pu_alerted = True
    w._check_phantom_unread(now=2_000.0)
    assert len(bus.events) == 1
    assert bus.events[0][0] == "phantom_unread_alert"
    assert bus.events[0][1]["recovered"] is True
    assert w._pu_alerted is False
    w._check_phantom_unread(now=3_000.0)
    assert len(bus.events) == 1
