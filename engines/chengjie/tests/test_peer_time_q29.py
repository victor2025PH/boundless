# -*- coding: utf-8 -*-
"""Q-29（#305 · 2RKH3H）：客户当地时间感知 + 时间问句去重。

四条门禁：① location=「東京」→ Asia/Tokyo + 注入含「不要问…几点」；② 客户「我这边晚上」→ 12h 内出站
「白天还是晚上」被砍；③ tz 未知 → 顺口问一次，24h 内不再问；④ 2RKH3H 回放（03:57「我这边四点了」→
05:25「你那边现在是白天还是晚上？」必砍）。另：persona_reply / decide_probe_target 零改动、location 槽语义不变、
城市表命中不到不猜、FactGate transient 只认原句锚定。"""
from __future__ import annotations

import inspect
import json
import time

import pytest

from src.companion import fact_gate as fg
from src.companion import peer_time as pt
from src.inbox import excuse_budget as eb
from src.inbox import repeat_question_guard as rq
from src.inbox import time_context as tc
from src.utils import business_domain as bd

CONV = "telegram:8538547216:7332005191"
CFG = {"business_domain": "companion", "companion": {"time_schedule": {"enabled": False}}}
CFG_SALES = {"business_domain": "sales"}
# 2RKH3H 现场：服务器 UTC+8，客户 03:57 说「四点」 → 客户偏移 UTC+8（2026-09-12 03:57 CST = 2026-09-11 19:57Z）
T_SAID = 1789156620.0  # 2026-09-11T19:57:00Z
T_WRONG = T_SAID + 41 * 60  # 04:38 我方错说「应该是晚上了吧」
T_REPEAT = T_SAID + 44 * 60  # 04:41 客户重申
T_DRAFT = T_SAID + 88 * 60  # 05:25 我方稿问「白天还是晚上」


class _Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.kv = {}

    def list_recent_messages(self, conv, limit=50):
        return self.rows[-limit:]

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True


def _in(text, ts):
    return {"direction": "in", "text": text, "ts": ts}


def _out(text, ts):
    return {"direction": "out", "text": text, "ts": ts}


def _profile(loc, status="confirmed", key="location"):
    return {"fields": {key: {"value": loc, "source": "customer", "status": status}}}


@pytest.fixture(autouse=True)
def _reset_domain():
    bd.reset_active_business_domain()
    yield
    bd.reset_active_business_domain()


# ---------------- A. 城市表 → 时区：命中不到不猜 ----------------

@pytest.mark.parametrize("text, tz", [
    ("東京", "Asia/Tokyo"), ("东京", "Asia/Tokyo"), ("Tokyo", "Asia/Tokyo"), ("tokyo", "Asia/Tokyo"),
    ("台北", "Asia/Taipei"), ("香港", "Asia/Hong_Kong"), ("HK", "Asia/Hong_Kong"), ("LA", "America/Los_Angeles"),
    ("I live in LA", "America/Los_Angeles"), ("new york city", "America/New_York"), ("Manchester", "Europe/London"),
    ("日本", "Asia/Tokyo"), ("成都", "Asia/Shanghai"), ("北京都很冷", "Asia/Shanghai"),
])
def test_city_to_tz_hits(text, tz):
    assert pt.city_to_tz(text) == tz


@pytest.mark.parametrize("text", [
    "", "Nice weather", "美国", "in the US", "I'm from Tokyo but live in London", "san jose", "完成都要", "hk",
    "somewhere warm", "火星",
])
def test_city_to_tz_never_guesses(text):
    assert pt.city_to_tz(text) is None


def test_resolve_peer_tz_confirmed_over_mentioned_and_none_when_unknown():
    assert pt.resolve_peer_tz(_profile("東京")) == "Asia/Tokyo"
    assert pt.resolve_peer_tz(_profile("東京", status="mentioned")) == "Asia/Tokyo"
    prof = {"fields": {"location": {"value": "東京", "status": "confirmed"},
                       "residence": {"value": "London", "status": "mentioned"}}}
    assert pt.resolve_peer_tz(prof) == "Asia/Tokyo"
    assert pt.resolve_peer_tz(_profile("美国")) is None
    assert pt.resolve_peer_tz({"fields": {}}) is None
    assert pt.resolve_peer_tz(None) is None


def test_peer_local_time_shape():
    lt = pt.peer_local_time("Asia/Tokyo", T_DRAFT)  # 2026-09-11 21:25Z → 东京 06:25 周六
    assert lt["hh_mm"] == "06:25" and lt["period"] == "早上" and lt["weekday"] == "周六"
    assert pt.peer_local_time("Mars/Olympus", T_DRAFT) is None
    assert pt.peer_local_time("UTC+8", T_DRAFT)["hh_mm"] == "05:25"


# ---------------- A. 时间叙述检出 ----------------

@pytest.mark.parametrize("text, kind, hour, period", [
    ("我这边四点了，再一会天就亮了", "clock", 4, "夜里"),
    ("你又记忆不行了我说我这边四点了再一会天就亮了", "clock", 4, "夜里"),
    ("我这边现在是晚上", "period", None, "夜里"),
    ("刚天亮", "period", None, "早上"),
    ("我这边下午三点半", "clock", 15, "白天"),
    ("现在凌晨两点了还没睡", "clock", 2, "夜里"),
    ("it's 4am here", "clock", 4, "夜里"),
    ("it's pretty late here", "period", None, "夜里"),
    ("morning here!", "period", None, "早上"),
])
def test_detect_time_statement_hits(text, kind, hour, period):
    got = pt.detect_time_statement(text)
    assert got and got["kind"] == kind and got["hour"] == hour and got["period"] == period


@pytest.mark.parametrize("text", [
    "你那边应该已经是晚上了吧", "我们晚上八点见", "晚上八点下班", "这里有3点建议", "我这边一点都不冷",
    "白天也上班，晚上也上班", "现在几点了？", "I have 3 kids here", "let's meet at 3pm", "is it night there?", "",
])
def test_detect_time_statement_rejects(text):
    assert pt.detect_time_statement(text) is None


def test_detect_keeps_customer_original_sentence_as_evidence():
    got = pt.detect_time_statement("我这边四点了，再一会天就亮了")
    assert got["sentence"] == "我这边四点了，再一会天就亮了"  # 原句（含全角逗号），FactGate 逐字锚定用
    assert got["quoted"] is False
    # 复述：钟点是先前读的，说话时刻不是读钟时刻 → 不能拿 04:41 当「四点」反推时区
    assert pt.detect_time_statement("你又记忆不行了我说我这边四点了再一会天就亮了")["quoted"] is True
    assert pt.detect_time_statement("like I said it's 4am here")["quoted"] is True


def test_offset_from_statement_replay():
    assert pt.offset_from_statement(4, 0, T_SAID) == "UTC+8"


# ---------------- A. FactGate kind=transient：只认原句锚定 ----------------

def test_fact_gate_transient_requires_verbatim_evidence():
    ok, why = fg.check("我这边四点了", slot_or_kind=fg.KIND_TRANSIENT, evidence="我这边四点了，再一会天就亮了",
                       inbound_texts=["我这边四点了，再一会天就亮了"])
    assert ok and why == ""
    ok, why = fg.check("我这边四点了", slot_or_kind=fg.KIND_TRANSIENT, evidence="",
                       inbound_texts=["我这边四点了"])
    assert not ok and why == fg.REASON_NO_EVIDENCE
    ok, why = fg.check("我这边四点了", slot_or_kind=fg.KIND_TRANSIENT, evidence="我这边四点了",
                       inbound_texts=["今天好累"])
    assert not ok and why == fg.REASON_UNANCHORED
    assert fg.TRANSIENT_TTL_SEC == 12 * 3600


# ---------------- ① location=東京 → Asia/Tokyo + 注入「不要问几点」 ----------------

def test_gate1_tokyo_location_injects_do_not_ask(monkeypatch):
    st = _Store([_in("hi", T_DRAFT - 100)])
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: _profile("東京"))
    # 经 Q-8 F 的同一接线点（persona_reply ④ try-block 唯一调用），persona_reply 零改动
    s = eb.build_time_schedule_addendum({}, CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=st)
    assert s.startswith("【对方当地时间】")
    assert "06:25" in s and "早上" in s and "東京" in s
    assert "不要问对方几点" in s and "白天还是晚上" in s
    assert "时区未知" not in s
    s_en = eb.build_time_schedule_addendum({}, CONV, CFG, lang="en", now=T_DRAFT, inbox_store=st)
    assert "【peer local time】" in s_en and "Do NOT ask what time it is there" in s_en


def test_gate1_guard_strips_time_question_when_city_known(monkeypatch):
    st = _Store([_in("hi", T_DRAFT - 100)])
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: _profile("東京"))
    out, rep = rq.check_repeat_questions("好呀。你那边几点了？", conversation_id=CONV, lang="zh", store=st,
                                         now=T_DRAFT, cfg_root=CFG)
    assert out == "好呀。" and rep["action"] == "strip"
    assert rep["matched"][0]["matched"] == "peer_time:city" and rep["matched"][0]["sim"] == 1.0
    out, rep = rq.check_repeat_questions("nice. what time is it there?", conversation_id=CONV, lang="en", store=st,
                                         now=T_DRAFT, cfg_root=CFG)
    assert out == "nice." and rep["action"] == "strip"


# ---------------- ② 客户「我这边晚上」→ 12h 内「白天还是晚上」被砍 ----------------

def test_gate2_period_statement_blocks_day_or_night_within_12h(monkeypatch):
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: None)
    t_said = T_DRAFT - 2 * 3600
    st = _Store([_in("我这边现在是晚上", t_said)])
    draft = "哈哈好。你那边现在是白天还是晚上？"
    out, rep = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=T_DRAFT, cfg_root=CFG)
    assert out == "哈哈好。" and rep["matched"][0]["matched"] == "peer_time:hint"
    assert rep["matched"][0]["matched_ts"] == t_said
    # 注入侧同步：已知时段
    s = pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=st)
    assert "夜里" in s and "不要问对方几点" in s
    # 12h 之后：自述过期 → 不再据此拦（tz 未知 → 顺口问一次）
    t_late = t_said + 12 * 3600 + 60
    out2, rep2 = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=t_late, cfg_root=CFG)
    assert out2 == draft and rep2["action"] == "clean"
    s2 = pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=t_late, inbox_store=st)
    assert "时区未知" in s2


def test_gate2_hint_written_through_fact_gate_kv_12h(monkeypatch):
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: None)
    st = _Store([_in("我这边现在是晚上", T_DRAFT - 600)])
    pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=st)
    raw = st.kv.get(pt.hint_key(CONV))
    assert raw
    h = json.loads(raw)
    assert h["kind"] == "period" and h["period"] == "夜里" and h["evidence"] == "我这边现在是晚上"
    assert h["ttl_sec"] == 12 * 3600
    # 不过 FactGate 的不写：我方说的「晚上」不是客户自述
    st2 = _Store([_out("你那边应该已经是晚上了吧", T_DRAFT - 600)])
    pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=st2)
    assert pt.hint_key(CONV) not in st2.kv
    assert pt.note_inbound("我们晚上八点见", CONV, inbox_store=st2, ts=T_DRAFT) is None
    assert pt.hint_key(CONV) not in st2.kv


# ---------------- ③ tz 未知 → 顺口问一次，24h 内不再问 ----------------

def test_gate3_unknown_ask_once_then_not_within_24h(monkeypatch):
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: None)
    st = _Store([_in("在忙", T_DRAFT - 100)])
    s = pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=st)
    assert "时区未知" in s and "顺口问一次" in s and "24 小时内不要再问" in s and "已经问过" not in s
    draft = "好呀。你那边现在是白天还是晚上？"
    out, rep = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=T_DRAFT, cfg_root=CFG)
    assert out == draft and rep["action"] == "clean"  # 第一次：放行
    # 我方问了一次（换个说法）
    st.rows.append(_out("你那边几点了呀？", T_DRAFT + 60))
    t2 = T_DRAFT + 3 * 3600
    s2 = pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=t2, inbox_store=st)
    assert "时区未知" in s2 and "已经问过一次" in s2
    out2, rep2 = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=t2, cfg_root=CFG)
    assert out2 == "好呀。" and rep2["action"] == "strip" and rep2["matched"][0]["matched"] == "你那边几点了呀？"
    # 24h 后：窗口过了，可以再顺口问
    t3 = T_DRAFT + 60 + 24 * 3600 + 5
    out3, rep3 = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=t3, cfg_root=CFG)
    assert out3 == draft and rep3["action"] == "clean"
    assert "已经问过" not in pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=t3, inbox_store=st)


# ---------------- ④ 2RKH3H 回放 ----------------

def _replay_store():
    return _Store([
        _in("我这边四点了，再一会天就亮了", T_SAID),
        _out("嗯嗯，你那边应该已经是晚上了吧", T_WRONG),
        _in("你又记忆不行了我说我这边四点了再一会天就亮了", T_REPEAT),
    ])


def test_gate4_2rkh3h_replay_strips_day_or_night(monkeypatch):
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: None)
    st = _replay_store()
    known, source, hts = pt.peer_time_known(CONV, inbox_store=st, now=T_DRAFT, config=CFG)
    assert known and source == "hint" and hts == T_SAID
    draft = "哈哈好的，那你先去忙吧。你那边现在是白天还是晚上？"
    out, rep = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=T_DRAFT, cfg_root=CFG)
    assert out == "哈哈好的，那你先去忙吧。"
    assert rep["action"] == "strip" and rep["matched"][0]["matched"] == "peer_time:hint"


def test_gate4_2rkh3h_replay_injection_knows_it_is_dawn(monkeypatch):
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: None)
    st = _replay_store()
    info = pt.peer_time_status(CONV, inbox_store=st, now=T_DRAFT)
    assert info["known"] and info["source"] == "hint" and info["tz"] == "UTC+8"
    assert info["hh_mm"] == "05:25" and info["period"] == "早上" and info["approx"]
    s = tc.build_peer_time_hint(info, lang="zh")
    assert "05:25" in s and "早上" in s and "我这边四点了" in s and "不要问对方几点" in s
    # 04:38 那条「应该已经是晚上了吧」若是当时起草 → 也已知（04:38 夜里），prompt 不会让它问
    info2 = pt.peer_time_status(CONV, inbox_store=_Store([_in("我这边四点了，再一会天就亮了", T_SAID)]), now=T_WRONG)
    assert info2["known"] and info2["hh_mm"] == "04:38" and info2["period"] == "夜里"


def test_hint_reconciles_with_city_within_1h_else_customer_wins(monkeypatch):
    st = _replay_store()
    # 画像城市 台北（UTC+8）与「四点」反推 UTC+8 一致 → 取城市精确钟
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: _profile("台北"))
    info = pt.peer_time_status(CONV, inbox_store=st, now=T_DRAFT)
    assert info["source"] == "hint+city" and info["tz"] == "Asia/Taipei" and not info["conflict"]
    # 画像城市 London（UTC+1）与「四点」冲突 → 客户原话为准
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: _profile("London"))
    info = pt.peer_time_status(CONV, inbox_store=st, now=T_DRAFT)
    assert info["source"] == "hint" and info["tz"] == "UTC+8" and info["conflict"]
    assert "以对方原话为准" in tc.build_peer_time_hint(info, lang="zh")


# ---------------- 时间问句族 ----------------

@pytest.mark.parametrize("s", [
    "你那边现在是白天还是晚上？", "你那边几点了？", "现在几点了呀？", "你那边应该是晚上了吧？", "你那边天亮了吗？",
    "是不是该睡了？", "你那边早上还是晚上呀", "what time is it there?", "is it night there?", "morning or night for you?",
    "Is it already morning where you are?", "what time zone are you in?", "shouldn't you be asleep?",
])
def test_is_time_question_family(s):
    assert rq.is_time_question(s)
    assert "slot:peer_time" in rq._slot_tags(s)


@pytest.mark.parametrize("s", [
    "你一般几点下班？", "我们几点见？", "你平时几点睡？", "你晚上一般干嘛？", "你喜欢白天还是晚上工作？",
    "what time do you usually wake up?", "shall we meet at 3pm?", "what do you do for work?", "早上好呀，睡得好吗？",
    "Do you like the night life?", "",
])
def test_is_time_question_not_family(s):
    assert not rq.is_time_question(s)


def test_time_question_family_same_thing_sim_1():
    assert rq.similarity("你那边现在是白天还是晚上？", "你那边几点了？", "zh") == 1.0
    assert rq.similarity("what time is it there?", "is it morning or night for you?", "en") == 1.0


# ---------------- 开关 / 红线 ----------------

def test_sales_domain_default_off(monkeypatch):
    monkeypatch.setattr(pt, "load_profile", lambda conv, goal_store=None: _profile("東京"))
    st = _replay_store()
    assert pt.build_peer_time_addendum(CONV, CFG_SALES, lang="zh", now=T_DRAFT, inbox_store=st) == ""
    draft = "好呀。你那边几点了？"
    out, rep = rq.check_repeat_questions(draft, conversation_id=CONV, lang="zh", store=st, now=T_DRAFT, cfg_root=CFG_SALES)
    assert out == draft and rep["action"] == "clean"
    assert pt.is_enabled({"business_domain": "sales", "companion": {"peer_time": {"enabled": True}}})


def test_no_inbox_store_or_no_conv_is_silent():
    class _Kv:
        def get_app_setting(self, k, d=""):
            return d

        def set_app_setting(self, k, v, updated_by=""):
            return True

    assert pt.build_peer_time_addendum(CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=_Kv()) == ""
    assert pt.build_peer_time_addendum("", CFG, lang="zh", now=T_DRAFT, inbox_store=_Store()) == ""
    assert eb.build_time_schedule_addendum({}, CONV, CFG, lang="zh", now=T_DRAFT, inbox_store=_Kv()) == ""


def test_persona_reply_and_decide_probe_target_untouched():
    from src.inbox import persona_reply
    src = inspect.getsource(persona_reply._prompt_addenda)
    assert "peer_time" not in src and "peer_local" not in src
    assert src.count("build_time_schedule_addendum") == 2  # Q-8 F pin 不变：经同一 ④ try-block 接线
    from src.companion.goals import service as gsvc
    assert "peer_time" not in inspect.getsource(gsvc.decide_probe_target)


def test_location_slot_semantics_unchanged():
    """时间自述不是 location；peer_time 只读画像、绝不写画像槽。"""
    from src.companion.goals import profile_slots as ps
    got = ps.capture_from_text("我这边四点了，再一会天就亮了")
    assert not any(k == "location" for k, _v in (got or []))
    src = inspect.getsource(pt)
    for forbidden in ("set_customer_profile", "upsert_customer_profile", "update_customer_profile", "confirm_slot"):
        assert forbidden not in src
