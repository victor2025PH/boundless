"""生辰抽取纯函数门禁：关键词门控 / 多格式 / 时辰段落词 / 农历 / 性别 / 往返落库。"""

from __future__ import annotations

from src.companion.bazi_profile import (
    birth_info_fact_text,
    birth_info_from_turn,
    extract_birth_info,
    extract_gender,
)


# ── 关键词门控（保守原则：无出生/命理语境不解析） ──────────────────────────────

def test_no_keyword_no_extract():
    assert extract_birth_info("我们1995年3月5日开会") is None
    assert extract_birth_info("1995-03-05 之前交报告") is None


def test_birth_keyword_gates_open():
    info = extract_birth_info("我是1995年3月5日出生的")
    assert info is not None
    assert (info.year, info.month, info.day) == (1995, 3, 5)
    assert not info.hour_known()


def test_ri_sheng_suffix_gates_open():
    """「1995年3月5日生的」——「生」紧跟日期也算出生语境（真实高频口语）。"""
    info = extract_birth_info("我1995年3月5日生的")
    assert info is not None
    assert (info.year, info.month, info.day) == (1995, 3, 5)


def test_bazi_keyword_gates_open():
    """算命语境下报日期几乎必为生辰 → 命理系关键词同样开门。"""
    info = extract_birth_info("帮我算下八字，1990/12/1")
    assert info is not None
    assert (info.year, info.month, info.day) == (1990, 12, 1)


def test_month_day_only_not_enough():
    """只有月日（无年）归 birthday.py 管，本模块不出半截 BirthInfo。"""
    assert extract_birth_info("我生日是3月5日") is None


# ── 日期格式 ─────────────────────────────────────────────────────────────────

def test_cn_ymd_with_hour():
    info = extract_birth_info("我出生于1995年3月5日早上8点半")
    assert (info.year, info.month, info.day, info.hour, info.minute) == (1995, 3, 5, 8, 30)


def test_two_digit_year():
    info = extract_birth_info("我是95年3月5日出生的")
    assert info.year == 1995
    info2 = extract_birth_info("我是05年3月5日出生的")
    assert info2.year == 2005


def test_iso_datetime():
    info = extract_birth_info("出生时间 1995-03-05 08:30")
    assert (info.hour, info.minute) == (8, 30)


# ── 时辰解析 ─────────────────────────────────────────────────────────────────

def test_shichen_traditional():
    info = extract_birth_info("我出生在1995年3月5日辰时")
    assert info.hour == 7  # 辰时 07-09

def test_evening_hour_shift():
    info = extract_birth_info("1995年3月5日晚上8点出生")
    assert info.hour == 20


def test_noon_and_morning_hours():
    assert extract_birth_info("1995年3月5日中午12点出生").hour == 12
    assert extract_birth_info("1995年3月5日凌晨2点出生").hour == 2


def test_ambiguous_midnight_dropped():
    """「晚上12点」跨日柱歧义 → 宁缺勿错，时辰按未知。"""
    info = extract_birth_info("1995年3月5日晚上12点出生")
    assert info is not None
    assert not info.hour_known()


def test_hour_unknown_stated():
    info = extract_birth_info("我1995年3月5日出生，时辰未知")
    assert info is not None
    assert not info.hour_known()


# ── 第三人称护栏（画像污染防线） ───────────────────────────────────────────────

def test_third_party_birth_not_captured_as_self():
    """给男朋友/家人算 → 不落成本人生辰（排错盘比不排更糟）。"""
    assert extract_birth_info("帮我男朋友算算，他1993年5月2日出生") is None
    assert extract_birth_info("我妈是1968年8月8日生的，帮她看看") is None
    assert extract_birth_info("my boyfriend was born 1993-05-02, read his bazi") is None


def test_self_birth_still_captured():
    info = extract_birth_info("帮我算算，我是1995年3月5日出生的")
    assert info is not None


# ── 农历 / 性别 ───────────────────────────────────────────────────────────────

def test_lunar_flag():
    info = extract_birth_info("我农历1995年2月5日出生")
    assert info.is_lunar is True


def test_gender_in_text():
    info = extract_birth_info("女命，1995年3月5日辰时出生")
    assert info.gender == "female"


def test_extract_gender_bare_and_pattern():
    assert extract_gender("女生") == "female"
    assert extract_gender("男") == "male"
    assert extract_gender("性别：女") == "female"
    assert extract_gender("我今天见了个男生朋友") == ""  # 长句裸词不误判
    assert extract_gender("") == ""


# ── 一轮对话双路抽取（Stage S 同机制） ─────────────────────────────────────────

def test_from_turn_user_path():
    info = birth_info_from_turn("我出生于1995年3月5日早上8点", "好的记住啦")
    assert info is not None and info.hour == 8


def test_from_turn_ai_confirm_path():
    """用户裸报（无关键词不命中）→ AI 复述确认命中，并从用户消息补性别。"""
    info = birth_info_from_turn(
        "1995年3月5日早上8点，女生",
        "记住啦，你是1995年3月5日早上8点出生的呀")
    assert info is not None
    assert (info.year, info.hour) == (1995, 8)
    assert info.gender == "female"


def test_from_turn_ai_question_no_false_hit():
    """AI 只是提问（无日期）→ 不误抽。"""
    assert birth_info_from_turn("帮我算算", "你是哪年哪月哪日出生的呀？") is None


# ── 规范化落库往返（写出的事实必须能被自己复解析） ─────────────────────────────

def test_fact_text_roundtrip_full():
    from src.companion.bazi_engine import BirthInfo
    src = BirthInfo(1995, 3, 5, 8, 30, is_lunar=False, gender="female")
    fact = birth_info_fact_text(src)
    back = extract_birth_info(fact)
    assert back is not None
    assert back.cache_key() == src.cache_key()


def test_fact_text_roundtrip_lunar_no_hour():
    from src.companion.bazi_engine import BirthInfo
    src = BirthInfo(1995, 2, 5, is_lunar=True)
    fact = birth_info_fact_text(src)
    back = extract_birth_info(fact)
    assert back is not None
    assert back.is_lunar is True
    assert not back.hour_known()
    assert back.cache_key() == src.cache_key()


def test_fact_text_compatible_with_birthday_extractor():
    """同一条生辰事实可被 birthday.extract_birthday 解出 (月,日) 供生日仪式复用。"""
    from src.companion.bazi_engine import BirthInfo
    from src.utils.birthday import extract_birthday
    fact = birth_info_fact_text(BirthInfo(1995, 3, 5, 8, 30))
    assert extract_birthday(fact) == (3, 5)
