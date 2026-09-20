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


# ── D1b P0-5：冲刺推进器 / care 派发循环进同一张停摆表 ──────────────────────

class _LoopObj:
    """对象型循环桩（SprintGoalTicker / CareDispatcher 的最小契约）。"""

    def __init__(self, last_tick_ts, *, interval=120.0, running=True, ticks=7):
        self.last_tick_ts = last_tick_ts
        self._interval = interval
        self.ticks = ticks
        self._running = running

    def is_running(self):
        return self._running


def _fresh():
    return {"last_tick_ts": NOW, "ticks": 5, "gated": ""}


def test_loop_heartbeat_adapter_shape():
    assert hw._loop_heartbeat(None) is None
    hb = hw._loop_heartbeat(_LoopObj(NOW - 5, interval=300.0, running=False))
    assert hb == {"last_tick_ts": NOW - 5, "ticks": 7,
                  "interval_sec": 300.0, "running": False}

    class _Broken:
        @property
        def last_tick_ts(self):
            raise RuntimeError("boom")
    hb2 = hw._loop_heartbeat(_Broken())
    assert hb2 is not None and hb2["last_tick_ts"] == 0.0   # 不崩、按零心跳


def test_sprint_ticker_and_dispatcher_stall_alert(monkeypatch):
    w, bus = _wd(monkeypatch, goal_state=_fresh(), wf_state=_fresh())
    w._app.goal_sprint_ticker = _LoopObj(NOW - 30 * 60, running=False)
    w._app.care_engine = {"dispatcher": _LoopObj(NOW - 30 * 60)}
    w._check_scan_loop_stall(now=NOW)
    sp = _stall_events(bus, "goal_sprint")
    cd = _stall_events(bus, "care_dispatch")
    assert len(sp) == 1 and sp[0]["mounted"] is True
    assert sp[0]["running"] is False           # task 崩了：与「慢」区分
    assert sp[0]["ticks"] == 7 and sp[0]["rate_key"] == "scan_stall:goal_sprint"
    assert len(cd) == 1 and cd[0]["running"] is True
    assert _stall_events(bus, "goal_scan") == []


def test_sprint_ticker_fresh_heartbeat_not_stall_even_when_disabled(monkeypatch):
    # 配置关闸时 run_once 也打点（常备接线哲学）→ 心跳新鲜就不是停摆
    w, bus = _wd(monkeypatch, goal_state=_fresh(), wf_state=_fresh())
    w._app.goal_sprint_ticker = _LoopObj(NOW - 60)
    w._app.care_engine = {"dispatcher": _LoopObj(NOW - 90)}
    w._check_scan_loop_stall(now=NOW)
    assert bus.events == []


def test_stall_threshold_widens_with_loop_interval(monkeypatch):
    # ticker interval 热调到 10 分钟 → 阈值≥3 拍=30 分钟；20 分钟无心跳不算停摆
    w, bus = _wd(monkeypatch, goal_state=_fresh(), wf_state=_fresh())
    w._app.goal_sprint_ticker = _LoopObj(NOW - 20 * 60, interval=600.0)
    w._app.care_engine = {"dispatcher": _LoopObj(NOW)}
    w._check_scan_loop_stall(now=NOW)
    assert _stall_events(bus, "goal_sprint") == []
    # 过了 3 拍才报
    w._check_scan_loop_stall(now=NOW + 11 * 60)
    assert len(_stall_events(bus, "goal_sprint")) == 1


def test_sprint_ticker_from_care_engine_fallback_and_missing(monkeypatch):
    # app.state 没挂 goal_sprint_ticker 但 care_engine 里有 → 也认
    w, bus = _wd(monkeypatch, goal_state=_fresh(), wf_state=_fresh(),
                 watch_since=NOW - 30 * 60)
    w._app.care_engine = {"sprint_ticker": _LoopObj(NOW),
                          "dispatcher": _LoopObj(NOW)}
    w._check_scan_loop_stall(now=NOW)
    assert bus.events == []
    # 两处都没有 → 宽限外按未挂载告警
    w2, bus2 = _wd(monkeypatch, goal_state=_fresh(), wf_state=_fresh(),
                   watch_since=NOW - 30 * 60)
    w2._app.care_engine = {"dispatcher": _LoopObj(NOW)}
    w2._check_scan_loop_stall(now=NOW)
    hits = _stall_events(bus2, "goal_sprint")
    assert hits and hits[0]["mounted"] is False


def test_dispatcher_skipped_by_bootstrap_is_not_stall(monkeypatch):
    # ai_missing 等刻意跳过 → 不算停摆（那是配置态，不是故障）
    w, bus = _wd(monkeypatch, goal_state=_fresh(), wf_state=_fresh(),
                 watch_since=NOW - 30 * 60)
    w._app.goal_sprint_ticker = _LoopObj(NOW)
    w._app.care_engine = {"dispatcher_skip": "ai_missing"}
    w._check_scan_loop_stall(now=NOW)
    assert _stall_events(bus, "care_dispatch") == []


def test_webhook_formatter_names_new_loops():
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("scan_loop_stall_alert", {
        "loop": "goal_sprint", "stalled_min": 31.0, "mounted": True,
        "running": False, "ticks": 7, "reminder": False})
    assert "冲刺推进器" in title and "task 已退出" in text
    assert "一拍都不排" in text
    title2, text2 = _build_message("scan_loop_stall_alert", {
        "loop": "care_dispatch", "stalled_min": 12.0, "mounted": True,
        "running": True, "ticks": 3, "reminder": False})
    assert "派发循环" in title2 and "一条都不发" in text2
    assert "心跳停走" in text2                    # running=True → 慢，不是崩
