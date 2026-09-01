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


# ── 实施84 P0-5c（2026-08-29）：反 AI 问候腔 + 超短消息日 ────────────────────
# 老板点名「问候要像真人，不要虚假的问候像AI一样」——祝愿体/客服体/加油体是
# LLM 问候的最大公约数；形态日变（有时超短）是真人感的另一半。

def test_anti_bot_note_on_greeting_modes():
    for plan in ({"mode": "ritual_morning", "directive": "早安"},
                 {"mode": "gentle_checkin", "directive": "x", "silent_hours": 30},
                 {"mode": "follow_up", "directive": "x", "silent_hours": 30}):
        p = build_proactive_prompt("她", plan)
        assert "别带机器腔" in p, plan
        assert "元气满满" in p  # 禁词表在 prompt 里逐词点名


def test_anti_bot_note_milestone_weak_version_keeps_blessing():
    p = build_proactive_prompt(
        "她", {"mode": "milestone_birthday", "directive": "生日祝福"})
    assert "别带机器腔" in p
    assert "贺卡腔" in p
    # 弱化版不禁「祝你」——生日/节日祝福本身是正当用法
    assert "元气满满" not in p


def _brev_salts():
    import time as _t
    import zlib as _z
    now = _t.time()
    day = _t.strftime("%Y-%m-%d", _t.localtime(now))
    short = next(s for s in (f"c{i}" for i in range(300))
                 if _z.crc32(f"brev#{s}#{day}".encode("utf-8")) % 3 == 0)
    plain = next(s for s in (f"c{i}" for i in range(300))
                 if _z.crc32(f"brev#{s}#{day}".encode("utf-8")) % 3 != 0)
    return now, short, plain


def test_brevity_day_deterministic_per_conversation():
    now, short, plain = _brev_salts()
    mk = lambda cid: build_proactive_prompt(
        "她", {"mode": "ritual_morning", "directive": "早安",
               "conversation_id": cid}, now=now)
    assert "超短的" in mk(short)
    assert "超短的" in mk(short)   # 同会话同日恒定（tick 重试不换档）
    assert "超短的" not in mk(plain)


def test_brevity_suppressed_by_pending_and_non_greeting_modes():
    now, short, _plain = _brev_salts()
    # 悬空话头在身：接茬义务 > 超短形态
    p = build_proactive_prompt(
        "她", {"mode": "ritual_morning", "directive": "早安",
               "conversation_id": short},
        pending_inbound=["还没回我呢"], now=now)
    assert "超短的" not in p
    # follow_up 要引用具体事实，不吃超短档
    p2 = build_proactive_prompt(
        "她", {"mode": "follow_up", "directive": "x", "silent_hours": 30,
               "conversation_id": short}, now=now)
    assert "超短的" not in p2


def test_empty_plan_safe():
    p = build_proactive_prompt("", {})
    assert isinstance(p, str) and "她" in p  # ai_name 缺省回落「她」


def test_no_optional_blocks_when_absent():
    p = build_proactive_prompt("她", {"mode": "ritual_night", "directive": "晚安"})
    assert "背景" not in p          # 无 context_facts
    assert "最近的聊天" not in p     # 无 recent_context
    assert "风格示范" not in p       # 无 few_shot


# ── P1 语言锚补口（2026-08-03 おはよう 实锤）────────────────────────────────
# 会话 language=zh 的用户历史里混着日语探针消息 → 晨安 ritual 生成时 LLM 跟着
# 上下文写出「おはよう～」。此前语言钉子只对非中文加，zh 完全裸奔。

def test_peer_language_zh_pins_chinese():
    p = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安"},
        peer_language="zh",
        recent_context="TA(昨天): あなたは日本語で自己紹介をしますか？")
    assert "必须用中文写" in p
    assert "不要跟着换语言" in p


def test_peer_language_zh_variants_all_pin():
    for lang in ("zh", "zh-cn", "zh-TW", "zh-hans", "ZH-HANT"):
        p = build_proactive_prompt(
            "她", {"mode": "gentle_checkin", "directive": "x",
                   "silent_hours": 40}, peer_language=lang)
        assert "必须用中文写" in p, f"lang={lang} 未钉中文"


def test_peer_language_foreign_pin_unchanged():
    p = build_proactive_prompt(
        "她", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40},
        peer_language="ja")
    assert "必须用日语写" in p and "绝不要用中文" in p


def test_peer_language_unknown_or_empty_no_pin():
    for lang in ("", "unknown"):
        p = build_proactive_prompt(
            "她", {"mode": "gentle_checkin", "directive": "x",
                   "silent_hours": 40}, peer_language=lang)
        assert "必须用中文写" not in p
        assert "必须用" not in p


# ── P0 悬空话头接茬（2026-08-05 实锤：客户 22:27「以后给你介绍做你老公」无人接，
# 07:10 收到一条完全不接茬的通用晨安语音——上下文明明在 prompt 里，但 ritual 的
# 「发一句平常的问候 + ≤30字」框定压制了接茬动机）─────────────────────────────

def test_ritual_with_pending_inbound_requires_pickup():
    p = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安"},
        pending_inbound=["以后给你介绍做你老公"], pending_inbound_age="昨天")
    assert "以后给你介绍做你老公" in p
    assert "接住" in p and "装没看见" in p
    assert "TA 昨天说的" in p          # 相对时间进措辞
    assert "不超过40字" in p           # 有话头要接 → 字数放宽
    assert "不超过30字" not in p


def test_ritual_without_pending_inbound_unchanged():
    p = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安"})
    assert "接住" not in p             # 无话头 → 无接茬块（零行为变化）
    assert "不超过30字" in p


def test_checkin_with_pending_inbound_block():
    """接茬块对沉默回访类 mode 同样生效（不只 ritual）。"""
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40},
        pending_inbound=["我小弟，没关系", "以后给你介绍做你老公"])
    assert "我小弟，没关系" in p and "以后给你介绍做你老公" in p
    assert "TA 之前说的" in p          # 无 age → 兜底措辞


def test_pending_inbound_capped_and_truncated():
    p = build_proactive_prompt(
        "小柔", {"mode": "gentle_checkin", "directive": "x", "silent_hours": 40},
        pending_inbound=["第一条旧话头", "第二条旧话头", "长" * 80])
    assert "第一条旧话头" not in p     # 只保留最近 2 条
    assert "第二条旧话头" in p
    assert "长" * 60 in p and "长" * 61 not in p  # 单条截断 60 字


def test_ritual_header_has_casual_texting_style():
    """晨安文风钉子：像随手发微信、别用工整书面句（配合语音口语化 P0）。"""
    p = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安"})
    assert "随手发的微信" in p
    assert "句尾别用句号" in p


# ── P1 晨/晚安当日切入角（确定性轮换，防「每天同一句问候壳」）────────────────

def test_ritual_angle_deterministic_same_day_rotates_across_days():
    from src.utils.proactive_prompt import (
        _RITUAL_MORNING_ANGLES,
        _ritual_angle,
    )
    base = 1_785_900_000.0  # 2026-08-05 白天（UTC 与 UTC+8 落同一天，跨时区稳定）
    a1 = _ritual_angle("ritual_morning", "cid1", now=base)
    a2 = _ritual_angle("ritual_morning", "cid1", now=base + 3600)
    assert a1 and a1 == a2                    # 同日恒定（15min tick 重试不换角）
    assert a1 in _RITUAL_MORNING_ANGLES
    days = {_ritual_angle("ritual_morning", "cid1", now=base + 86400 * i)
            for i in range(10)}
    assert len(days) > 1                      # 跨日轮换（确定性非随机）
    assert _ritual_angle("gentle_checkin", "cid1", now=base) == ""


def test_ritual_prompt_carries_daily_angle_only_without_pending():
    p = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安",
                 "conversation_id": "c1"})
    assert "今天的切入角" in p
    p2 = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安",
                 "conversation_id": "c1"},
        pending_inbound=["以后给你介绍做你老公"])
    assert "今天的切入角" not in p2            # 接茬就是今天的切入角，不再另配


# ── 实施53 P2-2 人设当地钟批注（2026-08-22「一会白天一会晚上」延伸修复）──────
# 日历块管「问候时点」（对方的钟），本批注管「人设自身状态」（自己的钟）——
# 两个框架显式分工，防 LLM 在两个都正确的时间框架间随机横跳（B 线双时钟教训）。

def test_persona_clock_note_pure_fn_composes_local_frame():
    import datetime as dt

    from src.utils.proactive_prompt import build_persona_clock_note

    note = build_persona_clock_note(
        "加拿大·温哥华", dt.datetime(2026, 8, 21, 12, 31), -15.0)
    assert "加拿大·温哥华" in note
    assert "8月21日" in note and "周五" in note and "12:31" in note
    assert "中午" in note                      # 时段词随人设当地小时
    assert "我这边" in note                    # 单边桥话术
    assert "问候时点跟它走" in note            # 与日历块显式分工
    assert "绝不要把自己的作息放进对方的时段" in note


def test_persona_clock_note_near_tz_or_missing_returns_empty():
    import datetime as dt

    from src.utils.proactive_prompt import build_persona_clock_note

    t = dt.datetime(2026, 8, 21, 12, 31)
    assert build_persona_clock_note("泰国·清迈", t, -1.0) == ""   # <3h 不注入
    assert build_persona_clock_note("", t, -15.0) == ""            # 缺城市
    assert build_persona_clock_note("加拿大·温哥华", None, -15.0) == ""  # 缺时刻
    assert build_persona_clock_note("加拿大·温哥华", t, "bad") == ""     # 脏偏移


def test_persona_clock_note_injected_after_calendar_block():
    import datetime as dt

    from src.utils.proactive_prompt import build_persona_clock_note

    note = build_persona_clock_note(
        "加拿大·温哥华", dt.datetime(2026, 8, 21, 17, 5), -15.0)
    p = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安"},
        persona_clock_note=note)
    assert "你的当地钟" in p
    assert p.index("真实日历") < p.index("你的当地钟")  # 紧跟日历块，分工相邻可见
    p2 = build_proactive_prompt(
        "小柔", {"mode": "ritual_morning", "directive": "道一句早安"})
    assert "你的当地钟" not in p2               # 近时区/无居住地恒空=零行为变化
