# -*- coding: utf-8 -*-
"""O-3 A（#236 族，HM7XBA / 5XRQ7C，2026-09-08）：目标摸底主动腿——「到期未发」根因回放。

取证结论（发版对账_v1.0.78_O3 §〇，五选一＝**没到期**）：
- 3 天「客户摸底」＝自然档；``due_daily`` 建目标当天（day 0）设计上不排、次日只在
  10–20 点窗口 → 14:45 建、02:10 报告，这段时间**一次都没到期**；watchdog 的全局
  ``stalled`` 在 4h/8h 就喊，是对自然档的误报（第一个可出手时刻还在 19h 以后）。
- 但顺着往后看：9/8 00:11 客户来消息 → 回复链把当日拍 ``consumed``（方向进了拟稿，
  模型却顺着聊天气没问坐标）→ ``due_daily`` 规则 3 看到 consumed 整天不出手 =
  **客户越活跃越永远不摸底**（指令嫌疑 ①）。

本文件钉：
1. HM7XBA 时间线回放：day 0 / 窗外 → not_due（不记 blocked、不喊 stalled）；
   全局 stall 判据按「首拍资格」起算，自然档 8h 不响、30h 响；
2. 摸底目标：当日拍 consumed 但**没问出来**（detail≠asked:*）→ 到点照样排主动拍；
   ``asked:<slot>`` → 让位；非摸底模板 consumed 仍让位（旧口径不动）；
3. 主动拍 source_text 钉进未填槽问法；
4. ``goal:{gid}:q{ts}`` 分型为 probe（坐席手动摸底），旧三型不变；
5. O-1 A 停联冻结 → 运行时闸 ``frozen`` 拦下（记 beat_blocked(frozen)）；
6. 逐目标 stalled 落 WARNING。
"""
from __future__ import annotations

import logging
import time

from types import SimpleNamespace

from src.companion.goals import liveness, service, sprint_ticker
from src.companion.goals.planner import (
    ACTIVE_INBOUND_MIN,
    customer_active,
    plan_beat,
    with_probe,
)
from src.companion.goals.service import (
    build_block_for_chat,
    discovery_probe_asks,
    merged_beat_intent,
    refresh_goal,
)
from src.companion.goals.signals import GoalSignals, inbound_count_since
from src.companion.goals.templates import (
    PROBE_CLAUSE_SEP,
    get_template,
    intent_en_for,
)
from src.companion.goals.liveness import (
    collect_send_liveness,
    first_eligible_delay_sec,
    stall_verdict,
)
from src.companion.goals.sprint_ticker import (
    SprintGoalTicker,
    conversation_frozen,
    due_daily,
    goal_runtime_gates,
    is_discovery_goal,
    natural_source_text,
    parse_goal_care_kind,
    parse_goal_care_norm,
    parse_sprint_cfg,
    probe_asked_in_action,
    probe_norm,
)
from src.companion.goals.store import GoalStore

CONV = "whatsapp:12137839654:19096186612"
PLAT, ACCT, CK = "whatsapp", "12137839654", "19096186612"


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


# HM7XBA：9/7 14:45 建 3 天摸底目标
T_CREATE = _local(2026, 9, 7, 14, 45)
CFG = parse_sprint_cfg({"sprint": {"enabled": True}})


def _discovery_goal(gs: GoalStore, *, now=T_CREATE, autonomy="auto"):
    return gs.create_goal(
        conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
        template="profile_discovery", autonomy=autonomy, deadline_days=3,
        params={"slots": "location,occupation,age,interests"}, now=now) or {}


# ── 1. HM7XBA 时间线：没到期 ──────────────────────────────────────────────────
def test_hm7xba_day0_and_pre_window_are_not_due():
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    # 18:42 / 22:43（watchdog 两次喊 stalled 的时刻）：day 0 → 不排
    for h, m in ((18, 42), (22, 43)):
        assert due_daily(g, cfg=CFG, now=_local(2026, 9, 7, h, m),
                         discovery=True) is None
    # 9/8 02:10 报告时刻：窗口（10–20）之外 → 不排
    assert due_daily(g, cfg=CFG, now=_local(2026, 9, 8, 2, 10), discovery=True) is None
    # 9/8 10:00 窗口开、客户 6h 没开口 → 第一次真到期
    assert due_daily(g, cfg=CFG, now=_local(2026, 9, 8, 10, 5),
                     last_inbound_ts=_local(2026, 9, 8, 2, 1),
                     discovery=True) == "2026-09-08"


def test_global_stall_alarm_counts_from_first_eligible_slot():
    """自然档 8h 不响（HM7XBA 的 auto=3 sent_24h=0 oldest_h=8.0 是误报）；30h 响。"""
    gs = GoalStore(":memory:")
    now = T_CREATE + 8 * 3600
    for ck in ("a", "b", "c"):
        gs.create_goal(conversation_id=f"whatsapp:x:{ck}", platform="whatsapp",
                       account_id="x", chat_key=ck, template="profile_discovery",
                       autonomy="auto", deadline_days=3, now=T_CREATE)
    snap = collect_send_liveness(gs, now=now)
    assert snap["active_auto"] == 3 and snap["sent_24h"] == 0
    assert snap["oldest_age_sec"] >= 7.9 * 3600          # 原始年龄照给（日志 oldest_h）
    assert snap["oldest_eligible_sec"] == 0.0            # 但一个都还没到能出手的时刻
    eng = {"sprint_effective": True}
    assert stall_verdict(eng, snap, min_active=2, min_age_sec=4 * 3600) is None
    snap30 = collect_send_liveness(gs, now=T_CREATE + 30 * 3600)
    assert snap30["oldest_eligible_sec"] >= 5.9 * 3600
    assert stall_verdict(eng, snap30, min_active=2, min_age_sec=4 * 3600) == "stalled"
    # 旧快照（无 eligible 键）回落旧口径
    assert stall_verdict(eng, {"active_auto": 2, "sent_24h": 0,
                               "oldest_age_sec": 5 * 3600}) == "stalled"


def test_first_eligible_delay_sprint_zero_natural_day():
    assert first_eligible_delay_sec({"template": "profile_discovery",
                                     "params": {}}) == liveness.GOAL_STALL_MIN_AGE_SEC
    assert first_eligible_delay_sec({"template": "custom",
                                     "params": {"pace": "today"}}) == 0.0


def test_stalled_goal_logs_warning(caplog):
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    with caplog.at_level(logging.WARNING, logger="src.companion.goals.liveness"):
        snap = collect_send_liveness(gs, now=T_CREATE + 30 * 3600)
    assert [s["goal_id"] for s in snap["stalled_goals"]] == [g["goal_id"]]
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "[goal-liveness] stalled" in r.getMessage()]
    assert len(warns) == 1
    assert f"conv={CONV}" in warns[0].getMessage()


# ── 2. 摸底目标：consumed ≠ asked ──────────────────────────────────────────────
def test_discovery_consumed_without_ask_does_not_yield_daily_beat():
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    assert is_discovery_goal(g)
    t = _local(2026, 9, 8, 11, 0)
    quiet = _local(2026, 9, 8, 2, 1)     # 客户 02:01 后没开口（6h 沉默闸过）
    consumed = {"status": "consumed", "detail": "reply"}
    asked = {"status": "consumed", "detail": "asked:location"}
    sent = {"status": "sent", "detail": "daily:auto"}
    # 摸底目标：consumed 但没问出来 → 照样到期
    assert due_daily(g, cfg=CFG, now=t, last_inbound_ts=quiet,
                     today_action=consumed, discovery=True) == "2026-09-08"
    # 真问出来了 → 今天让位
    assert due_daily(g, cfg=CFG, now=t, last_inbound_ts=quiet,
                     today_action=asked, discovery=True) is None
    # 今天已主动发过 → 让位
    assert due_daily(g, cfg=CFG, now=t, last_inbound_ts=quiet,
                     today_action=sent, discovery=True) is None
    # 非摸底模板：consumed 仍让位（旧口径逐字不变）
    assert due_daily(g, cfg=CFG, now=t, last_inbound_ts=quiet,
                     today_action=consumed, discovery=False) is None
    assert due_daily(g, cfg=CFG, now=t, last_inbound_ts=quiet,
                     today_action=consumed) is None
    assert probe_asked_in_action(asked) and not probe_asked_in_action(consumed)
    assert not probe_asked_in_action(None)


class _Care:
    def __init__(self):
        self.rows = []

    def add_scheduled_care(self, **kw):
        self.rows.append(kw)
        return len(self.rows)


class _Inbox:
    def __init__(self, *, mode="auto_ai", last_in=0.0, meta=None):
        self.mode, self.last_in, self.meta = mode, last_in, meta or {}

    def get_automation_mode(self, conv):
        return self.mode

    def get_conv_meta(self, conv):
        return dict(self.meta)

    def list_recent_messages(self, conv, limit=10):
        return [{"direction": "in", "ts": self.last_in}] if self.last_in else []


def _ticker(gs, care, inbox):
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:",
                                   "sprint": {"enabled": True}}}}
    return SprintGoalTicker(care_store=care, config_obj=cfg, goals_store=gs,
                            inbox_store_getter=lambda: inbox,
                            emotion_gate=lambda g, m: "")


def test_ticker_schedules_probe_for_consumed_but_unasked_discovery_goal():
    """端到端：回复链 00:11 consumed 了当日拍（没问）→ 11:00 客户已 6h 没开口 →
    ticker 排入主动拍，source_text 钉「人在哪个城市」。"""
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    gid = g["goal_id"]
    row = gs.upsert_action(gid, "2026-09-08", intent="先把互动热起来",
                           push_level="soft", now=_local(2026, 9, 8, 0, 11))
    gs.mark_action(row["action_id"], "consumed", detail="reply")
    care = _Care()
    tk = _ticker(gs, care, _Inbox(last_in=_local(2026, 9, 8, 2, 1)))
    out = tk.run_once(now=_local(2026, 9, 8, 11, 0))
    assert out["scheduled"] == 1, out
    r = care.rows[0]
    assert r["topic_norm"] == f"goal:{gid}:d20260908"
    assert "人在哪个城市" in r["source_text"]
    assert "一轮只问一个" in r["source_text"]
    # 已真问出来（asked:）→ 同一时刻不排
    gs.mark_action(row["action_id"], "consumed", detail="asked:location")
    care2 = _Care()
    tk2 = _ticker(gs, care2, _Inbox(last_in=_local(2026, 9, 8, 2, 1)))
    out2 = tk2.run_once(now=_local(2026, 9, 8, 11, 0))
    assert out2["scheduled"] == 0 and out2["skips"].get("not_due") == 1


def test_ticker_keeps_silence_gate_for_discovery():
    """客户 30 分钟前还在说话 → 沉默闸仍拦（不打断正聊着的对话，交给 C 段硬注入）。"""
    gs = GoalStore(":memory:")
    _discovery_goal(gs)
    care = _Care()
    t = _local(2026, 9, 8, 11, 0)
    tk = _ticker(gs, care, _Inbox(last_in=t - 1800))
    out = tk.run_once(now=t)
    assert out["scheduled"] == 0 and out["skips"].get("not_due") == 1
    gid = gs.list_goals(status="active")[0]["goal_id"]
    blocked = gs.list_events(gid, kinds=("beat_blocked",))
    assert blocked and blocked[0]["detail"] == "silence@d2026-09-08"


# ── 3. 主动拍 source_text ──────────────────────────────────────────────────────
def test_natural_source_text_pins_probe_hint():
    g = {"title": "客户摸底"}
    s = natural_source_text(g, None, probe_hint="人在哪个城市")
    assert "自然问一句：人在哪个城市" in s and "一轮只问一个" in s
    s2 = natural_source_text(g, {"intent": "顺着话题聊"}, probe_hint="做什么生意/工作")
    assert s2.startswith("日常推进「客户摸底」；本拍：顺着话题聊；自然问一句：做什么生意/工作")
    # 意图里已含该问法 → 不重复拼
    s3 = natural_source_text(g, {"intent": "顺势问问人在哪个城市"}, probe_hint="人在哪个城市")
    assert s3.count("人在哪个城市") == 1
    # 无提示 → 旧文案不变
    assert natural_source_text(g, None) == "日常推进「客户摸底」；本拍：自然接住对方近况，把这件事轻轻带进来一步，先看反应"


# ── 4. probe 行分型 ────────────────────────────────────────────────────────────
def test_probe_norm_parses_and_old_kinds_unchanged():
    ts = 1788000000
    tn = probe_norm("abc123", ts)
    assert tn == f"goal:abc123:q{ts}"
    assert parse_goal_care_kind(tn) == ("probe", "abc123", ts)
    assert parse_goal_care_norm(tn) == ("abc123", None)     # 派发器走普通目标行 prompt
    assert parse_goal_care_kind("goal:abc123:p2") == ("sprint", "abc123", 2)
    assert parse_goal_care_kind("goal:abc123:d20260908") == ("daily", "abc123", "2026-09-08")
    assert parse_goal_care_kind("goal:abc123") == ("care", "abc123", None)
    assert parse_goal_care_kind("birthday") == ("", "", None)


# ── 5. O-1 A 冻结闸 ────────────────────────────────────────────────────────────
def test_frozen_conversation_blocks_runtime_gates_and_records_block():
    now = _local(2026, 9, 8, 11, 0)
    assert conversation_frozen({"stop_contact": True}, now=now)
    assert conversation_frozen({"freeze_until": now + 60}, now=now)
    assert not conversation_frozen({"freeze_until": now - 60}, now=now)
    assert not conversation_frozen({"stop_contact": False, "frozen": 0}, now=now)
    assert not conversation_frozen(None) and not conversation_frozen({})
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    rg = goal_runtime_gates(g, cfg=CFG, inbox=_Inbox(meta={"stop_contact": True}),
                            now=now, stop_on_fail=False)
    assert rg["gates"]["frozen"] is False and rg["skip"] == "frozen"
    assert "frozen" in sprint_ticker.RUNTIME_GATE_ORDER
    assert "frozen" in sprint_ticker.BLOCKED_REASONS
    care = _Care()
    tk = _ticker(gs, care, _Inbox(last_in=now - 8 * 3600, meta={"stop_contact": True}))
    out = tk.run_once(now=now)
    assert out["scheduled"] == 0 and out["skips"].get("frozen") == 1
    ev = gs.list_events(g["goal_id"], kinds=("beat_blocked",))
    assert ev and ev[0]["detail"] == "frozen@d2026-09-08"


# ══ B 第 1 天就摸 + 客户活跃自适应 ═══════════════════════════════════════════════
TPL = get_template("profile_discovery")
ASKS = ["人在哪个城市", "做什么生意/工作", "大概哪个年龄段", "平时喜欢做什么"]


def _cfg_obj():
    return SimpleNamespace(
        config={"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}},
        config_path=None)


def _sig(**kw):
    return GoalSignals(now=T_CREATE, **kw)


def test_plan_day1_discovery_carries_one_probe():
    """第 1 天（里程碑 0）计划就含 ≥1 个摸底问句，围绕第一个未填槽。"""
    g = {"goal_id": "g1", "milestone_idx": 0, "template": "profile_discovery",
         "params": {"slots": "location,occupation,age,interests"}}
    beat = plan_beat(template=TPL, goal=g, signals=_sig(), day="2026-09-07",
                     probe_asks=ASKS)
    assert beat["probes"] == 1 and beat["probe_slots"] == ["人在哪个城市"]
    assert beat["intent"].endswith(f"{PROBE_CLAUSE_SEP}人在哪个城市")
    assert "不急着问" not in beat["intent"]          # 旧暖场文案退场
    assert beat["push_level"] == "soft" and beat.get("advanced") is False
    # 英文展示态能反查（基础意图 + 槽位问法两段都译）
    en = intent_en_for(TPL, g["params"], beat["intent"])
    assert en and en.endswith("; ask at least one thing naturally today: which city they are in")
    # 非摸底模板 / 无缺口：零变化
    cust = plan_beat(template=get_template("custom"), goal={**g, "template": "custom"},
                     signals=_sig(), day="2026-09-07", probe_asks=ASKS)
    assert set(cust) == {"intent", "push_level"}          # 非摸底模板形状逐字不变
    assert PROBE_CLAUSE_SEP not in cust["intent"]
    full = plan_beat(template=TPL, goal=g, signals=_sig(), day="2026-09-07", probe_asks=[])
    assert full["probes"] == 0 and PROBE_CLAUSE_SEP not in full["intent"]


def test_plan_active_customer_advances_warmup_to_collect():
    """客户活跃（30min ≥5 条）→ 里程碑 0 的暖场意图提前换成里程碑 1「顺着话头带出」。"""
    g = {"goal_id": "g1", "milestone_idx": 0, "template": "profile_discovery",
         "params": {"slots": "location"}}
    quiet = plan_beat(template=TPL, goal=g, signals=_sig(), day="2026-09-07",
                      probe_asks=["人在哪个城市"], active=False)
    busy = plan_beat(template=TPL, goal=g, signals=_sig(), day="2026-09-07",
                     probe_asks=["人在哪个城市"], active=True)
    assert busy["advanced"] is True and quiet["advanced"] is False
    ms1_pool = [zh for zh, _en in TPL["intents"][1]]
    assert busy["intent"].split(PROBE_CLAUSE_SEP)[0] in ms1_pool
    assert quiet["intent"].split(PROBE_CLAUSE_SEP)[0] in [zh for zh, _ in TPL["intents"][0]]
    assert busy["push_level"] == "soft"            # 力度仍按真实里程碑 0
    assert customer_active(ACTIVE_INBOUND_MIN) and not customer_active(ACTIVE_INBOUND_MIN - 1)
    assert not customer_active(None)


def test_plan_safety_beats_probe_floor():
    """退避陪伴日 / 情绪 hold 优先于「每天 ≥1 问」——安全语义不让 KPI 覆盖。"""
    g = {"goal_id": "g1", "milestone_idx": 0, "template": "profile_discovery",
         "params": {"slots": "location"}}
    care = plan_beat(template=TPL, goal=g, signals=_sig(), day="d", probe_asks=ASKS,
                     engaged_since_inbound=2)
    assert care["push_level"] == "none" and care["probes"] == 0
    hold = plan_beat(template=TPL, goal=g, signals=_sig(negative_emotion=True),
                     day="d", probe_asks=ASKS)
    assert hold == {"hold": "emotion"}
    assert with_probe("聊聊", "人在哪个城市") == f"聊聊{PROBE_CLAUSE_SEP}人在哪个城市"
    assert with_probe("顺口问问人在哪个城市", "人在哪个城市") == "顺口问问人在哪个城市"
    assert with_probe("", "人在哪个城市") == "今天至少自然问一个：人在哪个城市"
    # 注入链合流：先剥计划子句，再按本轮缺口带一个问法（一轮只带一个；缺口换了跟着换）
    merged = merged_beat_intent(f"x{PROBE_CLAUSE_SEP}人在哪个城市", "人在哪个城市")
    assert merged == "x；本轮顺势了解：人在哪个城市（像朋友闲聊，不要像查户口，一轮只问一个）"
    rotated = merged_beat_intent(f"x{PROBE_CLAUSE_SEP}人在哪个城市", "做什么生意/工作")
    assert "人在哪个城市" not in rotated and "本轮顺势了解：做什么生意/工作" in rotated
    assert merged_beat_intent(f"x{PROBE_CLAUSE_SEP}人在哪个城市", "") == "x"   # 全填 → 剥掉过期子句


class _InboxMsgs:
    def __init__(self, msgs):
        self.msgs = msgs

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)

    def get_conversation(self, conv):
        return {"last_ts": max((m["ts"] for m in self.msgs), default=0)}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"


def test_refresh_goal_day1_plan_has_probe_and_logs(caplog):
    """端到端：HM7XBA 14:45 建目标 → 当天第一稿 refresh_goal 排出的当日拍含问句，
    ``[goal-plan] day=1 probes=1 slots=['人在哪个城市']``；00:11–00:43 九条入站 → active=True。"""
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    cfg = _cfg_obj().config
    with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
        res = refresh_goal(gs, cfg, g, now=T_CREATE + 600, inbound_turn=True)
    act = res["action"]
    assert act and PROBE_CLAUSE_SEP in act["intent"] and "人在哪个城市" in act["intent"]
    plan_logs = [r.getMessage() for r in caplog.records if "[goal-plan]" in r.getMessage()]
    assert plan_logs and "day=1 probes=1 slots=['人在哪个城市'] active=False" in plan_logs[0]
    # 客户 30 分钟 9 条 → active=True，暖场段提前
    t = _local(2026, 9, 8, 0, 43)
    msgs = [{"direction": "in", "ts": t - 60 * i, "text": f"m{i}"} for i in range(9)]
    assert inbound_count_since(_InboxMsgs(msgs), CONV, t - 1800) == 9
    g2 = gs.create_goal(conversation_id="whatsapp:x:y", platform="whatsapp", account_id="x",
                        chat_key="y", template="profile_discovery", autonomy="auto",
                        deadline_days=3, params={"slots": "location"}, now=T_CREATE)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
        res2 = refresh_goal(gs, cfg, g2, inbox_store=_InboxMsgs(msgs), now=t, inbound_turn=True)
    assert res2["action"] and "人在哪个城市" in res2["action"]["intent"]
    logs2 = [r.getMessage() for r in caplog.records if "[goal-plan]" in r.getMessage()]
    assert logs2 and "active=True inbound_30m=9" in logs2[0]
    # 结果口径：暖场段文案不再出现——要么 ledger 因对方开口已把里程碑推到 1，
    # 要么 planner 因活跃提前用里程碑 1 池；两条路都落在「顺着话头带出」上
    base = res2["action"]["intent"].split(PROBE_CLAUSE_SEP)[0]
    assert base in [zh for zh, _ in TPL["intents"][1]] or int(
        res2["goal"].get("milestone_idx") or 0) >= 1


def test_discovery_probe_asks_orders_unfilled_and_skips_filled():
    gs = GoalStore(":memory:")
    g = _discovery_goal(gs)
    assert discovery_probe_asks(gs, TPL, g) == ASKS
    gs.upsert_customer_profile(PLAT, CK, {"location": "Manila"}, source="agent")
    g = gs.get_goal(g["goal_id"])
    assert discovery_probe_asks(gs, TPL, g) == ASKS[1:]
    assert discovery_probe_asks(gs, get_template("custom"), g) == []


def test_inject_block_day1_carries_probe_once():
    """第 1 天第一稿注入块：今天还没真问过 → C 段「每日下限」硬行【本轮必问】接管，
    计划子句被剥、软缺口行让位——块里同一问法只出现一次；旧「不急着问」退场。"""
    service._inject_log_seen.clear()
    gs = GoalStore(":memory:")
    from src.companion.goals.store import reset_goal_store, get_goal_store
    reset_goal_store()
    store = get_goal_store(":memory:")
    _discovery_goal(store)
    try:
        blk = build_block_for_chat(
            _cfg_obj(), platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
            user_context={}, chain="draft", inbound_text="hi there", now=T_CREATE + 60)
        assert blk and blk.count("人在哪个城市") == 1
        assert "【本轮必问】今天还没问过：人在哪个城市" in blk
        assert "【画像缺口】" not in blk and "本轮顺势了解" not in blk
        assert "不急着问" not in blk and PROBE_CLAUSE_SEP not in blk
        # 卡片 today.intent（计划层）仍带「今天至少自然问一个：坐标」
        act = store.get_action(store.list_goals(status="active")[0]["goal_id"],
                               time.strftime("%Y-%m-%d", time.localtime(T_CREATE + 60)))
        assert act and f"{PROBE_CLAUSE_SEP}人在哪个城市" in act["intent"]
    finally:
        reset_goal_store()


# ══ C 注入变硬：线索词 → 槽位 / 【本轮必问】 / 出站校验 / 下轮重试 ═══════════════
from src.companion.goals.profile_slots import (  # noqa: E402
    detect_cues,
    has_question,
    pick_probe_target,
    probe_hard_line,
    reply_asks_slot,
)
from src.companion.goals.service import (  # noqa: E402
    INJECT_COUNT_PARAM,
    PROBE_PENDING_PARAM,
    build_beats_trace,
    verify_pending_probe,
)


def test_detect_cues_maps_weather_time_work_family():
    assert detect_cues("it rained here yesterday, so lazy") == [("location", "rain")]
    assert detect_cues("昨天这里下雨了") == [("location", "下雨")]
    assert detect_cues("it's morning here, heading to work") == [
        ("location", "morning here"), ("occupation", "work")]
    assert detect_cues("my mom is visiting this weekend")[0][0] in ("interests", "family_status")
    assert ("family_status", "mom") in detect_cues("my mom is visiting this weekend")
    # 拉丁词按词边界：train 不是 rain、message 不是 age
    assert detect_cues("took the train, got your message") == []
    assert detect_cues("") == [] and detect_cues("hello") == []
    # 只在给定槽里找
    assert detect_cues("it's raining and I'm at work", slots=["occupation"]) == [("occupation", "work")]


def test_reply_asks_slot_requires_question_and_keyword():
    assert has_question("Which city are you in?") and has_question("你那边是哪个城市呀")
    assert not has_question("Sounds cozy, enjoy the rain.")
    assert reply_asks_slot("Rainy days are the best. Which city are you in, by the way?", "location")
    assert reply_asks_slot("下雨天最适合窝着了～你那边是哪个城市呀", "location")
    # 有问号没问到坐标 → 不算
    assert not reply_asks_slot("Rain again? Hope you stayed dry!", "location")
    # 问到坐标但不是问句 → 不算
    assert not reply_asks_slot("I love which city you're in.", "location")
    assert reply_asks_slot("What do you do for a living?", "occupation")
    assert reply_asks_slot("你平时喜欢做什么呢", "interests")
    assert not reply_asks_slot("", "location")


def test_pick_probe_target_priority_retry_cue_floor():
    un = ["location", "occupation", "age"]
    assert pick_probe_target(cues=[("occupation", "work")], unfilled=un, asked_today=False,
                             retry_slot="location") == ("location", "", "retry")
    assert pick_probe_target(cues=[("occupation", "work")], unfilled=un, asked_today=True) == (
        "occupation", "work", "cue")
    assert pick_probe_target(cues=[], unfilled=un, asked_today=False) == ("location", "", "floor")
    assert pick_probe_target(cues=[], unfilled=un, asked_today=True) == ("", "", "")     # 不连环追问
    assert pick_probe_target(cues=[("interests", "gym")], unfilled=un, asked_today=True) == ("", "", "")
    assert pick_probe_target(cues=[("location", "rain")], unfilled=[], asked_today=False) == ("", "", "")
    line = probe_hard_line("location", "下雨")
    assert line.startswith("【本轮必问】") and "下雨" in line and "人在哪个城市" in line
    assert "一轮只问一个" in line and "必须问出来" in line
    assert "今天还没问过" in probe_hard_line("occupation")


class _ConvInbox:
    """可追加出站消息的收件箱替身（校验链读 direction=out & ts）。"""

    def __init__(self):
        self.msgs = []

    def add(self, direction, text, ts):
        self.msgs.append({"direction": direction, "text": text, "ts": ts})

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)[-limit:]

    def get_conversation(self, conv):
        return {"last_ts": max((m["ts"] for m in self.msgs), default=0)}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"


def _fresh_store():
    from src.companion.goals.store import get_goal_store, reset_goal_store
    reset_goal_store()
    service._inject_log_seen.clear()
    return get_goal_store(":memory:")


def _inject(store_cfg, inbox, text, now, chain="draft"):
    return build_block_for_chat(
        store_cfg, platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
        user_context={}, chain=chain, inbound_text=text, inbox_store=inbox, now=now)


def test_hm7xba_replay_nine_inbound_at_least_two_location_probes(caplog):
    """回放 00:11–00:43 九条入站（客户透露时差 / 昨天下雨）：至少 2 条注入块带坐标硬追问；
    模型没问（出站无问句）→ probe_missed + 下轮同槽重试；问了 → probe_asked + 当日拍 asked:。"""
    from src.companion.goals.store import reset_goal_store
    store = _fresh_store()
    _discovery_goal(store)
    cfg = _cfg_obj()
    inbox = _ConvInbox()
    base = _local(2026, 9, 8, 0, 11)
    inbound = [
        "good morning! it's early morning here",          # 时差线索 → location
        "just woke up, coffee first",
        "haha yes",
        "it rained here yesterday, everything is wet",    # 天气线索 → location
        "ok",
        "what about you",
        "nice",
        "i like that",
        "talk later",
    ]
    # 模型每次都顺着聊、不问（复刻 HM7XBA 九条零问句）
    ai_replies = [
        "Early bird! Enjoy your coffee.", "Coffee is life haha.", "Right?",
        "Rainy days are cozy though.", "Yep.", "I'm good, just chilling.",
        "Glad you like it.", "Same here.", "Talk soon!",
    ]
    hard = 0
    try:
        with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
            for i, (txt, reply) in enumerate(zip(inbound, ai_replies)):
                t = base + i * 240
                inbox.add("in", txt, t)
                blk = _inject(cfg, inbox, txt, t)
                assert blk
                if "【本轮必问】" in blk:
                    hard += 1
                    assert blk.count("人在哪个城市") == 1
                    assert "【画像缺口】" not in blk and PROBE_CLAUSE_SEP not in blk
                inbox.add("out", reply, t + 30)
        assert hard >= 2, hard
        gid = store.list_goals(status="active")[0]["goal_id"]
        g = store.get_goal(gid)
        # 每次硬注入下一轮都校验出「没问」→ missed 事件；同槽重试
        missed = store.list_events(gid, kinds=("probe_missed",))
        assert len(missed) >= 2 and all(e["detail"].startswith("location@") for e in missed)
        assert not store.list_events(gid, kinds=("probe_asked",))
        msgs = [r.getMessage() for r in caplog.records]
        assert any("[goal-inject] target=location cue=morning here mode=cue result=pending" in m
                   for m in msgs)
        assert any("[goal-inject] target=location cue=morning here result=missed" in m for m in msgs)
        assert any(" mode=retry result=pending" in m for m in msgs)
        assert int((g.get("params") or {}).get(INJECT_COUNT_PARAM) or 0) == 9
        tr = build_beats_trace(store, g, now=base + 3600)
        assert tr["summary"]["probe_missed"] >= 2 and tr["summary"]["probe_asked"] == 0
        assert [b for b in tr["beats"] if b["kind"] == "probe"][0]["status"] == "missed"
        # 第 10 轮：模型终于问了 → asked + 当日拍 detail=asked:location（A 段让位钥匙）+ 今天不再硬追
        t10 = base + 9 * 240
        inbox.add("in", "you still there?", t10)
        blk10 = _inject(cfg, inbox, "you still there?", t10)
        assert "【本轮必问】" in blk10
        inbox.add("out", "Here! Btw which city are you in? Sounds like a rainy one 🌧", t10 + 30)
        t11 = t10 + 240
        inbox.add("in", "los angeles", t11)
        with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
            blk11 = _inject(cfg, inbox, "los angeles", t11)
        asked = store.list_events(gid, kinds=("probe_asked",))
        assert len(asked) == 1 and asked[0]["detail"] == "location@-"
        assert any("[goal-inject] target=location cue=- result=asked" in r.getMessage()
                   for r in caplog.records)
        from src.companion.goals.planner import day_key
        row = store.get_action(gid, day_key(t11))
        assert row and row["detail"] == "asked:location" and row["status"] == "consumed"
        assert PROBE_PENDING_PARAM not in (store.get_goal(gid).get("params") or {})
        # 今天已真问过一次且本轮无新线索 → 不再硬追（软合流照旧带一个缺口，不连环追问）
        assert blk11 and "【本轮必问】" not in blk11
    finally:
        reset_goal_store()


def test_verify_pending_probe_states():
    store = GoalStore(":memory:")
    g = _discovery_goal(store)
    gid = g["goal_id"]
    t0 = _local(2026, 9, 8, 0, 11)
    store.update_goal_fields(gid, params={**g["params"], PROBE_PENDING_PARAM: {
        "slot": "location", "cue": "rain", "ts": t0, "mode": "cue"}})
    g = store.get_goal(gid)
    # 无 inbox → pending 保留
    assert verify_pending_probe(store, g, inbox_store=None, now=t0 + 60)["result"] == "pending"
    inbox = _ConvInbox()
    inbox.add("out", "Before pending", t0 - 10)           # 早于挂起时刻的出站不算
    assert verify_pending_probe(store, g, inbox_store=inbox, now=t0 + 60)["result"] == "pending"
    inbox.add("out", "Cozy!", t0 + 20)
    inbox.add("out", "Which city are you in?", t0 + 25)   # 拆成两条气泡也要合并判
    v = verify_pending_probe(store, g, inbox_store=inbox, now=t0 + 60)
    assert v["result"] == "asked" and v["slot"] == "location" and v["cue"] == "rain"
    assert PROBE_PENDING_PARAM not in v["patch"]
    ev = store.list_events(gid, kinds=("probe_asked",))
    assert ev and ev[0]["text_head"].startswith("Cozy! Which city")
    # 超 24h → expired 静默丢弃、不记事件
    store.update_goal_fields(gid, params={**g["params"], PROBE_PENDING_PARAM: {
        "slot": "occupation", "cue": "", "ts": t0 - 90000}})
    v2 = verify_pending_probe(store, store.get_goal(gid), inbox_store=inbox, now=t0)
    assert v2["result"] == "expired" and PROBE_PENDING_PARAM not in v2["patch"]
    assert not store.list_events(gid, kinds=("probe_missed",))
    # 无挂起 → none
    assert verify_pending_probe(store, {"params": {}}, inbox_store=inbox, now=t0)["result"] == "none"


def test_hard_line_survives_overflow_and_soft_gap_yields():
    from src.companion.goals.context_block import build_goal_block
    line = probe_hard_line("location", "下雨")
    blk = build_goal_block(
        title="客户摸底", milestone_label="破冰起步", milestone_idx=0, day_index=1,
        total_days=3, intent="先把互动热起来" * 30, push_level="soft",
        profile_gap="人在哪个城市", profile_facts="称呼:Martin" * 10,
        context_note="备注" * 60, probe_line=line, max_chars=360)
    assert line in blk                                    # 超长也不丢
    assert "【画像缺口】" not in blk                        # 有硬行时软行让位
    assert "【推进纪律】" in blk
    blk2 = build_goal_block(title="客户摸底", milestone_label="x", milestone_idx=0, day_index=1,
                            total_days=3, intent="聊", profile_gap="人在哪个城市")
    assert "【画像缺口】像朋友闲聊，不要像查户口，一轮只问一个：人在哪个城市" in blk2
