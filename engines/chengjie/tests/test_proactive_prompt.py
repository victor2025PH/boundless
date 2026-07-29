"""Stage O：主动外发文案 prompt 组装（build_proactive_prompt）按 mode 自适应框定。"""

from __future__ import annotations

from src.utils.proactive_prompt import build_proactive_prompt


def test_ritual_morning_framing_not_long_absence():
    p = build_proactive_prompt("小柔", {"mode": "ritual_morning", "directive": "道一句早安"})
    assert "早安" in p
    assert "每天都会惦记" in p
    assert "许久未联系" not in p  # 仪式问候不套「久别重逢」框定（Stage O 修复）
    assert "不超过30字" in p
    assert "道一句早安" in p  # directive 入 prompt
    assert "小柔" in p


def test_ritual_night_framing():
    p = build_proactive_prompt("小柔", {"mode": "ritual_night", "directive": "道一句晚安"})
    assert "晚安" in p
    assert "许久未联系" not in p


def test_milestone_anniversary_framing_not_long_absence():
    p = build_proactive_prompt(
        "小柔", {"mode": "milestone_anniversary", "directive": "认识第100天"})
    assert "特别" in p and "应景" in p
    assert "许久未联系" not in p  # 节点不套「久别重逢」框定（Stage P）
    assert "认识第100天" in p  # 具体场合由 directive 承载


def test_milestone_holiday_framing():
    p = build_proactive_prompt(
        "小柔", {"mode": "milestone_holiday", "directive": "圣诞快乐"})
    assert "许久未联系" not in p
    assert "圣诞快乐" in p


def test_milestone_birthday_framing():
    p = build_proactive_prompt(
        "小柔", {"mode": "milestone_birthday", "directive": "生日快乐呀"})
    assert "许久未联系" not in p
    assert "生日快乐呀" in p


def test_scene_note_weaves_photo_context():
    """Phase17 文案-场景对齐：scene_note 非空 → prompt 要求把场景融进文案，
    且禁止「给你看照片/如图」类穿帮词；空则零行为变更。"""
    p = build_proactive_prompt(
        "小雨", {"mode": "gentle_checkin", "directive": "随口问候"},
        scene_note="convenience store where she works part-time, evening shift")
    assert "convenience store" in p
    assert "自拍" in p and "自然融进" in p
    assert "给你看照片" in p  # 作为禁止项出现在指令里
    p2 = build_proactive_prompt(
        "小雨", {"mode": "gentle_checkin", "directive": "随口问候"})
    assert "自拍" not in p2  # 不配图时无场景块（零行为变更）


def test_silence_mode_long_gap_keeps_long_absence_framing():
    """真隔了 ≥14 天 → 才允许「许久未联系」框定。"""
    p = build_proactive_prompt(
        "小柔", {"mode": "follow_up", "directive": "回访备考",
                 "silent_hours": 500, "gap_bucket": "long"})
    assert "许久未联系" in p
    assert "不超过40字" in p
    assert "回访备考" in p


def test_silence_mode_same_day_forbids_long_absence(caplog):
    """P0 修：沉默几小时的开场绝不能再被框成「许久未联系」（实锤根因）。"""
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "随口问候",
                 "silent_hours": 6.0, "gap_bucket": "same_day"})
    assert "许久未联系" not in p
    assert "今天才和TA聊过" in p
    assert "绝不要用「好久没联系" in p
    assert "大约 6 小时" in p  # 真实间隔进 prompt


def test_silence_mode_few_days_framing():
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x",
                 "silent_hours": 40.0, "gap_bucket": "few_days"})
    assert "一两天没聊" in p
    assert "许久未联系" not in p


def test_silence_mode_bucket_derived_from_hours_when_missing():
    """旧调用方/ask_* opener 不带 gap_bucket → 按 silent_hours 现算档位。"""
    p = build_proactive_prompt(
        "小柔", {"mode": "ask_birthday", "directive": "顺势问生日",
                 "silent_hours": 6.0})
    assert "今天才和TA聊过" in p
    assert "许久未联系" not in p


def test_silence_mode_no_gap_info_defaults_conservative():
    """连 silent_hours 都没有的兜底：按「几天没聊」框定，绝不默认久别重逢。"""
    p = build_proactive_prompt("小柔", {"mode": "follow_up", "directive": "回访备考"})
    assert "许久未联系" not in p
    assert "几天没聊" in p


def test_persona_style_injected():
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40},
        persona_style="温柔碎碎念，偶尔用波浪号，喜欢叫人「小笨蛋」")
    assert "你的说话风格" in p and "小笨蛋" in p
    p2 = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40})
    assert "你的说话风格" not in p2


def test_avoid_texts_block_lists_unanswered_openers():
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40},
        avoid_texts=["好久没联系啦，你最近过得怎么样呀？", "  ", "嘿，在忙吗"])
    assert "TA 还没有回应" in p
    assert "好久没联系啦，你最近过得怎么样呀？" in p
    assert "嘿，在忙吗" in p
    assert "完全不同的切入点" in p


def test_avoid_texts_capped_at_four():
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40},
        avoid_texts=[f"第{i}条旧开场" for i in range(1, 7)])
    assert "第4条旧开场" in p
    assert "第5条旧开场" not in p


def test_context_facts_block_included():
    p = build_proactive_prompt(
        "她", {"mode": "follow_up", "directive": "x",
               "context_facts": ["养了只猫", "  ", "下月搬家"]})
    assert "养了只猫" in p and "下月搬家" in p
    assert "背景" in p


def test_context_facts_truncated_to_three():
    p = build_proactive_prompt(
        "她", {"mode": "follow_up", "directive": "x",
               "context_facts": ["a", "b", "c", "d"]})
    assert "- d" not in p  # 仅取前 3 条


def test_recent_context_and_few_shot_appended():
    p = build_proactive_prompt(
        "她", {"mode": "ritual_morning", "directive": "早安"},
        recent_context="昨天聊了考试", few_shot_block="\n【风格示范】...\n")
    assert "昨天聊了考试" in p
    assert "【风格示范】" in p


def test_empty_plan_safe():
    p = build_proactive_prompt("", {})
    assert isinstance(p, str) and "她" in p  # ai_name 缺省回落「她」


def test_no_optional_blocks_when_absent():
    p = build_proactive_prompt("她", {"mode": "ritual_night", "directive": "晚安"})
    assert "背景" not in p          # 无 context_facts
    assert "最近的聊天" not in p     # 无 recent_context
    assert "风格示范" not in p       # 无 few_shot
