# -*- coding: utf-8 -*-
"""案例积压巡检（HealthWatchdog._check_case_backlog，2026-08-03 案例中心 P4）。

存在理由：case_alert 只在开案那一刻响一声，之后没人认领/处理是静默的。
两档判据（都只数**未认领且未结案**）：
- 危机档：severity 3 无人认领超 urgent_min（默认 30min）→ 立即报（一条就够格）；
- 常规档：severity ≤2 无人认领超 min_age_hours 达 min_count 条 → 聚合报。
重点覆盖「不该报」的路径（已认领/新鲜/数量不足/关开关/无 store）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw
from src.utils.case_center import claim_case, open_case


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


class _Store:
    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}


def _case(uid: str, source: str, age_h: float, *, claimed: str = "",
          now: float) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {"last_message": "x"}
    open_case(ctx, uid, source, f"case.reason.{source}", now=now - age_h * 3600.0)
    if claimed:
        claim_case(ctx, claimed, now=now)
    return ctx


def _wd(monkeypatch, store, cfg_extra=None, *, sm_present: bool = True):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    sm = SimpleNamespace(_context_store=store) if sm_present else None
    state = SimpleNamespace(skill_manager=sm, telegram_client=None)
    conf: Dict[str, Any] = {"health_watchdog": {"case_backlog_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["case_backlog_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=state)
    w._config_manager = SimpleNamespace(config=conf)
    w._cb_alerted = False
    w._cb_last_remind = 0.0
    w.total_case_backlog_alerts = 0
    return w, bus


def _backlog_events(bus):
    return [(n, p) for n, p in bus.events if n == "case_backlog_alert"]


def test_urgent_unclaimed_crisis_alerts_immediately(monkeypatch):
    """危机级无人认领超 30min：一条就报，不受 min_count 限制。"""
    now = time.time()
    store = _Store()
    store._cache["u1"] = _case("u1", "crisis", age_h=1.0, now=now)
    w, bus = _wd(monkeypatch, store)
    w._check_case_backlog(now=now)
    evts = _backlog_events(bus)
    assert len(evts) == 1
    p = evts[0][1]
    assert p["urgent_count"] == 1 and p["stale_count"] == 0
    assert p["by_source"] == {"crisis": 1}
    assert w.total_case_backlog_alerts == 1


def test_fresh_or_claimed_crisis_stays_silent(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["fresh"] = _case("fresh", "crisis", age_h=0.2, now=now)   # 12min < 30min
    store._cache["claimed"] = _case("claimed", "crisis", age_h=5.0,
                                    claimed="alice", now=now)
    w, bus = _wd(monkeypatch, store)
    w._check_case_backlog(now=now)
    assert _backlog_events(bus) == []


def test_regular_backlog_needs_min_count(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["a"] = _case("a", "human_request", age_h=6.0, now=now)
    store._cache["b"] = _case("b", "ai_doubt", age_h=8.0, now=now)
    w, bus = _wd(monkeypatch, store)          # 默认 min_count=3
    w._check_case_backlog(now=now)
    assert _backlog_events(bus) == []
    store._cache["c"] = _case("c", "media_complaint", age_h=5.0, now=now)
    w._check_case_backlog(now=now)
    evts = _backlog_events(bus)
    assert len(evts) == 1
    assert evts[0][1]["stale_count"] == 3


def test_reminder_interval_and_recovery(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["u1"] = _case("u1", "crisis", age_h=2.0, now=now)
    w, bus = _wd(monkeypatch, store)
    w._check_case_backlog(now=now)
    w._check_case_backlog(now=now + 60)          # 间隔内不重提
    assert len(_backlog_events(bus)) == 1
    w._check_case_backlog(now=now + 5 * 3600)    # 超间隔重提
    assert len(_backlog_events(bus)) == 2
    assert _backlog_events(bus)[-1][1]["reminder"] is True
    # 处理掉（认领即视为有人在跟）→ 恢复通知
    claim_case(store._cache["u1"], "alice", now=now)
    w._check_case_backlog(now=now + 6 * 3600)
    evts = _backlog_events(bus)
    assert evts[-1][1].get("recovered") is True
    assert w._cb_alerted is False


def test_disabled_or_missing_store_silent(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["u1"] = _case("u1", "crisis", age_h=2.0, now=now)
    w, bus = _wd(monkeypatch, store, {"enabled": False})
    w._check_case_backlog(now=now)
    assert bus.events == []
    w2, bus2 = _wd(monkeypatch, None, sm_present=False)
    w2._check_case_backlog(now=now)
    assert bus2.events == []


def test_closed_cases_do_not_count(monkeypatch):
    from src.utils.case_center import close_case
    now = time.time()
    store = _Store()
    ctx = _case("u1", "crisis", age_h=3.0, now=now)
    close_case(ctx, "handled", now=now)
    store._cache["u1"] = ctx
    w, bus = _wd(monkeypatch, store)
    w._check_case_backlog(now=now)
    assert _backlog_events(bus) == []


# ── P5 媒体档：单条媒体质疑超龄即报（穿帮风险不等凑数） ──────────────────────

def test_single_stale_media_alerts_without_min_count(monkeypatch):
    """1 条媒体质疑无人认领超 4h：常规档（min_count=3）不够格，媒体档兜住。"""
    now = time.time()
    store = _Store()
    store._cache["m1"] = _case("m1", "media_complaint", age_h=5.0, now=now)
    w, bus = _wd(monkeypatch, store)
    w._check_case_backlog(now=now)
    evts = _backlog_events(bus)
    assert len(evts) == 1
    p = evts[0][1]
    assert p["media_stale_count"] == 1
    assert p["media_stale_hours"] == 4.0
    assert p["media_oldest_hours"] >= 5.0
    assert p["urgent_count"] == 0
    # 媒体行同时计入常规 stale 名单（同一条案例，不玩双口径）
    assert p["stale_count"] == 1


def test_fresh_or_claimed_media_stays_silent(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["fresh"] = _case("fresh", "media_complaint", age_h=1.0, now=now)
    store._cache["claimed"] = _case("claimed", "media_complaint", age_h=9.0,
                                    claimed="alice", now=now)
    w, bus = _wd(monkeypatch, store)
    w._check_case_backlog(now=now)
    assert _backlog_events(bus) == []


def test_media_threshold_configurable_and_recovery(monkeypatch):
    from src.utils.case_center import claim_case as _claim
    now = time.time()
    store = _Store()
    store._cache["m1"] = _case("m1", "media_complaint", age_h=3.0, now=now)
    # 阈值放宽到 6h → 3h 的媒体案不报
    w, bus = _wd(monkeypatch, store, {"media_stale_hours": 6})
    w._check_case_backlog(now=now)
    assert _backlog_events(bus) == []
    # 收紧到 2h → 报；认领后 → 恢复通知
    w2, bus2 = _wd(monkeypatch, store, {"media_stale_hours": 2})
    w2._check_case_backlog(now=now)
    assert _backlog_events(bus2)[-1][1]["media_stale_count"] == 1
    _claim(store._cache["m1"], "alice", now=now)
    w2._check_case_backlog(now=now + 5 * 3600)
    assert _backlog_events(bus2)[-1][1].get("recovered") is True
    assert w2._cb_alerted is False


# ── P6：演练残影自动清扫（卫生动作：只清「不再活动」的 drill，零告警） ──────

def test_drill_hygiene_autocleans_idle_drill_only(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["990001004"] = _case("990001004", "media_complaint",
                                      age_h=7.0, now=now)     # 闲置 7h → 清
    store._cache["990001005"] = _case("990001005", "media_complaint",
                                      age_h=0.5, now=now)     # 还在跑 → 留
    store._cache["u_real"] = _case("u_real", "media_complaint",
                                   age_h=30.0, now=now)       # 真实客户永不动
    w, bus = _wd(monkeypatch, store)
    w._check_drill_hygiene(now=now)
    assert store._cache["990001004"]["_case_closed"] is True
    assert not store._cache["990001005"].get("_case_closed")
    assert not store._cache["u_real"].get("_case_closed")
    assert w.total_drill_autocleaned == 1
    assert bus.events == []          # 卫生动作零告警
    # 30min 节流：紧接着的第二轮不动（fresh 那条已到龄也要等下一窗）
    store._cache["990001005"]["_case_events"][-1]["ts"] = now - 7 * 3600
    w._check_drill_hygiene(now=now + 60)
    assert not store._cache["990001005"].get("_case_closed")
    # 过节流窗后清掉
    w._check_drill_hygiene(now=now + 1900)
    assert store._cache["990001005"]["_case_closed"] is True


def test_drill_hygiene_disabled_at_zero(monkeypatch):
    now = time.time()
    store = _Store()
    store._cache["990001004"] = _case("990001004", "media_complaint",
                                      age_h=48.0, now=now)
    w, bus = _wd(monkeypatch, store, {"drill_autoclean_hours": 0})
    w._check_drill_hygiene(now=now)
    assert not store._cache["990001004"].get("_case_closed")
