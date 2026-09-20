# -*- coding: utf-8 -*-
"""Q-1（#264 #269 #270 K24YJ2 / FCQFKF / T9KN8X，2026-09-09）：目标引擎「已知即不问」。

事故：客户 23:46 说了「it's a job… convention center / union carpenter」，AI 自己也复述过，
引擎仍按「画像字段为空」把 occupation 当没问 → 十小时 asked 8 次、注入 13 次，客户
「I told you what I do for work like 50 times… Like I am talking to a wall」→ Goodbye。

本文件钉（A / B 段）：
1. 槽位三态 ``slot_state``：字段（三种形状）/ 最近 30 轮客户「说过」/ 我方「复述过」/
   「问过且对方接了话」→ mentioned；confirmed 永不问；
2. ``decide_probe_target`` 只从 unknown 选；「问过的加回末尾」已撤；mentioned 只许 deepen
   （每槽每日 ≤1、只跟线索、不按下限兜）；
3. 回归用例「同会话职业已答后不得再问」（K24YJ2 回放）；
4. B：每槽每日 missed ≤2 当日休眠；回复含槽位词判 covered 不计 missed。
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

from src.companion.goals import service
from src.companion.goals.profile_slots import (
    SLOT_STATE_CONFIRMED,
    SLOT_STATE_MENTIONED,
    SLOT_STATE_UNKNOWN,
    cell_view,
    deepen_line,
    known_slots_line,
    pick_probe_target,
    reply_covers_slot,
    slot_state,
    slot_states,
)
from src.companion.goals.service import (
    PROBE_EVENT_DEEPEN,
    PROBE_PENDING_PARAM,
    PROBE_RETRY_PER_DAY,
    build_block_for_chat,
    decide_probe_target,
    discovery_probe_asks,
    verify_pending_probe,
)
from src.companion.goals.store import GoalStore, get_goal_store, reset_goal_store
from src.companion.goals.templates import get_template

CONV = "whatsapp:12137839654:13105551234"
PLAT, ACCT, CK = "whatsapp", "12137839654", "13105551234"


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


T0 = _local(2026, 9, 8, 20, 30)      # 同一本地日内回放（每槽每日计数不跨午夜）


def _cfg_obj():
    return SimpleNamespace(
        config={"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}},
        config_path=None)


class _Inbox:
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


def _goal(gs, slots="occupation,age,location,interests", now=T0):
    return gs.create_goal(
        conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
        template="profile_discovery", autonomy="auto", deadline_days=3,
        params={"slots": slots}, now=now) or {}


def _fresh():
    reset_goal_store()
    service._inject_log_seen.clear()
    return get_goal_store(":memory:")


def _inject(inbox, text, now):
    return build_block_for_chat(
        _cfg_obj(), platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
        user_context={}, chain="draft", inbound_text=text, inbox_store=inbox, now=now)


# ── A1 字段三种形状 ───────────────────────────────────────────────────────────
def test_cell_view_accepts_three_shapes_and_maps_sources():
    # Q-5 新形
    assert cell_view({"value": "carpenter", "source": "user", "status": "confirmed"}) == (
        "carpenter", "user", SLOT_STATE_CONFIRMED)
    assert cell_view({"value": "Nori", "source": "nickname", "status": "mentioned"})[2] == SLOT_STATE_MENTIONED
    assert cell_view({"value": "34", "source": "ai_inferred"})[2] == SLOT_STATE_MENTIONED
    # 旧形 {v, src, ts}
    assert cell_view({"v": "LA", "src": "agent", "ts": 1.0})[2] == SLOT_STATE_CONFIRMED
    assert cell_view({"v": "LA", "src": "auto", "ts": 1.0})[2] == SLOT_STATE_CONFIRMED
    assert cell_view({"v": "bakery", "src": "llm", "ts": 1.0})[2] == SLOT_STATE_MENTIONED
    # 裸字符串 = confirmed；空 = unknown
    assert cell_view("Martin") == ("Martin", "", SLOT_STATE_CONFIRMED)
    assert cell_view(None) == ("", "", SLOT_STATE_UNKNOWN)
    assert cell_view({"v": "", "src": "agent"})[2] == SLOT_STATE_UNKNOWN


# ── A2 三态：字段 / 说过 / 复述过 / 问过且接了话 ──────────────────────────────
def test_slot_state_field_wins_then_history_scan():
    assert slot_state("occupation", {"v": "carpenter", "src": "agent"}) == (
        SLOT_STATE_CONFIRMED, "field:agent")
    assert slot_state("occupation", {"v": "carpenter", "src": "llm"})[0] == SLOT_STATE_MENTIONED
    # K24YJ2 23:46：客户自己说了 → said
    hist = [{"direction": "in", "text": "lol it's a job. i'm a union carpenter at the convention center"}]
    st, why = slot_state("occupation", None, hist)
    assert st == SLOT_STATE_MENTIONED and why.startswith("said:")
    # AI 自己复述过（非问句）→ echoed
    hist2 = [{"direction": "out", "text": "Convention center work sounds intense, your job must keep you busy."}]
    st, why = slot_state("occupation", None, hist2)
    assert st == SLOT_STATE_MENTIONED and why.startswith("echoed:")
    # 我方问过 + 客户接了话 → answered（哪怕正则抽不出值）
    hist3 = [{"direction": "out", "text": "So what do you do for work?"},
             {"direction": "in", "text": "construction stuff, long days"}]
    assert slot_state("occupation", None, hist3) == (SLOT_STATE_MENTIONED, "answered")
    # 我方问了但客户没接（下一条还是我方）→ 不算 answered；我方问句本身不算 echoed
    hist4 = [{"direction": "out", "text": "What do you do for work?"},
             {"direction": "out", "text": "still there?"}]
    assert slot_state("occupation", None, hist4) == (SLOT_STATE_UNKNOWN, "")
    # 「i like that」是应答不是爱好；「I'm in bed」不是坐标
    assert slot_state("interests", None, [{"direction": "in", "text": "i like that"}])[0] == SLOT_STATE_UNKNOWN
    assert slot_state("interests", None, [{"direction": "in", "text": "i love fishing on weekends"}])[0] == SLOT_STATE_MENTIONED
    assert slot_state("location", None, [{"direction": "in", "text": "I'm in bed lol"}])[0] == SLOT_STATE_UNKNOWN
    assert slot_state("location", None, [{"direction": "in", "text": "I'm in Chicago this week"}])[0] == SLOT_STATE_MENTIONED
    assert slot_state("age", None, [{"direction": "in", "text": "i'm 34 btw"}])[0] == SLOT_STATE_MENTIONED
    assert slot_state("age", None, [{"direction": "in", "text": "我今年三十多"}])[0] == SLOT_STATE_MENTIONED
    # 只扫最近 30 轮：第 31 条之前的提及不算
    old = [{"direction": "in", "text": "i work at a bakery"}] + [
        {"direction": "in", "text": f"msg {i}"} for i in range(30)]
    assert slot_state("occupation", None, old)[0] == SLOT_STATE_UNKNOWN
    # 裸字符串历史也认
    assert slot_state("occupation", None, ["i work nights at the warehouse"])[0] == SLOT_STATE_MENTIONED
    # 坏输入不抛
    assert slot_state("occupation", object(), [None, 5, {"direction": "x"}])[0] == SLOT_STATE_UNKNOWN
    sts = slot_states(["occupation", "age"], {}, hist)
    assert sts["occupation"][0] == SLOT_STATE_MENTIONED and sts["age"][0] == SLOT_STATE_UNKNOWN


# ── A3 决议：只选 unknown；mentioned 只 deepen；不加回末尾 ────────────────────
def test_pick_probe_target_unknown_only_and_deepen_only_on_cue():
    # unknown 有候选 → 旧优先级 retry > cue > floor
    assert pick_probe_target(cues=[("occupation", "work")], unfilled=["age"], asked_today=False,
                             mentioned=["occupation"]) == ("age", "", "floor")
    # unknown 空 + mentioned 有线索 + 今天没深化过 → deepen
    assert pick_probe_target(cues=[("occupation", "work")], unfilled=[], asked_today=False,
                             mentioned=["occupation"], deepen_ok=["occupation"]) == (
        "occupation", "work", "deepen")
    # 今天已深化过 → 不再
    assert pick_probe_target(cues=[("occupation", "work")], unfilled=[], asked_today=False,
                             mentioned=["occupation"], deepen_ok=[]) == ("", "", "")
    # mentioned 无线索 → 绝不按下限兜（「每天问一遍已知的事」正是病根）
    assert pick_probe_target(cues=[], unfilled=[], asked_today=False,
                             mentioned=["occupation"], deepen_ok=["occupation"]) == ("", "", "")
    line = deepen_line("occupation", "work")
    assert line.startswith("【已知不再问】") and "绝不要再问" in line and "职业/生意" in line
    assert known_slots_line(["职业/生意", "年龄"]).startswith("【已知不再问】客户已经说过：职业/生意、年龄")
    assert known_slots_line([]) == ""


def test_decide_probe_target_never_returns_mentioned_and_drops_readd_tail(caplog):
    gs = GoalStore(":memory:")
    g = _goal(gs)
    gid = g["goal_id"]
    hist = [{"direction": "in", "text": "i'm a union carpenter at the convention center", "ts": T0}]
    # 职业已说过、无线索、今天没问过 → floor 落到第一个 unknown（age），不是 occupation
    slot, cue, mode, cues, states = decide_probe_target(
        gs, g, prof_fields={}, sel_slots=["occupation", "age", "location", "interests"],
        inbound_text="hey", retry_slot="", now=T0, history=hist)
    assert states["occupation"][0] == SLOT_STATE_MENTIONED
    assert (slot, mode) == ("age", "floor")
    # 「问过的加回末尾」已撤：全部 unknown 都问过 + 今天已真问过 + 无线索 → 不必问
    gs.update_goal_fields(gid, params={**g["params"], "_gap_asked": ["age", "location", "interests"]})
    gs.add_event(gid, "probe_asked", "age@-", now=T0)
    g2 = gs.get_goal(gid)
    slot, cue, mode, _c, _s = decide_probe_target(
        gs, g2, prof_fields={}, sel_slots=["occupation", "age", "location", "interests"],
        inbound_text="ok", retry_slot="", now=T0 + 60, history=hist)
    assert (slot, mode) == ("", "")
    # 职业线索来了：occupation 是 mentioned → deepen（不是 probe）
    slot, cue, mode, _c, _s = decide_probe_target(
        gs, g2, prof_fields={}, sel_slots=["occupation", "age", "location", "interests"],
        inbound_text="ugh long day at work", retry_slot="", now=T0 + 120, history=hist)
    assert (slot, mode) == ("occupation", "deepen")
    # confirmed 字段：连 deepen 都不出
    slot, cue, mode, _c, sts = decide_probe_target(
        gs, g2, prof_fields={"occupation": {"v": "carpenter", "src": "agent", "ts": T0}},
        sel_slots=["occupation", "age", "location", "interests"],
        inbound_text="ugh long day at work", retry_slot="", now=T0 + 180, history=hist)
    assert sts["occupation"][0] == SLOT_STATE_CONFIRMED and slot == ""
    # retry 只认 unknown：上轮 missed 的槽这轮客户答了 → 不再重试
    slot, cue, mode, _c, _s = decide_probe_target(
        gs, g2, prof_fields={}, sel_slots=["occupation", "age"],
        inbound_text="whatever", retry_slot="occupation", now=T0 + 240, history=hist)
    assert slot != "occupation"


# ── A4 回归：同会话职业已答后引擎不得再问（K24YJ2 回放）──────────────────────
def test_regression_same_conversation_occupation_answered_never_asked_again(caplog):
    store = _fresh()
    _goal(store, slots="occupation,age")
    inbox = _Inbox()
    try:
        with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
            # 23:41 第一稿：职业 unknown → 硬问职业（正常）
            inbox.add("in", "hey, finally off", T0)
            blk = _inject(inbox, "hey, finally off", T0)
            assert blk and "【本轮必问】" in blk and "做什么生意/工作" in blk
            inbox.add("out", "Finally! What do you do for work, by the way?", T0 + 30)
            # 23:46 客户答了（抽取关着，字段仍空）
            t2 = T0 + 300
            inbox.add("in", "lol it's a job. union carpenter at the convention center", t2)
            blk2 = _inject(inbox, "lol it's a job. union carpenter at the convention center", t2)
            assert blk2 and "【本轮必问】" not in blk2
            # 客户刚说了职业 + 话头是 job → 深化式一次（「绝不要再问…最多顺着追一句细节」）
            assert "【已知不再问】" in blk2 and "职业/生意" in blk2 and "绝不要再问" in blk2
            # 之后 12 轮任何话头（含 work / job 线索）都不得再出「必问职业」
            replies = ["Nice, that sounds like solid work.", "Haha yeah.", "Right?", "Same here.",
                       "Cool.", "Fair.", "Totally.", "Yep.", "Mm.", "Ok!", "Haha.", "Sure."]
            inbounds = ["long day at work today", "my boss is annoying", "ok", "what about you",
                        "job's fine", "nice", "haha", "hmm", "yeah", "work again tomorrow",
                        "meeting was long", "shift ended"]
            for i, (txt, rp) in enumerate(zip(inbounds, replies)):
                t = t2 + (i + 1) * 300
                inbox.add("in", txt, t)
                b = _inject(inbox, txt, t)
                assert b is not None
                if "【本轮必问】" in b:
                    assert "做什么生意/工作" not in b, b        # 必问只能指向年龄，永不指向职业
                    assert "大概哪个年龄段" in b
                inbox.add("out", rp, t + 30)
        gid = store.list_goals(status="active")[0]["goal_id"]
        # 职业 pending 只挂了第一次（第 1 稿）；之后 occupation 再没进 probe_missed / pending
        occ_missed = [e for e in store.list_events(gid, kinds=("probe_missed",))
                      if e["detail"].startswith("occupation@")]
        assert not occ_missed
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("target=occupation" in m and "decision=probe" in m and "result=pending" in m
                       for m in msgs[3:])
        # 深化式最多一次（同日）且是 deepen 不是 probe
        deep = store.list_events(gid, kinds=(PROBE_EVENT_DEEPEN,))
        assert len(deep) <= 1
        assert any("state=mentioned decision=deepen" in m for m in msgs) or not deep
        # 字段仍空（抽取关着）——「已知」完全来自会话扫描
        prof = store.get_customer_profile(PLAT, CK)
        assert not ((prof or {}).get("fields") or {}).get("occupation")
    finally:
        reset_goal_store()


def test_discovery_probe_asks_filters_mentioned_when_inbox_given():
    gs = GoalStore(":memory:")
    g = _goal(gs, slots="occupation,age")
    tpl = get_template("profile_discovery")
    inbox = _Inbox()
    inbox.add("in", "i work at the convention center", T0)
    assert discovery_probe_asks(gs, tpl, g) == ["做什么生意/工作", "大概哪个年龄段"]
    assert discovery_probe_asks(gs, tpl, g, inbox_store=inbox) == ["大概哪个年龄段"]


# ── B 每槽每日 retry ≤2 + covered ─────────────────────────────────────────────
def test_reply_covers_slot_without_question():
    assert reply_covers_slot("Yeah the carpentry job sounds tough, work must be long.", "occupation")
    assert not reply_covers_slot("Rainy days are cozy though.", "occupation")
    assert reply_covers_slot("Chicago winters are brutal!", "location") is False   # 没有坐标词
    assert reply_covers_slot("Which city vibe do you like more, big or small town?", "location")
    assert not reply_covers_slot("", "occupation")


def test_verify_pending_probe_covered_not_missed():
    store = GoalStore(":memory:")
    g = _goal(store)
    gid = g["goal_id"]
    store.update_goal_fields(gid, params={**g["params"], PROBE_PENDING_PARAM: {
        "slot": "occupation", "cue": "job", "ts": T0, "mode": "cue"}})
    inbox = _Inbox()
    # AI 没问，但回复顺着「job / convention center」聊了 → covered，不计 missed
    inbox.add("out", "Convention center gigs sound exhausting, that job must eat your evenings.", T0 + 20)
    v = verify_pending_probe(store, store.get_goal(gid), inbox_store=inbox, now=T0 + 60)
    assert v["result"] == "covered" and PROBE_PENDING_PARAM not in v["patch"]
    assert store.list_events(gid, kinds=("probe_covered",))
    assert not store.list_events(gid, kinds=("probe_missed",))


def test_slot_sleeps_after_two_missed_same_day(caplog):
    store = _fresh()
    _goal(store, slots="location,occupation")
    inbox = _Inbox()
    base = _local(2026, 9, 9, 9, 0)
    try:
        with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
            texts = ["it's raining here", "so wet outside", "weather is crazy here", "rain rain rain"]
            hard_loc = 0
            for i, txt in enumerate(texts):
                t = base + i * 300
                inbox.add("in", txt, t)
                b = _inject(inbox, txt, t)
                assert b
                if "【本轮必问】" in b and "人在哪个城市" in b:
                    hard_loc += 1
                inbox.add("out", "Haha yeah.", t + 30)        # 模型每次都不问、也没盖到坐标词
        # 线索每轮都有，但坐标只硬问 2 次（cue + retry）即当日休眠；第三次线索转到职业（floor）
        assert hard_loc == PROBE_RETRY_PER_DAY == 2
        gid = store.list_goals(status="active")[0]["goal_id"]
        loc_missed = [e for e in store.list_events(gid, kinds=("probe_missed",))
                      if e["detail"].startswith("location@")]
        assert len(loc_missed) == 2
        # 次日线索再来 → 坐标醒了
        t5 = _local(2026, 9, 10, 10, 0)
        inbox.add("in", "raining again here", t5)
        b5 = _inject(inbox, "raining again here", t5)
        assert b5 and "人在哪个城市" in b5
    finally:
        reset_goal_store()
