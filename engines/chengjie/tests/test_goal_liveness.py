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


def test_collect_skips_drill_uids():
    """演练号段不算自动跟进库存——两条 duel 目标不得撑起 stalled。"""
    gs = _store()
    _auto(gs, age_h=40, chat="990001088")
    _auto(gs, age_h=40, chat="990001099")
    snap = collect_send_liveness(gs, now=NOW)
    assert snap["active_auto"] == 0
    assert snap["stalled_goals"] == []
    _auto(gs, age_h=40, chat="8017135513")
    snap2 = collect_send_liveness(gs, now=NOW)
    assert snap2["active_auto"] == 1
    assert all(
        "990001" not in str(s.get("conversation_id") or "")
        for s in snap2["stalled_goals"])


def test_collect_skips_structural_beat_blocked():
    """人审档 / 冻结等结构性拦拍：不算 stalled，也不撑起全局有货零真发。"""
    from src.companion.goals.sprint_ticker import record_beat_blocked

    gs = _store()
    g1 = _auto(gs, age_h=40, chat="jerry")
    g2 = _auto(gs, age_h=40, chat="cooper")
    record_beat_blocked(gs, g1["goal_id"], "automation_mode",
                        slot="d2026-09-14", now=NOW - 3600)
    record_beat_blocked(gs, g2["goal_id"], "automation_mode",
                        slot="d2026-09-15", now=NOW - 1800)
    snap = collect_send_liveness(gs, now=NOW)
    assert snap["active_auto"] == 2
    assert snap["sent_24h"] == 0
    assert snap["structural_blocked"] == 2
    assert snap["stalled_goals"] == []
    eng = {"sprint_effective": True}
    assert stall_verdict(eng, snap, min_active=2, min_age_sec=4 * 3600) is None

    # 节奏闸（silence）仍算真 stalled——引擎想发、只是等窗口
    g3 = _auto(gs, age_h=40, chat="real")
    record_beat_blocked(gs, g3["goal_id"], "silence",
                        slot="d2026-09-15", now=NOW - 600)
    snap2 = collect_send_liveness(gs, now=NOW)
    assert snap2["structural_blocked"] == 2
    assert [s["goal_id"] for s in snap2["stalled_goals"]] == [g3["goal_id"]]


def test_goal_sprint_goal_alert_wording_not_scan_stall():
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("scan_loop_stall_alert", {
        "loop": "goal_sprint_goal",
        "goal_id": "52d57b432fab410b",
        "conversation_id": "telegram:8244899900:990001088",
        "title": "",
        "reminder": False,
    })
    assert "常备循环停摆" not in title
    assert "自动跟进停住" in title
    assert "990001088" in text
    t2, _ = _build_message("scan_loop_stall_alert", {
        "loop": "goal_sprint_goal", "recovered": True,
        "rate_key": "scan_stall:goal:x:recovered",
    })
    assert "常备循环已恢复" not in t2
    assert "自动跟进已恢复" in t2


def test_goal_sprint_sends_severity_is_warning_not_critical():
    from src.inbox.webhook_notifier import _build_card, event_severity

    assert event_severity("scan_loop_stall_alert") == "critical"
    assert event_severity("scan_loop_stall_alert", {
        "loop": "goal_sprint"}) == "critical"
    assert event_severity("scan_loop_stall_alert", {
        "loop": "goal_sprint_sends"}) == "warning"
    assert event_severity("scan_loop_stall_alert", {
        "loop": "goal_sprint_goal"}) == "warning"
    card = _build_card("scan_loop_stall_alert", {
        "loop": "goal_sprint_sends", "active_auto": 2, "sent_24h": 0,
    }, "自动推进有货零真发", "判定")
    assert card.startswith("🟠 警告 · 运营")


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _wd(monkeypatch, store, cfg_extra=None, config_path=None):
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
    w._config_manager = SimpleNamespace(config=conf, config_path=config_path)
    w.total_goal_sprint_stall_alerts = 0
    # 无落盘路径时清空内存账本；有路径时保留（模拟重启读 health_remind_state.json）
    if not config_path:
        w._goal_sprint_stall_alerted = 0.0
    return w, bus


def test_watchdog_alerts_then_throttles_then_recovers(monkeypatch):
    gs = _store()
    # O-3 A（#236）：自然档目标的「够老」从**第一个可出手时刻**（建目标 +24h）起算——
    # 6h/8h 的自然档目标结构上还没到能出手的时候（HM7XBA 18:42 那声 stalled 就是
    # 这样喊错的），这里用 30h/32h（可出手已过 6h/8h）才该响。
    _auto(gs, age_h=30, chat="a")
    _auto(gs, age_h=32, chat="b")
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
             if n == "scan_loop_stall_alert"
             and p.get("loop") == "goal_sprint_sends"
             and not p.get("recovered")]
    assert len(hits2) == 1
    gid = gs.list_goals(status="active")[0]["goal_id"]
    gs.add_event(gid, "beat_sent", "ok")
    w._check_goal_sprint_liveness(now=NOW + 120)
    rec = [p for n, p in bus.events
           if n == "scan_loop_stall_alert" and p.get("recovered")]
    assert rec and rec[0]["rate_key"].endswith(":recovered")
    assert w._goal_sprint_stall_alerted == 0.0


def test_watchdog_goal_stall_throttle_survives_restart(monkeypatch, tmp_path):
    """节流落盘：假「重启」后同内容不得立刻重发（2026-09-16 运维群噪声）。"""
    gs = _store()
    _auto(gs, age_h=30, chat="a")
    _auto(gs, age_h=32, chat="b")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("x: 1\n", encoding="utf-8")
    w1, bus1 = _wd(monkeypatch, gs, config_path=str(cfg_file))
    w1._check_goal_sprint_liveness(now=NOW)
    assert sum(1 for n, p in bus1.events
               if n == "scan_loop_stall_alert"
               and p.get("loop") == "goal_sprint_sends"
               and not p.get("recovered")) == 1
    # 新实例、同一账本路径 → 视为进程重启
    w2, bus2 = _wd(monkeypatch, gs, config_path=str(cfg_file))
    w2._check_goal_sprint_liveness(now=NOW + 60)
    assert bus2.events == []
    # 过常规间隔仍会因指纹未变走 unchanged（默认 24h）→ 仍 HOLD
    w2._check_goal_sprint_liveness(now=NOW + 241 * 60)
    assert bus2.events == []


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
