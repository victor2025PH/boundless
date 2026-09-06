# -*- coding: utf-8 -*-
"""D1b P0-5：自动推进「有货零真发」判据 + 看门狗巡检。"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.companion.goals.liveness import collect_send_liveness, stall_verdict
from src.companion.goals.store import GoalStore
from src.inbox import health_watchdog as hw

NOW = time.time()


def _store() -> GoalStore:
    return GoalStore(":memory:")


def _auto(gs: GoalStore, *, age_h: float, chat: str) -> Dict[str, Any]:
    return gs.create_goal(
        conversation_id=f"telegram:a1:{chat}", platform="telegram",
        account_id="a1", chat_key=chat, template="engagement_reactivate",
        autonomy="auto", deadline_days=14, now=NOW - age_h * 3600) or {}


def test_collect_counts_auto_and_recent_sends():
    gs = _store()
    _auto(gs, age_h=6, chat="old")
    gs.create_goal(
        conversation_id="telegram:a1:sug", platform="telegram",
        account_id="a1", chat_key="sug", template="engagement_reactivate",
        autonomy="suggest", deadline_days=14, now=NOW - 10 * 3600)
    snap = collect_send_liveness(gs, now=NOW)
    assert snap["active_auto"] == 1
    assert snap["sent_24h"] == 0
    assert snap["oldest_age_sec"] >= 5.5 * 3600

    gid = [g for g in gs.list_goals(status="active")
           if g.get("autonomy") == "auto"][0]["goal_id"]
    # M-7（#236）：care 钩子每次真发同时写 beat_sent + care_sent，看门狗只数
    # beat_sent 一种——一次真发只计 1（skuio 机 sent_24h=4 实为 2 次的双计修掉）
    gs.add_event(gid, "beat_sent", "daily:auto care#7")
    gs.add_event(gid, "care_sent", "每日主动拍已发出")
    snap2 = collect_send_liveness(gs, now=NOW)
    assert snap2["sent_24h"] == 1


def test_stall_verdict_only_when_engine_live_and_inventory_old():
    eng = {"sprint_effective": True}
    young = {"active_auto": 3, "sent_24h": 0, "oldest_age_sec": 3600}
    few = {"active_auto": 1, "sent_24h": 0, "oldest_age_sec": 10 * 3600}
    sent = {"active_auto": 3, "sent_24h": 1, "oldest_age_sec": 10 * 3600}
    stalled = {"active_auto": 2, "sent_24h": 0, "oldest_age_sec": 5 * 3600}
    assert stall_verdict({"sprint_effective": False}, stalled) is None
    assert stall_verdict(eng, young) is None
    assert stall_verdict(eng, few) is None
    assert stall_verdict(eng, sent) is None
    assert stall_verdict(eng, stalled) == "stalled"
    assert stall_verdict(eng, None) is None


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, store, cfg_extra=None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(
        "src.companion.goals.service.get_configured_store",
        lambda *a, **k: store)
    conf: Dict[str, Any] = {
        "companion": {
            "goals": {
                "enabled": True,
                "sprint": {"enabled": True, "dry_run": False,
                           "platforms": ["telegram", "whatsapp", "line"]},
            },
        },
        "health_watchdog": {
            "goal_sprint_liveness": {
                "enabled": True, "min_active": 2,
                "min_age_hours": 4, "interval_min": 240,
            },
        },
    }
    if cfg_extra:
        conf["health_watchdog"]["goal_sprint_liveness"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(config=conf, config_path=None)
    w._goal_sprint_stall_alerted = 0.0
    w.total_goal_sprint_stall_alerts = 0
    return w, bus


def test_watchdog_alerts_then_throttles_then_recovers(monkeypatch):
    gs = _store()
    _auto(gs, age_h=6, chat="a")
    _auto(gs, age_h=8, chat="b")
    w, bus = _wd(monkeypatch, gs)
    w._check_goal_sprint_liveness(now=NOW)
    hits = [p for n, p in bus.events
            if n == "scan_loop_stall_alert"
            and p.get("loop") == "goal_sprint_sends"
            and not p.get("recovered")]
    assert len(hits) == 1
    assert hits[0]["active_auto"] == 2 and hits[0]["sent_24h"] == 0
    assert hits[0]["rate_key"] == "scan_stall:goal_sprint_sends"
    assert w.total_goal_sprint_stall_alerts == 1
    w._check_goal_sprint_liveness(now=NOW + 60)
    hits2 = [p for n, p in bus.events
             if n == "scan_loop_stall_alert" and not p.get("recovered")]
    assert len(hits2) == 1
    gid = gs.list_goals(status="active")[0]["goal_id"]
    gs.add_event(gid, "beat_sent", "ok")
    w._check_goal_sprint_liveness(now=NOW + 120)
    rec = [p for n, p in bus.events
           if n == "scan_loop_stall_alert" and p.get("recovered")]
    assert rec and rec[0]["rate_key"].endswith(":recovered")
    assert w._goal_sprint_stall_alerted == 0.0


def test_watchdog_silent_when_engine_off_or_switch_off(monkeypatch):
    gs = _store()
    _auto(gs, age_h=8, chat="a")
    _auto(gs, age_h=8, chat="b")
    w, bus = _wd(monkeypatch, gs, cfg_extra={"enabled": False})
    w._check_goal_sprint_liveness(now=NOW)
    assert bus.events == []
    w2, bus2 = _wd(monkeypatch, gs)
    w2._config_manager.config["companion"]["goals"]["sprint"]["enabled"] = False
    w2._check_goal_sprint_liveness(now=NOW)
    assert bus2.events == []
