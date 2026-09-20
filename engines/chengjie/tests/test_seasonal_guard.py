# -*- coding: utf-8 -*-
"""季节/节令日历守卫门禁（2026-08-18「迎新表演」事故回归网）。

事故：8 月 18 日主动开场发出「诶我今天被拉去社团排迎新表演了」——迎新季在
9 月，暑假里说社团排练当场穿帮。三层防线各自回归：
① 素材层 pick_life_beat 过滤出窗节令节拍（到窗自动恢复，不删运营数据）；
② prompt 层真实日历行（无条件注入）+ 仪式双切入角池让行；
③ 出站层 present_claim_season_conflict（窄口径：同子句 现在时+出窗节令词；
   回忆/展望零误伤）+ _send 接线静态钉。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.companion.seasonal_guard import (
    filter_seasonal_beats,
    in_window,
    present_claim_season_conflict,
    season_conflict,
)

_AUG = datetime(2026, 8, 18)
_SEP = datetime(2026, 9, 10)
_DEC = datetime(2026, 12, 20)
_JUL = datetime(2026, 7, 15)
_JAN = datetime(2026, 1, 15)
_MAY = datetime(2026, 5, 10)

# 事故原文（生产逐字）
_INCIDENT = "诶我今天被拉去社团排迎新表演了，让我跳一小段舞😂  肢体不协调到把大家笑惨了"


# ── in_window ────────────────────────────────────────────────────────────────

def test_in_window_normal_and_wrap():
    assert in_window(901, (825, 1010)) is True
    assert in_window(818, (825, 1010)) is False
    # 跨年窗（寒假 12/20 → 3/1）
    assert in_window(1225, (1220, 301)) is True
    assert in_window(115, (1220, 301)) is True
    assert in_window(510, (1220, 301)) is False


# ── season_conflict（素材层口径：命中即冲突）────────────────────────────────

def test_season_conflict_incident_beat_in_august():
    beat = "社团在排迎新表演，我被拉去跳一小段舞，肢体不协调笑死大家"
    assert season_conflict(beat, _AUG) == "迎新"


def test_season_conflict_clears_in_september():
    beat = "社团在排迎新表演，我被拉去跳一小段舞"
    assert season_conflict(beat, _SEP) == ""


def test_season_conflict_christmas_and_winter_break():
    assert season_conflict("在准备圣诞礼物", _JUL) == "圣诞"
    assert season_conflict("在准备圣诞礼物", _DEC) == ""
    assert season_conflict("寒假想回老家", _JAN) == ""   # 跨年窗内
    assert season_conflict("寒假想回老家", _MAY) == "寒假"


def test_season_conflict_summer_and_neutral():
    assert season_conflict("暑假在便利店打工", _JUL) == ""
    assert season_conflict("暑假在便利店打工", datetime(2026, 10, 20)) == "暑假"
    assert season_conflict("在准备日语能力考，每天背单词", _AUG) == ""
    assert season_conflict("", _AUG) == ""


def test_season_conflict_bad_now_fails_open():
    assert season_conflict("圣诞快乐", object()) == ""


# ── present_claim_season_conflict（出站层口径：同子句 现在时+出窗）──────────

def test_present_claim_hits_incident_text():
    assert present_claim_season_conflict(_INCIDENT, _AUG) == "迎新"


def test_present_claim_passes_memory_and_outlook():
    # 回忆：无现在时标记 → 放行
    assert present_claim_season_conflict("上次圣诞和你聊得好开心", _AUG) == ""
    # 展望：无现在时标记 → 放行
    assert present_claim_season_conflict("离圣诞还有四个月呢", _AUG) == ""
    assert present_claim_season_conflict("圣诞想去北海道看雪", _AUG) == ""


def test_present_claim_requires_same_clause():
    # 现在时与节令词分属两个子句 → 放行（不做跨句联想）
    assert present_claim_season_conflict("今天上班好累。圣诞的事以后再说", _AUG) == ""


def test_present_claim_in_window_passes():
    assert present_claim_season_conflict("今天圣诞快乐呀", datetime(2026, 12, 25)) == ""
    assert present_claim_season_conflict(_INCIDENT, _SEP) == ""


def test_present_claim_near_future_marker():
    assert present_claim_season_conflict("明天就是万圣节派对了", _AUG) == "万圣节"


# ── filter_seasonal_beats + pick_life_beat 集成 ──────────────────────────────

_BEATS = [
    "这阵子便利店排班特别满，脚都站酸了",
    "在准备日语能力考 N1，每天背单词背到头大",
    "社团在排迎新表演，我被拉去跳一小段舞，肢体不协调笑死大家",
    "在追一部新番，昨晚一口气看到凌晨两点",
]


def test_filter_removes_out_of_window_only():
    out = filter_seasonal_beats(_BEATS, _AUG)
    assert len(out) == 3
    assert all("迎新" not in b for b in out)
    assert filter_seasonal_beats(_BEATS, _SEP) == _BEATS  # 9 月到窗，原池不动


def test_filter_all_conflict_returns_empty():
    assert filter_seasonal_beats(["圣诞夜要去唱诗班帮忙"], _AUG) == []


def test_pick_life_beat_never_returns_out_of_window_beat():
    from src.companion.deep_persona import pick_life_beat
    persona = {"id": "p1", "life_arc": {"beats": _BEATS, "stride_days": 1}}
    # 8 月上中旬每天轮询：迎新节拍绝不出现（stride=1 → 每天推进，覆盖全池）
    for day in range(1, 21):
        beat = pick_life_beat(persona, datetime(2026, 8, day))
        assert beat and "迎新" not in beat, (day, beat)
    # 9 月连续轮询：迎新节拍恢复可选（窗内，池完整轮转必然轮到）
    seen = {pick_life_beat(persona, datetime(2026, 9, d)) for d in range(1, 30)}
    assert any(b and "迎新" in b for b in seen)


def test_pick_life_beat_all_filtered_returns_none():
    from src.companion.deep_persona import pick_life_beat
    persona = {"id": "p1", "life_arc": {"beats": ["圣诞集市摆摊中"]}}
    assert pick_life_beat(persona, _AUG) is None
    assert pick_life_beat(persona, _DEC) == "圣诞集市摆摊中"


# ── prompt 层：真实日历行 + 双切入角池让行 ───────────────────────────────────

def test_prompt_carries_real_date_line():
    from src.utils.proactive_prompt import build_proactive_prompt
    ts = datetime(2026, 8, 18, 10, 0).timestamp()
    p = build_proactive_prompt(
        "小林", {"mode": "gentle_checkin", "directive": "问候一下"}, now=ts)
    assert "今天是8月18日" in p
    assert "节令活动必须符合" in p  # 反季约束在场


def test_prompt_ritual_angle_yields_to_directive_angle():
    from src.utils.proactive_prompt import build_proactive_prompt
    # directive 层已带「开场切入」（build_ritual_opener 素材化产物）→ prompt 层让行
    p = build_proactive_prompt(
        "小林",
        {"mode": "ritual_morning", "conversation_id": "c1",
         "directive": "道早安。今天的开场切入（参考方向）：聊聊早餐。"})
    assert "今天的切入角：" not in p
    # directive 无切入角（旧调用方/soft 档）→ prompt 层自己的池照常兜底
    p2 = build_proactive_prompt(
        "小林",
        {"mode": "ritual_morning", "conversation_id": "c1",
         "directive": "道早安即可。"})
    assert "今天的切入角：" in p2


# ── 问候词×时刻守卫（2026-08-19「上午 10:12 晚安」事故第四层）────────────────

from src.companion.seasonal_guard import greeting_time_conflict  # noqa: E402

# 事故原文（生产逐字，含尾部 emoji）
_WANAN_INCIDENT = "外面雨声哗哗的，被子裹紧点哈，晚安🌙"


def test_greeting_guard_hits_incident_text_at_morning():
    assert greeting_time_conflict(_WANAN_INCIDENT, 10) == "晚安"
    # 同文案在晚间是合法问候
    assert greeting_time_conflict(_WANAN_INCIDENT, 22) == ""
    assert greeting_time_conflict(_WANAN_INCIDENT, 0) == ""   # 凌晨说晚安合法


def test_greeting_guard_head_anchor():
    assert greeting_time_conflict("晚安呀，做个好梦", 14) == "晚安"
    assert greeting_time_conflict("早安！今天也要加油", 20) == "早安"
    assert greeting_time_conflict("早上好呀", 8) == ""


def test_greeting_guard_no_false_positive_on_mentions():
    # 「今晚安排」句首不是问候行为（锚定防中缀误伤的关键用例）
    assert greeting_time_conflict("今晚安排了什么呀", 10) == ""
    # 句中提及不拦（转述/回忆是合法人话）
    assert greeting_time_conflict("他昨天跟我说晚安来着，好可爱", 10) == ""


def test_greeting_guard_english_and_bad_hour():
    assert greeting_time_conflict("Good morning! 新的一天", 21) == "good morning"
    assert greeting_time_conflict("sleep tight, good night~", 3) == ""
    assert greeting_time_conflict("good night", 15) == "good night"
    assert greeting_time_conflict("晚安", "not-a-number") == ""
    assert greeting_time_conflict("", 10) == ""


def test_prompt_carries_recipient_hour():
    from src.utils.proactive_prompt import build_proactive_prompt
    p = build_proactive_prompt(
        "小林", {"mode": "gentle_checkin", "directive": "问候",
                 "local_hour": 22})
    assert "现在约22点" in p
    assert "上午别说晚安" in p
    # 无 local_hour → 服务器小时兜底
    ts = datetime(2026, 8, 19, 15, 0).timestamp()
    p2 = build_proactive_prompt(
        "小林", {"mode": "gentle_checkin", "directive": "问候"}, now=ts)
    assert "现在约15点" in p2


# ── _send 接线静态钉（守卫被移除/断线时先红）─────────────────────────────────

def test_send_path_wires_season_guard():
    src = Path("src/companion/proactive_topic.py").read_text(encoding="utf-8")
    assert "present_claim_season_conflict" in src
    assert "record_season_block" in src


def test_send_path_wires_greeting_time_guard():
    src = Path("src/companion/proactive_topic.py").read_text(encoding="utf-8")
    assert "greeting_time_conflict" in src
    assert "record_greeting_time_block" in src
