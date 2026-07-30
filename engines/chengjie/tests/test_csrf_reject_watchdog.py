# -*- coding: utf-8 -*-
"""CSRF 写请求拦截激增巡检（P1，2026-07-31 人设切换事故沉淀）。

事故教训：中间件 403 静默吞了两周——修复后拒绝虽进了看板（csrf_stats →
metrics/ops 卡），但**看板要有人开才有用**。本巡检把「观察窗内拦截激增」
升级为主动告警：cookie_no_header 激增＝某前端宿主写通道断裂（功能事故）；
origin/referer_mismatch/bare 激增＝反代漂移或跨站探测（安全信号）。

测试重点与本仓惯例一致：**「不该告警」的路径优先**（零误报是告警可信度的
生命线），其次才是首提/重提/恢复的状态机语义。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.web.csrf_stats import get_csrf_reject_stats


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, cfg_extra=None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    conf: Dict[str, Any] = {"health_watchdog": {"csrf_reject_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["csrf_reject_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace()
    w._config_manager = SimpleNamespace(config=conf)
    w._csrf_samples = []
    w._csrf_alerted = False
    w._csrf_last_remind = 0.0
    w.total_csrf_reject_alerts = 0
    return w, bus


def _reject(n: int, path: str = "/api/persona/bind", *, had_cookie: bool = True):
    st = get_csrf_reject_stats()
    for _ in range(n):
        st.record(path=path, had_cookie=had_cookie, had_header=False)


def setup_function(_fn):
    get_csrf_reject_stats().reset()


# ── 不该告警的路径 ────────────────────────────────────────────────────────

def test_no_alert_on_first_tick_even_with_history():
    """首个 tick 只建基线：即便历史累计很大，窗口增量=0，绝不告警。"""
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        w, bus = _wd(monkeypatch)
        _reject(50)
        w._check_csrf_rejects(now=time.time())
        assert bus.events == []
    finally:
        monkeypatch.undo()


def test_no_alert_below_threshold(monkeypatch):
    w, bus = _wd(monkeypatch, {"min_count": 5})
    t0 = time.time()
    w._check_csrf_rejects(now=t0)
    _reject(4)                                   # 4 < 5
    w._check_csrf_rejects(now=t0 + 60)
    assert bus.events == []


def test_disabled_config_is_silent(monkeypatch):
    w, bus = _wd(monkeypatch, {"enabled": False})
    t0 = time.time()
    w._check_csrf_rejects(now=t0)
    _reject(50)
    w._check_csrf_rejects(now=t0 + 60)
    assert bus.events == []


def test_no_reremind_inside_interval(monkeypatch):
    """告警后 interval 内增量继续 ≥ 阈值 → 不重提（去抖）。"""
    w, bus = _wd(monkeypatch, {"min_count": 5, "interval_min": 240})
    t0 = time.time()
    w._check_csrf_rejects(now=t0)
    _reject(6)
    w._check_csrf_rejects(now=t0 + 60)
    assert len(bus.events) == 1
    _reject(6)
    w._check_csrf_rejects(now=t0 + 120)          # interval 内
    assert len(bus.events) == 1


def test_no_recovery_notice_if_never_alerted(monkeypatch):
    """没告警过的抖动平息不发恢复通知（防噪）。"""
    w, bus = _wd(monkeypatch, {"min_count": 5, "window_min": 60})
    t0 = time.time()
    w._check_csrf_rejects(now=t0)
    _reject(2)
    w._check_csrf_rejects(now=t0 + 60)
    w._check_csrf_rejects(now=t0 + 3700)         # 窗口滑走，增量归零
    assert bus.events == []


# ── 该告警的路径 ─────────────────────────────────────────────────────────

def test_alert_carries_window_deltas_and_diagnosis(monkeypatch):
    """告警载荷=窗口增量（非累计）：形态与接口 Top 都按窗口内新增算。"""
    w, bus = _wd(monkeypatch, {"min_count": 5, "window_min": 60})
    t0 = time.time()
    _reject(100, "/api/old/noise")               # 基线前的历史存量
    w._check_csrf_rejects(now=t0)                # 建基线（含存量）
    _reject(4, "/api/persona/bind")
    _reject(3, "/api/unified-inbox/send", had_cookie=False)
    w._check_csrf_rejects(now=t0 + 60)

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "csrf_reject_alert"
    assert p["count"] == 7                        # 只算窗口增量，历史 100 不掺入
    assert p["by_kind"] == {"cookie_no_header": 4, "bare": 3}
    assert p["top_paths"] == {"/api/persona/bind": 4, "/api/unified-inbox/send": 3}
    assert p["reminder"] is False
    assert p["rate_key"] == "csrf_reject:remind"
    assert w.total_csrf_reject_alerts == 1


def test_reremind_after_interval_marks_reminder(monkeypatch):
    w, bus = _wd(monkeypatch, {"min_count": 5, "window_min": 600, "interval_min": 10})
    t0 = time.time()
    w._check_csrf_rejects(now=t0)
    _reject(6)
    w._check_csrf_rejects(now=t0 + 60)
    _reject(6)
    w._check_csrf_rejects(now=t0 + 60 + 11 * 60)  # 超过 interval
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_recovery_after_window_drains(monkeypatch):
    """告警过 + 窗口增量归零 → 补发恢复通知并复位状态机。"""
    w, bus = _wd(monkeypatch, {"min_count": 5, "window_min": 60})
    t0 = time.time()
    w._check_csrf_rejects(now=t0)
    _reject(6)
    w._check_csrf_rejects(now=t0 + 60)
    assert len(bus.events) == 1
    w._check_csrf_rejects(now=t0 + 3700)          # 全部样本滑出窗口 → 增量 0
    assert len(bus.events) == 2
    assert bus.events[1][1].get("recovered") is True
    assert w._csrf_alerted is False
    # 恢复后再次激增 → 能再次首提（状态机可循环）
    _reject(6)
    w._check_csrf_rejects(now=t0 + 3760)
    assert len(bus.events) == 3
    assert bus.events[2][1]["reminder"] is False
