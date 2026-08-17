# -*- coding: utf-8 -*-
"""常备扫描循环停摆巡检门禁（P4 2026-08-09）。

事故背景：目标结算/提醒扫描与工作链推进都曾挂在 report.enabled 闸死的
调度器上**静默从未运行**，三轮开发后才被生产探针拆穿。心跳（app.state 快照）
+ 本巡检把「没跑」变成主动告警。

覆盖：停摆首提/重提节流/恢复通知；未挂载（state 缺失）按启动宽限后同罪；
心跳新鲜不报；被配置闸住但心跳照跳＝不算停摆；开关关闭静默。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw

NOW = time.time()


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, *, goal_state=None, wf_state=None, cfg_extra=None,
        watch_since=None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    app = SimpleNamespace()
    if goal_state is not None:
        app.goal_scan_state = goal_state
    if wf_state is not None:
        app.workflow_autorun_state = wf_state
    conf: Dict[str, Any] = {
        "health_watchdog": {"scan_loop_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["scan_loop_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = app
    w._config_manager = SimpleNamespace(config=conf)
    if watch_since is not None:
        w._scanloop_alerted = {}
        w._scanloop_watch_since = watch_since
    return w, bus


def _stall_events(bus, loop):
    return [p for n, p in bus.events
            if n == "scan_loop_stall_alert"
            and p.get("loop") == loop and not p.get("recovered")]


def test_stalled_heartbeat_alerts_then_throttles_then_reminds(monkeypatch):
    goal_state = {"last_tick_ts": NOW - 30 * 60, "ticks": 42, "gated": ""}
    wf_state = {"last_tick_ts": NOW - 30, "ticks": 40, "gated": ""}
    w, bus = _wd(monkeypatch, goal_state=goal_state, wf_state=wf_state)
    w._check_scan_loop_stall(now=NOW)
    hits = _stall_events(bus, "goal_scan")
    assert len(hits) == 1
    p = hits[0]
    assert p["mounted"] is True and p["reminder"] is False
    assert p["stalled_min"] >= 29 and p["rate_key"] == "scan_stall:goal_scan"
    assert _stall_events(bus, "workflow_autorun") == []   # 心跳新鲜不报
    # 重提节流：间隔内不重复
    w._check_scan_loop_stall(now=NOW + 60)
    assert len(_stall_events(bus, "goal_scan")) == 1
    # 过了 interval_min → 重提且 reminder=True
    w._check_scan_loop_stall(now=NOW + 241 * 60)
    hits = _stall_events(bus, "goal_scan")
    assert len(hits) == 2 and hits[1]["reminder"] is True


def test_recovery_notice_only_after_alert(monkeypatch):
    goal_state = {"last_tick_ts": NOW - 30 * 60, "ticks": 1, "gated": ""}
    w, bus = _wd(monkeypatch, goal_state=goal_state,
                 wf_state={"last_tick_ts": NOW, "ticks": 9, "gated": ""})
    w._check_scan_loop_stall(now=NOW)
    assert len(_stall_events(bus, "goal_scan")) == 1
    # 心跳恢复 → 恢复通知 + 标记清除（再停摆可再报）
    goal_state["last_tick_ts"] = NOW + 300
    w._check_scan_loop_stall(now=NOW + 360)
    rec = [p for n, p in bus.events
           if n == "scan_loop_stall_alert" and p.get("recovered")]
    assert len(rec) == 1 and rec[0]["loop"] == "goal_scan"
    # 从未告过警的循环恢复不发（防噪）：workflow_autorun 无恢复事件
    assert all(p.get("loop") != "workflow_autorun" for p in rec)


def test_missing_state_alerts_after_grace(monkeypatch):
    # 宽限内（watch_since 刚设）：不报
    w, bus = _wd(monkeypatch, watch_since=NOW - 60)
    w._check_scan_loop_stall(now=NOW)
    assert bus.events == []
    # 宽限外仍无心跳 → mounted=False 告警（挂载失败与停摆同罪）
    w2, bus2 = _wd(monkeypatch, watch_since=NOW - 30 * 60)
    w2._check_scan_loop_stall(now=NOW)
    hits = _stall_events(bus2, "goal_scan")
    assert hits and hits[0]["mounted"] is False


def test_gated_loop_with_fresh_heartbeat_is_not_stall(monkeypatch):
    # 配置闸住（gated 非空）但心跳照跳＝正常态
    w, bus = _wd(
        monkeypatch,
        goal_state={"last_tick_ts": NOW, "ticks": 5,
                    "gated": "scans_disabled"},
        wf_state={"last_tick_ts": NOW, "ticks": 5,
                  "gated": "autorun_disabled"})
    w._check_scan_loop_stall(now=NOW)
    assert bus.events == []


def test_disabled_config_silent(monkeypatch):
    w, bus = _wd(monkeypatch,
                 goal_state={"last_tick_ts": NOW - 999 * 60, "ticks": 1},
                 cfg_extra={"enabled": False})
    w._check_scan_loop_stall(now=NOW)
    assert bus.events == []
