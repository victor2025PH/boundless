# -*- coding: utf-8 -*-
"""账号真相真幽灵巡检：会话库有、注册表没有（剔除 web / 桌面镜像）。

原「已登出未读积压」方案被否决——summary 对 logged_out/removed 强制 unread=0，
那条告警永远不响。本检查只盯 directory_ghost_keys 的真泄漏。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, directory, registry_rows, cfg_extra=None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    class _Reg:
        def list(self, include_removed=False):
            return list(registry_rows)

    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: _Reg())

    store = SimpleNamespace(account_directory=lambda: dict(directory))
    state = SimpleNamespace(inbox_store=store)
    conf: Dict[str, Any] = {
        "health_watchdog": {"accounts_truth_remind": {"enabled": True}},
    }
    if cfg_extra:
        conf["health_watchdog"]["accounts_truth_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = state
    w._config_manager = SimpleNamespace(config=conf)
    w._at_alerted = False
    w._at_last_remind = 0.0
    w.total_accounts_truth_alerts = 0
    return w, bus


def test_alerts_on_true_ghost_and_skips_desktop_and_web(monkeypatch):
    directory = {
        ("web", "web"): {"count": 9},
        ("telegram", "tg-desktop"): {"count": 7},
        ("telegram", "leak"): {"count": 1},
    }
    registry = [
        {"platform": "telegram", "account_id": "tg-desktop",
         "mode": "desktop", "status": "online"},
    ]
    w, bus = _wd(monkeypatch, directory, registry)
    w._check_accounts_truth(now=time.time())
    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "accounts_truth_alert"
    assert p["ghost_count"] == 1
    assert p["samples"] == ["telegram:leak"]
    assert w.total_accounts_truth_alerts == 1


def test_desktop_only_is_not_a_ghost(monkeypatch):
    directory = {("telegram", "tg-desktop"): {"count": 7}}
    registry = [{"platform": "telegram", "account_id": "tg-desktop",
                 "mode": "desktop", "status": "online"}]
    w, bus = _wd(monkeypatch, directory, registry)
    w._check_accounts_truth(now=time.time())
    assert bus.events == []


def test_disabled_is_silent(monkeypatch):
    directory = {("telegram", "leak"): {"count": 1}}
    w, bus = _wd(monkeypatch, directory, [], {"enabled": False})
    w._check_accounts_truth(now=time.time())
    assert bus.events == []


def test_recovers_when_ghosts_cleared(monkeypatch):
    directory = {("telegram", "leak"): {"count": 1}}
    w, bus = _wd(monkeypatch, directory, [])
    w._check_accounts_truth(now=time.time())
    assert bus.events[0][1].get("ghost_count") == 1
    w._app.inbox_store.account_directory = lambda: {}
    w._check_accounts_truth(now=time.time())
    assert bus.events[-1] == (
        "accounts_truth_alert",
        {"recovered": True, "rate_key": "accounts_truth:recovered"},
    )
    assert w._at_alerted is False


def test_reminder_respects_interval(monkeypatch):
    directory = {("telegram", "leak"): {"count": 1}}
    w, bus = _wd(monkeypatch, directory, [], {"interval_min": 240})
    t0 = time.time()
    w._check_accounts_truth(now=t0)
    w._check_accounts_truth(now=t0 + 60 * 60)
    assert len(bus.events) == 1
    w._check_accounts_truth(now=t0 + 4 * 3600 + 10)
    assert len(bus.events) == 2
    assert bus.events[1][1].get("reminder") is True


def test_tick_calls_accounts_truth_check():
    src = open(hw.__file__, encoding="utf-8").read()
    assert "_check_accounts_truth()" in src, "tick 未调用 _check_accounts_truth"
