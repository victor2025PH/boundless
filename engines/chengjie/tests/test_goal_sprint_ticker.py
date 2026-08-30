# -*- coding: utf-8 -*-
"""冲刺推进器门禁（P0 2026-08-30）。

重点覆盖「不该出手」的路径：关闸/非冲刺/非 auto/平台外/人审会话/无 inbox
（fail-closed）/危机 block/opt-out/沉默闸/间隔闸/相位去重——主动发消息的
误发代价远高于漏发。真发链（care 派发豁免策略、冲刺拟稿模板）在
test_care_dispatcher 风格的最小派发器上验证。
"""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

from src.companion.goals.sprint_ticker import (
    DEFAULT_PHASE_POINTS,
    SprintGoalTicker,
    due_phase,
    parse_goal_care_norm,
    parse_sprint_cfg,
    phase_directive,
    phase_norm,
    record_sprint_beat_sent,
    remaining_phrase,
    sprint_source_text,
)
from src.companion.goals.store import GoalStore
from src.contacts.care_schedule import CareScheduleStore

NOW = time.time()


# ── 配置解析 ─────────────────────────────────────────────────────────────
def test_parse_cfg_defaults():
    cfg = parse_sprint_cfg({})
    assert cfg["enabled"] is False
    assert cfg["phase_points"] == DEFAULT_PHASE_POINTS
    assert cfg["exempt_contact_budget"] is True
    assert cfg["ignore_quiet_hours"] is False
    assert cfg["platforms"] == ("telegram",)
    assert cfg["jitter_sec"] == (30.0, 180.0)


def test_parse_cfg_bad_values_fall_back():
    cfg = parse_sprint_cfg({"sprint": {
        "enabled": True, "interval_sec": "abc", "phase_points": ["x", 2.0],
        "jitter_sec": ["a"], "platforms": [], "max_per_tick": -3,
    }})
    assert cfg["enabled"] is True
    assert cfg["interval_sec"] == 120.0          # 坏值回默认
    assert cfg["phase_points"] == DEFAULT_PHASE_POINTS  # 全越界回默认
    assert cfg["jitter_sec"] == (30.0, 180.0)
    assert cfg["platforms"] == ("telegram",)
    assert cfg["max_per_tick"] == 1              # 夹下限


def test_parse_cfg_phase_points_clamped_sorted():
    cfg = parse_sprint_cfg({"sprint": {
        "phase_points": [0.75, 0.0, 0.45, 1.5, 0.45]}})
    assert cfg["phase_points"] == (0.0, 0.45, 0.75)   # 去重排序、>0.95 剔除


# ── topic_norm 相位键 ────────────────────────────────────────────────────
def test_phase_norm_roundtrip():
    assert parse_goal_care_norm(phase_norm("abc123", 2)) == ("abc123", 2)
    assert parse_goal_care_norm("goal:abc123") == ("abc123", None)  # 旧格式
    assert parse_goal_care_norm("goal:") == ("", None)
    assert parse_goal_care_norm("其他:xx") == ("", None)
    assert parse_goal_care_norm(None) == ("", None)
    # goal_id 内不含 :p 时，p 后非数字不误拆
    assert parse_goal_care_norm("goal:abc:pxyz") == ("abc:pxyz", None)


def test_phase_directive_clamps_to_closing():
    assert phase_directive(0) != phase_directive(2)
    assert phase_directive(9, 3) == phase_directive(2, 3)   # 越界夹收口
    assert "答复" in phase_directive(2, 3)


def test_remaining_phrase_scales():
    assert remaining_phrase(30) == "不到1分钟"
    assert remaining_phrase(45 * 60) == "约45分钟"
    assert "小时" in remaining_phrase(3 * 3600)


def test_sprint_source_text_carries_title_and_phase():
    g = {"title": "拿到微信号", "deadline_ts": NOW + 3600}
    s = sprint_source_text(g, 2, NOW, 3)
    assert "拿到微信号" in s and "剩余" in s and len(s) <= 160


# ── due_phase 判定 ───────────────────────────────────────────────────────
def _goal(start_off=-600.0, span=3 * 3600.0):
    return {"start_ts": NOW + start_off, "deadline_ts": NOW + start_off + span}


_CFG = parse_sprint_cfg({"sprint": {"enabled": True}})


def test_due_phase_basic_progression():
    # 刚建 5% → p0；过 50% → p1；过 80% → p2
    assert due_phase(_goal(-540, 3 * 3600), cfg=_CFG, now=NOW) == 0
    assert due_phase(_goal(-5400, 3 * 3600), cfg=_CFG, now=NOW) == 1
    assert due_phase(_goal(-8640, 3 * 3600), cfg=_CFG, now=NOW) == 2


def test_due_phase_window_guards():
    assert due_phase({}, cfg=_CFG, now=NOW) is None
    assert due_phase({"start_ts": 0, "deadline_ts": NOW + 100},
                     cfg=_CFG, now=NOW) is None
    # 已到期 / 剩余不足 min_remaining（180s）
    assert due_phase(_goal(-7200, 7000), cfg=_CFG, now=NOW) is None
    assert due_phase(_goal(-3500, 3600), cfg=_CFG, now=NOW) is None


def test_due_phase_silence_gate():
    # 对方 5 分钟前刚开口（silence_min 600s）→ 让回复链接管
    g = _goal(-1800, 3 * 3600)
    assert due_phase(g, cfg=_CFG, now=NOW, last_inbound_ts=NOW - 300) is None
    assert due_phase(g, cfg=_CFG, now=NOW, last_inbound_ts=NOW - 700) == 0
    # 从没开过口（0）→ 放行
    assert due_phase(g, cfg=_CFG, now=NOW, last_inbound_ts=0.0) == 0


def test_due_phase_outbound_gap_gate():
    g = _goal(-1800, 3 * 3600)
    assert due_phase(g, cfg=_CFG, now=NOW, last_outbound_ts=NOW - 600) is None
    assert due_phase(g, cfg=_CFG, now=NOW, last_outbound_ts=NOW - 1600) == 0


# ── 真发回执 → 拍行 ──────────────────────────────────────────────────────
def test_record_sprint_beat_sent_writes_action_and_event():
    gs = GoalStore(":memory:")
    goal = gs.create_goal(
        conversation_id="telegram:a1:u1", platform="telegram",
        account_id="a1", chat_key="u1", template="custom", autonomy="auto",
        deadline_days=3 / 24.0, params={"pace": "today", "note": "拿到微信号"})
    gid = str(goal["goal_id"])
    assert record_sprint_beat_sent(gs, gid, 1, now=NOW)
    rows = gs.list_actions(gid, limit=10)
    assert len(rows) == 1
    a = rows[0]
    assert str(a["day"]).startswith("z:")
    assert a["status"] == "sent" and a["detail"] == "sprint:p1"
    assert a["push_level"] == "direct" and a["intent"]
    kinds = [e["kind"] for e in gs.list_events(gid, limit=10)]
    assert "beat_sent" in kinds
    # 不存在的目标 → False 不抛
    assert record_sprint_beat_sent(gs, "nope", 0, now=NOW) is False


# ── run_once 扫描 ────────────────────────────────────────────────────────
class _TickInbox:
    def __init__(self, mode="auto_ai", msgs=None, meta=None):
        self.mode = mode
        self.msgs = msgs if msgs is not None else []
        self.meta = meta or {}

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_meta(self, cid):
        return dict(self.meta)

    def list_recent_messages(self, cid, limit=30):
        return list(self.msgs)


def _mk_ticker(gs, cs, *, sprint_cfg=None, inbox=None, emotion_gate=None):
    scfg = {"enabled": True}
    if sprint_cfg:
        scfg.update(sprint_cfg)
    cfg = {"companion": {"goals": {"enabled": True, "sprint": scfg}}}
    return SprintGoalTicker(
        care_store=cs,
        config_obj=SimpleNamespace(config=cfg, config_path=None),
        goals_store=gs,
        inbox_store_getter=(lambda: inbox),
        emotion_gate=emotion_gate,
    )


def _sprint_goal(gs, *, autonomy="auto", platform="telegram", started_ago=1200.0,
                 span_h=3.0, chat="u1"):
    g = gs.create_goal(
        conversation_id=f"{platform}:a1:{chat}", platform=platform,
        account_id="a1", chat_key=chat, template="custom", autonomy=autonomy,
        deadline_days=span_h / 24.0,
        params={"pace": "today", "note": "拿到微信号"},
        now=NOW - started_ago)
    return g


def test_run_once_disabled_is_noop():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    _sprint_goal(gs)
    t = _mk_ticker(gs, cs, sprint_cfg={"enabled": False},
                   inbox=_TickInbox())
    out = t.run_once(now=NOW)
    assert out == {"enabled": False, "scanned": 0, "scheduled": 0, "skips": {}}
    assert cs.count(status="pending") == 0


def test_run_once_schedules_phase_and_dedups():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    g = _sprint_goal(gs)
    gid = str(g["goal_id"])
    # 客户 20 分钟前说过话（沉默闸放行），我方 40 分钟没出站
    inbox = _TickInbox(msgs=[
        {"direction": "in", "content": "嗯", "ts": NOW - 1200},
        {"direction": "out", "content": "好", "ts": NOW - 2400},
    ])
    t = _mk_ticker(gs, cs, inbox=inbox)
    out = t.run_once(now=NOW)
    assert out["scheduled"] == 1 and out["scanned"] == 1
    due = cs.list_due(now=NOW + 30)
    assert len(due) == 1
    assert due[0]["topic_norm"] == phase_norm(gid, 0)
    assert "限时推进" in str(due[0]["source_text"])
    kinds = [e["kind"] for e in gs.list_events(gid, limit=10)]
    assert "sprint_scheduled" in kinds
    # 同相位再扫 → add_scheduled_care 去重
    out2 = t.run_once(now=NOW)
    assert out2["scheduled"] == 0 and out2["skips"].get("dedup") == 1


def test_run_once_gates_autonomy_platform_mode():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    _sprint_goal(gs, autonomy="suggest", chat="u1")
    _sprint_goal(gs, platform="messenger", chat="u2")
    inbox = _TickInbox(mode="review")
    g3 = _sprint_goal(gs, chat="u3")
    t = _mk_ticker(gs, cs, inbox=inbox)
    out = t.run_once(now=NOW)
    assert out["scheduled"] == 0
    assert out["skips"].get("autonomy") == 1
    assert out["skips"].get("platform") == 1
    assert out["skips"].get("automation_mode") == 1
    assert cs.count(status="pending") == 0
    del g3


def test_run_once_no_inbox_fails_closed():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    _sprint_goal(gs)
    t = _mk_ticker(gs, cs, inbox=None)
    out = t.run_once(now=NOW)
    assert out["scheduled"] == 0 and out["skips"].get("no_inbox") == 1


def test_run_once_crisis_block_skips():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    _sprint_goal(gs)
    t = _mk_ticker(gs, cs, inbox=_TickInbox(),
                   emotion_gate=lambda goal, meta: "block")
    out = t.run_once(now=NOW)
    assert out["scheduled"] == 0 and out["skips"].get("crisis") == 1
    # soft（普通负面）放行
    t2 = _mk_ticker(gs, cs, inbox=_TickInbox(),
                    emotion_gate=lambda goal, meta: "soft")
    out2 = t2.run_once(now=NOW)
    assert out2["scheduled"] == 1


def test_run_once_ignores_natural_goals():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    gs.create_goal(
        conversation_id="telegram:a1:u9", platform="telegram", account_id="a1",
        chat_key="u9", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "长线"}, now=NOW - 1200)
    t = _mk_ticker(gs, cs, inbox=_TickInbox())
    out = t.run_once(now=NOW)
    assert out["scanned"] == 0 and out["scheduled"] == 0


def test_run_once_silence_gate_defers_to_reply_chain():
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    _sprint_goal(gs)
    inbox = _TickInbox(msgs=[
        {"direction": "in", "content": "在聊", "ts": NOW - 120}])
    t = _mk_ticker(gs, cs, inbox=inbox)
    out = t.run_once(now=NOW)
    assert out["scheduled"] == 0 and out["skips"].get("not_due") == 1


# ── care 派发侧：冲刺行豁免策略 + 冲刺拟稿模板 ───────────────────────────
class _AI:
    def __init__(self, reply="时间不多啦，要不要现在定下来？"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(record):
    async def _send(channel, account_id, chat_name, reply, defer_until,
                    reason, staleness, extra):
        record.append({"channel": channel, "reply": reply,
                       "defer_until": defer_until})
        return 321
    return _send


def _sprint_care_row(cs, *, phase=2, contact="telegram:a1:u1", due_off=-10.0):
    return cs.add_scheduled_care(
        contact_key=contact, platform="telegram", account_id="a1",
        chat_key="u1", due_at=NOW + due_off, event_at=NOW + 1800,
        topic="拿到微信号", topic_norm=f"goal:g1:p{phase}",
        source_text="限时推进「拿到微信号」剩余约30分钟", confidence=1.0)


async def test_dispatcher_budget_blocks_sprint_row_without_policy():
    from src.contacts.care_dispatcher import CareDispatcher
    cs = CareScheduleStore(":memory:")
    _sprint_care_row(cs)
    rec = []
    d = CareDispatcher(store=cs, ai_client=_AI(), send_callback=_sender(rec),
                       budget_gate=lambda ck: False)
    n = await d.run_once(now=NOW)
    assert n == 0 and not rec
    assert cs.count(status="skipped") == 1


async def test_dispatcher_policy_exempts_budget_and_quiet():
    from src.contacts.care_dispatcher import CareDispatcher
    # 23:30（安静窗内）：无策略会顺延到次日 8 点；豁免后按快抖动近发
    late = datetime(2026, 6, 17, 23, 30, 0).timestamp()
    cs = CareScheduleStore(":memory:")
    cs.add_scheduled_care(
        contact_key="telegram:a1:u1", platform="telegram", account_id="a1",
        chat_key="u1", due_at=late - 10, event_at=late + 1800,
        topic="拿到微信号", topic_norm="goal:g1:p2",
        source_text="限时推进", confidence=1.0)
    rec = []
    ai = _AI()
    d = CareDispatcher(
        store=cs, ai_client=ai, send_callback=_sender(rec),
        budget_gate=lambda ck: False,
        goal_row_policy=lambda item: {
            "exempt_budget": True, "ignore_quiet": True,
            "jitter": (1.0, 2.0)},
    )
    n = await d.run_once(now=late)
    assert n == 1 and len(rec) == 1
    # 快抖动 + 不顺延：defer 落在 1-2s 内，绝不是次日 8 点
    assert late + 0.5 <= rec[0]["defer_until"] <= late + 3.0
    # 冲刺拟稿模板：限时叙事 + 收口相位指令
    assert "限时推进" in ai.prompts[0]
    assert "答复" in ai.prompts[0]


async def test_dispatcher_legacy_goal_row_keeps_old_prompt():
    from src.contacts.care_dispatcher import CareDispatcher
    cs = CareScheduleStore(":memory:")
    cs.add_scheduled_care(
        contact_key="telegram:a1:u1", platform="telegram", account_id="a1",
        chat_key="u1", due_at=NOW - 10, event_at=NOW + 86400,
        topic="老目标", topic_norm="goal:g9",
        source_text="工作目标「老目标」剩余 2.5 天到期", confidence=1.0)
    rec = []
    ai = _AI()
    d = CareDispatcher(store=cs, ai_client=ai, send_callback=_sender(rec))
    n = await d.run_once(now=NOW)
    assert n == 1
    assert "正在推进的事" in ai.prompts[0]
    assert "限时推进" not in ai.prompts[0]


def test_build_care_prompt_sprint_variant_pure():
    from src.contacts.care_dispatcher import build_care_prompt
    item = {"topic": "拿到微信号", "topic_norm": "goal:g1:p0",
            "event_at": NOW + 3600, "source_text": "限时推进"}
    p = build_care_prompt(item, now=NOW)
    assert "限时推进" in p and "先看反应" in p and "剩余" in p


# ── P1 集成：refresh_goal 的收口升档 / 全力模式 ──────────────────────────
_CFG_ROOT = {"companion": {"goals": {"enabled": True}}}


def test_refresh_goal_escalates_to_close_near_deadline():
    from src.companion.goals.service import refresh_goal
    gs = GoalStore(":memory:")
    # 3h 目标已过 2.5h → 剩余 ~17% < 35% → 收口档 + 半小时槽 + 收口池意图
    g = _sprint_goal(gs, started_ago=2.5 * 3600, span_h=3.0)
    res = refresh_goal(gs, _CFG_ROOT, g, now=NOW)
    a = res.get("action")
    assert a is not None
    assert a["push_level"] == "close"
    day = str(a["day"])
    assert day.endswith("h0") or day.endswith("h1")   # 收口窗半小时槽
    assert "微信号" in str(a["intent"])                # 收口池代入 note


def test_refresh_goal_max_mode_first_beat_direct():
    from src.companion.goals.service import refresh_goal
    gs = GoalStore(":memory:")
    g = gs.create_goal(
        conversation_id="telegram:a1:u8", platform="telegram",
        account_id="a1", chat_key="u8", template="custom", autonomy="auto",
        deadline_days=3 / 24.0,
        params={"pace": "today", "note": "拿到微信号", "sprint_mode": "max"},
        now=NOW - 600)
    res = refresh_goal(gs, _CFG_ROOT, g, now=NOW)
    a = res.get("action")
    assert a is not None
    assert a["push_level"] == "direct"                # 默认档首拍是 soft


def test_refresh_goal_default_first_beat_soft():
    from src.companion.goals.service import refresh_goal
    gs = GoalStore(":memory:")
    g = _sprint_goal(gs, started_ago=600, span_h=3.0, chat="u7")
    res = refresh_goal(gs, _CFG_ROOT, g, now=NOW)
    a = res.get("action")
    assert a is not None and a["push_level"] == "soft"
