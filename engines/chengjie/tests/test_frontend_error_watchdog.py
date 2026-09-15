# -*- coding: utf-8 -*-
"""前端脚本错误巡检（2026-09-15 `_psnArRender` 事故沉淀）。

事故：模板半成品 hunk 热更上生产 → 人设工坊整体 ReferenceError 3.5 天。beacon
（`/api/telemetry/frontend-error`）与 ops 卡早已存在，缺的只是「把人叫醒」。本文件
钉住：三元组聚合 → 达阈值才响 → 新坏符号按间隔重提 / 同批一天一次 → 安静 12h 恢复；
以及「一次不响」「网络型不算」这些**不该响**的边界。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.web.frontend_error_stats import FrontendErrorStats, SCRIPT_BUG_TYPES


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, stats: FrontendErrorStats, cfg_extra=None):
    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr("src.web.frontend_error_stats.get_frontend_error_stats", lambda: stats)
    conf: Dict[str, Any] = {"health_watchdog": {"frontend_error_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["frontend_error_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=SimpleNamespace())
    w._config_manager = SimpleNamespace(config=conf)
    w.total_frontend_error_alerts = 0
    return w, bus


# ── stats 层：三元组聚合 ─────────────────────────────────────────────────────────

def test_stats_script_bugs_triplet_only_counts_script_types():
    s = FrontendErrorStats()
    s.record(page="/personas", fn="_psnArRender", etype="ReferenceError")
    s.record(page="/personas?x=1#profile=a", fn="_psnArRender", etype="ReferenceError")
    s.record(page="/personas", fn="apiFetch", etype="timeout")          # 网络型不算
    s.record(page="/workspace", fn="", etype="SyntaxError")
    snap = s.script_bugs_snapshot()
    assert snap["items"] == {"/personas ReferenceError _psnArRender": 2,
                             "/workspace SyntaxError unknown": 1}
    assert snap["total"] == 3 and snap["last_ts"] > 0
    assert "script_bugs" in s.dump()
    s.reset()
    assert s.script_bugs_snapshot() == {"items": {}, "total": 0, "last_ts": 0.0}
    assert SCRIPT_BUG_TYPES == {"ReferenceError", "SyntaxError"}


# ── 看门狗：该响的 ────────────────────────────────────────────────────────────

def test_alert_fires_at_min_count_then_holds(monkeypatch):
    t0 = time.time()
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s)
    s.record(page="/personas", fn="_psnArRender", etype="ReferenceError")
    w._check_frontend_errors(now=t0)
    assert bus.events == []                         # 一次不响（可能只是旧标签页）
    s.record(page="/personas", fn="_psnArRender", etype="ReferenceError")
    w._check_frontend_errors(now=t0 + 60)
    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "frontend_error_alert"
    assert p["symbols"] == 1 and p["hits"] == 2 and p["pages"] == ["/personas"]
    assert p["top"] == [["/personas ReferenceError _psnArRender", 2]]
    assert p["reminder"] is False and p["rate_key"] == "frontend_error:remind"
    assert w.total_frontend_error_alerts == 1
    # 同一批符号：1h / 4h 后都不重提（一天一次）
    s.record(page="/personas", fn="_psnArRender", etype="ReferenceError")
    w._check_frontend_errors(now=t0 + 3600)
    w._check_frontend_errors(now=t0 + 4 * 3600)
    assert len(bus.events) == 1


def test_new_symbol_triggers_reminder_after_interval(monkeypatch):
    t0 = time.time()
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s)
    for _ in range(2):
        s.record(page="/personas", fn="_psnArRender", etype="ReferenceError")
    w._check_frontend_errors(now=t0)
    assert len(bus.events) == 1
    for _ in range(2):
        s.record(page="/whatsapp", fn="_intentBadge", etype="ReferenceError")
    w._check_frontend_errors(now=t0 + 10 * 60)        # 未到 30min 间隔 → hold
    assert len(bus.events) == 1
    w._check_frontend_errors(now=t0 + 31 * 60)
    assert len(bus.events) == 2
    p = bus.events[1][1]
    assert p["reminder"] is True and p["unchanged"] is False
    assert p["symbols"] == 2 and set(p["pages"]) == {"/personas", "/whatsapp"}


def test_quiet_window_sends_recovery_once(monkeypatch):
    t0 = time.time()
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s)
    for _ in range(2):
        s.record(page="/personas", fn="_psnArRender", etype="ReferenceError")
    w._check_frontend_errors(now=t0)
    assert len(bus.events) == 1
    # 11h 后仍未安静够 → 不恢复；12h 后 → 恢复一次；再检查不重复
    w._check_frontend_errors(now=t0 + 11 * 3600)
    assert len(bus.events) == 1
    w._check_frontend_errors(now=t0 + 12 * 3600 + 5)
    assert len(bus.events) == 2 and bus.events[1][1]["recovered"] is True
    assert bus.events[1][1]["rate_key"] == "frontend_error:recovered"
    w._check_frontend_errors(now=t0 + 13 * 3600)
    assert len(bus.events) == 2


# ── 不该响的 ──────────────────────────────────────────────────────────────────

def test_network_type_errors_never_alert(monkeypatch):
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s)
    for _ in range(50):
        s.record(page="/workspace", fn="apiFetch", etype="neterr")
        s.record(page="/workspace", fn="loadThread", etype="timeout")
    w._check_frontend_errors(now=time.time())
    assert bus.events == []


def test_disabled_by_config(monkeypatch):
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s, {"enabled": False})
    for _ in range(5):
        s.record(page="/personas", fn="_x", etype="ReferenceError")
    w._check_frontend_errors(now=time.time())
    assert bus.events == []


def test_no_recovery_without_prior_alert(monkeypatch):
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s)
    w._check_frontend_errors(now=time.time())
    assert bus.events == []


def test_stats_failure_is_silent(monkeypatch):
    s = FrontendErrorStats()
    w, bus = _wd(monkeypatch, s)

    def _boom():
        raise RuntimeError("no stats")
    monkeypatch.setattr("src.web.frontend_error_stats.get_frontend_error_stats", _boom)
    w._check_frontend_errors(now=time.time())
    assert bus.events == []


def test_tick_wires_the_check():
    import inspect
    src = inspect.getsource(hw.HealthWatchdog._tick)
    assert "self._check_frontend_errors()" in src
