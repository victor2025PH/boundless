# -*- coding: utf-8 -*-
"""M-7 C（#236 = #166 族第五次复报）：到期结算不静默。

取证（发版对账_v1.0.77_M7 §2 问 3）：BABY BEAR 原目标（3+1 天自然档）09-07 静默到期——
自然档不进 scan_sprint_miss（只收 today/session），进 scan_miss_digest 又卡「3 条或 24h」
门槛；goal_miss_alert 只到 webhook，工作台 SSE/铃铛白名单不认；用户重建后卡片 last=None，
上一目标结局从卡上消失。

本文件钉：
1. settle-on-read 翻终态那一刻写 ``settlement`` 事件（幂等）+ 发 ``goal_settled_alert``
   （不经聚合门槛、不受 notify.enabled 闸）；done 只记摘要不推；
2. 摘要字段：planned / sent / injected / blocked(+by) / replies / reason 码；
3. 工作台白名单 + i18n 映射 + 前端 _TYPE_META 登记齐备（通知中心门禁同口径）；
4. for-conversation：无活跃目标 → last 带 settlement + settled_recent（7 天）；
   新目标 → prev_settled 带上一目标结算。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from src.companion.goals import notify
from src.companion.goals.service import refresh_goal
from src.companion.goals.sprint_ticker import record_beat_blocked, record_natural_beat_sent
from src.companion.goals.store import GoalStore

REPO = Path(__file__).resolve().parents[1]
NOW = time.time()
CONV = "whatsapp:17345893506:13308422244"
CFG = {"companion": {"goals": {"enabled": True, "sprint": {"enabled": True}}}}


class _Bus:
    def __init__(self):
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


class _Inbox:
    def __init__(self, replies=0, name="BABY BEAR"):
        self.replies = replies
        self.name = name

    def count_inbound_between(self, conv, since, until=0.0):
        return self.replies

    def get_conversations_for_ids(self, ids):
        return {c: {"display_name": self.name} for c in ids}

    def list_recent_messages(self, conv, limit=10):
        return []


def _expired_goal(gs: GoalStore, **kw):
    base = dict(
        conversation_id=CONV, platform="whatsapp", account_id="17345893506",
        chat_key="13308422244", template="custom", autonomy="auto",
        params={"note": "把关系推进到愿意视频"}, deadline_days=3,
        now=NOW - 4 * 86400,                      # 3 天目标、4 天前建 → 已过期
    )
    base.update(kw)
    return gs.create_goal(**base)


# ── 1/2. 翻终态那一刻结算 + 推工作台事件 ─────────────────────────────────────
def test_refresh_expires_goal_and_settles_once_with_alert(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    gs = GoalStore(":memory:")
    g = _expired_goal(gs)
    gid = g["goal_id"]
    # 过程中的痕迹：一次主动拍、两次被沉默闸拦、一次顺势带方向
    record_natural_beat_sent(gs, gid, "2026-09-04", now=NOW - 3 * 86400, care_id=9)
    record_beat_blocked(gs, gid, "silence", slot="d2026-09-05", now=NOW - 2 * 86400)
    record_beat_blocked(gs, gid, "silence", slot="d2026-09-06", now=NOW - 86400)
    gs.add_event(gid, "beat_injected", "draft:2026-09-06", now=NOW - 80000)

    res = refresh_goal(gs, CFG, gs.get_goal(gid), inbox_store=_Inbox(replies=7),
                       now=NOW, inbound_turn=True)
    assert res["goal"]["status"] == "expired"
    ev = gs.last_event(gid, notify.SETTLEMENT_EVENT_KIND)
    assert ev is not None and ev["conversation_id"] == CONV
    s = json.loads(ev["detail"])
    assert s["status"] == "expired"
    assert s["planned"] == 3 and s["sent"] == 1 and s["injected"] == 1
    assert s["blocked"] == 2 and s["blocked_by"] == {"silence": 2}
    assert s["replies"] == 7
    assert s["reason"] == "blocked:silence"
    alerts = [p for n, p in bus.events if n == notify.SETTLED_ALERT]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["goal_id"] == gid and a["conversation_id"] == CONV
    assert a["contact_name"] == "BABY BEAR" and a["status"] == "expired"
    assert a["settlement"]["sent"] == 1 and a["rate_key"] == f"goal_settled:{gid}"
    # 幂等：再 refresh / 再 settle 不重复
    refresh_goal(gs, CFG, gs.get_goal(gid), inbox_store=_Inbox(), now=NOW + 60)
    assert notify.settle_and_notify(gs, gs.get_goal(gid), cfg_root=CFG, now=NOW + 61) is None
    assert len(gs.list_events(gid, kinds=(notify.SETTLEMENT_EVENT_KIND,))) == 1
    assert len([1 for n, _ in bus.events if n == notify.SETTLED_ALERT]) == 1


def test_settlement_not_gated_by_notify_switch_or_digest_threshold(monkeypatch):
    """notify.enabled=False（日报关）也结算；1 条、未满 24h（日报门槛未到）也推。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    cfg = {"companion": {"goals": {"enabled": True, "sprint": {"enabled": True},
                                   "notify": {"enabled": False, "miss_min_count": 3}}}}
    gs = GoalStore(":memory:")
    gid = _expired_goal(gs)["goal_id"]
    refresh_goal(gs, cfg, gs.get_goal(gid), now=NOW)
    assert [n for n, _ in bus.events] == [notify.SETTLED_ALERT]
    s = notify.read_settlement(gs, gid)
    assert s and s["reason"] == "never_due" and s["sent"] == 0


def test_reason_codes_cover_steered_only_no_reply_no_signal_engine():
    rc = notify._reason_code
    assert rc(status="expired", sent=0, injected=3, blocked_by={}, engine_blockers=[],
              replies=5) == "steered_only"
    assert rc(status="expired", sent=0, injected=0, blocked_by={}, engine_blockers=[],
              replies=0) == "never_due"
    assert rc(status="expired", sent=2, injected=0, blocked_by={}, engine_blockers=[],
              replies=0) == "no_reply"
    assert rc(status="failed", sent=2, injected=1, blocked_by={}, engine_blockers=[],
              replies=4) == "no_signal"
    assert rc(status="expired", sent=0, injected=0, blocked_by={"silence": 1},
              engine_blockers=["sprint_disabled"], replies=0) == "engine:sprint_disabled"
    assert rc(status="done", sent=0, injected=0, blocked_by={}, engine_blockers=[],
              replies=0) == "done"


def test_done_records_summary_but_does_not_alert(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    gs = GoalStore(":memory:")
    g = gs.create_goal(conversation_id=CONV, platform="whatsapp",
                       account_id="17345893506", chat_key="13308422244",
                       template="custom", autonomy="auto", deadline_days=3,
                       now=NOW - 3600)
    gs.update_goal_fields(g["goal_id"], status="done", done_at=NOW, progress=1.0)
    out = notify.settle_and_notify(gs, gs.get_goal(g["goal_id"]), cfg_root=CFG, now=NOW)
    assert out and out["reason"] == "done"
    assert bus.events == []
    # 人工标终态口径：notify=False 也只记摘要
    g2 = gs.create_goal(conversation_id="whatsapp:1:x", platform="whatsapp",
                        account_id="1", chat_key="x", template="custom",
                        autonomy="auto", deadline_days=3, now=NOW - 3600)
    gs.update_goal_fields(g2["goal_id"], status="failed", done_at=NOW)
    assert notify.settle_and_notify(gs, gs.get_goal(g2["goal_id"]), cfg_root=CFG,
                                    now=NOW, notify=False)
    assert bus.events == []
    assert notify.read_settlement(gs, g2["goal_id"])["status"] == "failed"


def test_settlement_detail_fits_event_column():
    gs = GoalStore(":memory:")
    gid = _expired_goal(gs, title="x" * 120)["goal_id"]
    for r in ("silence", "min_gap", "platform", "automation_mode", "crisis"):
        for d in range(4):
            record_beat_blocked(gs, gid, r, slot=f"d2026-09-0{d + 1}", now=NOW - 100)
    out = notify.settle_and_notify(gs, dict(gs.get_goal(gid), status="expired",
                                            done_at=NOW), cfg_root=CFG, now=NOW)
    raw = gs.last_event(gid, notify.SETTLEMENT_EVENT_KIND)["detail"]
    assert len(raw) <= 400 and json.loads(raw)["blocked"] == 20
    assert len(out["blocked_by"]) == 3                 # 只留前三种原因


# ── 3. 工作台通知中心登记（白名单 × i18n × 前端映射） ────────────────────────
def test_goal_settled_alert_registered_for_workspace():
    from src.web.routes.unified_inbox_realtime_routes import (
        _NOTIF_EVENT_TYPES, _NOTIF_TYPE_I18N, _SSE_EVENT_TYPES,
    )
    assert "goal_settled_alert" in _SSE_EVENT_TYPES
    assert "goal_settled_alert" in _NOTIF_EVENT_TYPES
    assert _NOTIF_TYPE_I18N["goal_settled_alert"] == (
        "base.notif.type_goal_settled", "base.notif.goal_settled_sub")
    from src.web.i18n_packs import goals as gp
    for k in ("base.notif.type_goal_settled", "base.notif.goal_settled_sub",
              "base.sse.goal_settled", "inbox.goal.settle.view",
              "inbox.goal.settle.reason.steered_only"):
        assert k in gp.ZH and k in gp.EN
    tpl = (REPO / "src/web/templates/workspace_base.html").read_text(encoding="utf-8")
    assert re.search(r"^\s*goal_settled_alert\s*:\s*\{", tpl, re.M)
    assert "msg.type==='goal_settled_alert'" in tpl          # toast + 点击跳会话
    assert "n.type==='goal_settled_alert'" in tpl            # 铃铛 sub + click


# ── 4. 卡片数据：last 带 settlement；新目标带 prev_settled ────────────────────
def test_card_source_and_frontend_wiring():
    routes = (REPO / "src/web/routes/goal_routes.py").read_text(encoding="utf-8")
    assert "def _attach_settlement(" in routes and "settled_recent" in routes
    assert '"prev_settled": prev_settled' in routes
    js = (REPO / "desktop/renderer/shared/copilot/components/cp-goal.js").read_text(encoding="utf-8")
    js2 = (REPO / "shared/copilot/components/cp-goal.js").read_text(encoding="utf-8")
    assert js == js2, "cp-goal.js 双树必须一致"
    for needle in ("_settlementHtml(", "_prevSettledHtml(", 'data-act="settle_toggle"',
                   "inbox.goal.settle.view", "inbox.goal.settle.prev",
                   "inbox.goal.settle.reason."):
        assert needle in js, needle
    # 三宿主缓存戳必须一致（具体值由 D 项测试钉住，随批次前移）
    stamps = {re.search(r"cp-goal\.js\?v=(\w+)", (REPO / host).read_text(encoding="utf-8")).group(1)
              for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html",
                           "src/web/templates/unified_inbox.html")}
    assert len(stamps) == 1, f"三宿主 cp-goal.js 缓存戳不一致：{stamps}"


def test_read_settlement_and_visible_window():
    gs = GoalStore(":memory:")
    gid = _expired_goal(gs)["goal_id"]
    assert notify.read_settlement(gs, gid) is None
    notify.settle_and_notify(gs, dict(gs.get_goal(gid), status="expired", done_at=NOW),
                             cfg_root=CFG, now=NOW, notify=False)
    s = notify.read_settlement(gs, gid)
    assert s and s["status"] == "expired" and float(s["at"]) > 0
    assert notify.SETTLEMENT_VISIBLE_SEC == 7 * 86400.0


# ══ M-7 D（#236）：卡片说真话——「自动推进中」显示条件 + 单目标 stalled ═══════════
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from src.companion.goals.store import get_goal_store, reset_goal_store  # noqa: E402


class _StubInbox:
    def __init__(self, mode="auto_ai"):
        self.mode = mode

    def get_automation_mode(self, conv):
        return self.mode

    def get_conv_meta(self, conv):
        return {}

    def list_recent_messages(self, conv, limit=10):
        return []


@pytest.fixture
def card(monkeypatch):
    """for-conversation 客户端：goals+sprint 开、白名单含 whatsapp、会话 auto_ai。"""
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    from src.web.routes.goal_routes import register_goal_routes

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    reset_goal_store()
    stub = {"inbox": _StubInbox()}
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: stub["inbox"])
    cfg = {"companion": {"goals": {
        "enabled": True, "db_path": ":memory:",
        "sprint": {"enabled": True, "dry_run": False,
                   "platforms": ["telegram", "whatsapp", "line"],
                   "natural_window": [0, 24]},
    }, "proactive_care": {"enabled": True, "dry_run": True}}}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": "", "user": "tester"}

    register_goal_routes(app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    c = TestClient(app)

    def _mk(created_ago_sec=0.0, autonomy="auto"):
        store = get_goal_store(":memory:")
        g = store.create_goal(
            conversation_id=CONV, platform="whatsapp", account_id="17345893506",
            chat_key="13308422244", template="custom", autonomy=autonomy,
            params={"note": "推进到愿意视频"}, deadline_days=5,
            now=NOW - created_ago_sec)
        return store, g["goal_id"]

    def _live():
        return c.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()["goal"]["sprint_live"]

    yield SimpleNamespace(client=c, mk=_mk, live=_live, stub=stub)
    reset_goal_store()


def test_card_new_goal_shows_waiting_first_not_auto_advancing(card):
    """新建 24h 内、零真发 → 不得显「自动推进中」：等待首拍（自然档下一拍 ≤24h）。"""
    card.mk()
    live = card.live()
    assert live["auto_state"] == "waiting_first"
    assert live["sent_24h"] == 0 and live["stalled"] is False
    assert live["beats_used"] == 0 and live["next_phase_ts"] > 0


def test_card_active_only_with_real_send_in_24h(card):
    store, gid = card.mk(created_ago_sec=2 * 86400)
    record_natural_beat_sent(store, gid, "2026-09-07", now=NOW - 3600, care_id=3)
    live = card.live()
    assert live["auto_state"] == "active"
    assert live["sent_24h"] == 1 and live["beats_used"] == 1 and live["stalled"] is False


def test_card_stalled_when_scheduled_but_zero_sends_24h(card):
    """BABY BEAR 形态：建了 2 天、引擎绿、会话全自动、零真发 → stalled 红字 + 上次被拦原因。"""
    store, gid = card.mk(created_ago_sec=2 * 86400)
    record_beat_blocked(store, gid, "silence", slot="d2026-09-07", now=NOW - 600)
    live = card.live()
    assert live["auto_state"] == "stalled" and live["stalled"] is True
    assert live["last_block"]["reason"] == "silence"
    assert live["blockers"] == []           # 引擎与运行时闸都绿，问题是没出手
    assert live["trace"]["today"]["blocked"] >= 1


def test_card_cap_reached_and_blocked_states(card):
    store, gid = card.mk(created_ago_sec=2 * 86400)
    record_beat_blocked(store, gid, "pace_cap", slot="2026-09-07T10", now=NOW - 60)
    assert card.live()["auto_state"] == "cap_reached"
    # 运行时闸没过（会话人审档）→ blocked，且 blockers 点名 automation_mode
    card.stub["inbox"] = _StubInbox("review")
    live = card.live()
    assert live["auto_state"] == "blocked" and live["blockers"] == ["automation_mode"]
    assert live["stalled"] is False         # 出不了手是闸的事，不算 stalled


def test_card_manual_goal_has_no_auto_claim(card):
    card.mk(autonomy="suggest")
    live = card.live()
    assert live["auto_state"] == "manual" and live["stalled"] is False


def test_card_frontend_states_and_watchdog_names_goal():
    js = (REPO / "desktop/renderer/shared/copilot/components/cp-goal.js").read_text(encoding="utf-8")
    for needle in ("inbox.goal.auto.active", "inbox.goal.auto.cap_reached",
                   "inbox.goal.auto.waiting_first", "inbox.goal.auto.stalled",
                   "inbox.goal.auto.blocked_recent", "inbox.goal.auto.tag_stalled_t",
                   'aSt === "stalled" ? "stalled"', "gl-status.stalled"):
        assert needle in js, needle
    assert 'this.t("inbox.goal.sprint.engine_wait")' not in js, "旧的空头承诺文案必须退场"
    from src.web.i18n_packs import goals as gp
    for k in ("inbox.goal.auto.active", "inbox.goal.auto.stalled", "inbox.goal.auto.cap_reached"):
        assert k in gp.ZH and k in gp.EN
    for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html",
                 "src/web/templates/unified_inbox.html"):
        # 三宿主戳一致（随批次前移：M-7 e → N-5 发版 20260908b → O-3 D c → O-3 E d → Q-1 E 20260910a
        # → Q-5 C b；unified_inbox.html 在 Q-5 时别线整文件在途，b 戳随宿主线前移，两戳皆认）
        assert re.search(r"cp-goal\.js\?v=20260910[ab]", (REPO / host).read_text(encoding="utf-8")), host
    # 「查看消息」对齐：组件回落宿主 __wsFocusConv；iframe 宿主 app.html 桥 postMessage（双树一致）
    assert "root.__wsFocusConv" in js
    for host in ("shared/copilot/app.html", "desktop/renderer/shared/copilot/app.html"):
        assert 'addEventListener("cp-goal-jump-message"' in (REPO / host).read_text(encoding="utf-8"), host


def test_watchdog_names_stalled_goal_throttles_and_recovers():
    from src.inbox import health_watchdog as hw

    class _Bus2:
        def __init__(self):
            self.events = []

        def publish(self, name, payload):
            self.events.append((name, payload))

    bus = _Bus2()
    import src.integrations.shared.event_bus as eb
    orig = eb.get_event_bus
    eb.get_event_bus = lambda: bus
    try:
        w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
        stalled = [{"goal_id": "g1", "conversation_id": CONV, "title": "推进到愿意视频"}]
        w._note_goal_stalled(stalled, now=NOW, interval_min=240)
        w._note_goal_stalled(stalled, now=NOW + 60, interval_min=240)      # 节流
        hits = [p for n, p in bus.events if n == "scan_loop_stall_alert"]
        assert len(hits) == 1
        assert hits[0]["loop"] == "goal_sprint_goal" and hits[0]["goal_id"] == "g1"
        assert hits[0]["conversation_id"] == CONV and hits[0]["rate_key"] == "scan_stall:goal:g1"
        w._note_goal_stalled(stalled, now=NOW + 241 * 60, interval_min=240)  # 过节流窗
        # 指纹未变 → unchanged 间隔（默认 24h）仍 HOLD；再拉到 25h 才重提
        assert len([p for n, p in bus.events if n == "scan_loop_stall_alert"]) == 1
        w._note_goal_stalled(stalled, now=NOW + 25 * 3600, interval_min=240)
        hits = [p for n, p in bus.events if n == "scan_loop_stall_alert"]
        assert len(hits) == 2 and hits[1]["reminder"] is True
        w._note_goal_stalled([], now=NOW + 26 * 3600, interval_min=240)       # 恢复清位
        assert "g1" not in w._goal_stalled_alerted
        rec = [p for n, p in bus.events
               if n == "scan_loop_stall_alert" and p.get("recovered")]
        assert rec and rec[0]["goal_id"] == "g1"
    finally:
        eb.get_event_bus = orig
