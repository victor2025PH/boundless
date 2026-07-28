"""user_clock 离线门禁：推断链（自述/国码/行为 MLE/互证/语种）+ naive 时钟 + 三档调度策略。

全部离线（tzdata 本地包），不触网、不依赖生产服务；时钟断言一律注入固定 aware UTC 时刻，
安静时段一律显式传 server_hour，保证与本机时区无关的确定性。
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.companion.user_clock import (
    ACTIVITY_PRIOR,
    PHONE_CC_COUNTRY,
    TRUST_ADVISORY,
    TRUST_NARROW,
    TRUST_REPLACE,
    UserClock,
    corroborate,
    dump_stats,
    in_quiet_hours,
    infer_from_activity,
    infer_from_language,
    infer_from_phone,
    infer_from_stated_place,
    reset_stats_for_tests,
    resolve_user_clock,
    schedule_clock,
    shift_hours_to_clock,
    user_day_key,
    user_local_hour,
    user_month_day,
    user_now,
    user_time_line,
)
from src.companion.user_clock import _COUNTRY_TZ, _MULTI_TZ_COUNTRIES

UTC = timezone.utc
SUMMER_UTC = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)   # 洛杉矶 PDT(-7) -> 05:00
WINTER_UTC = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)   # 洛杉矶 PST(-8) -> 04:00
BKK_0612_UTC = datetime(2026, 7, 27, 23, 12, tzinfo=UTC)  # 曼谷 2026-07-28 周二 06:12

# 安静时段真值表用的两个时刻（曼谷 +7）
TS_USER_QUIET = datetime(2026, 7, 27, 21, 0, tzinfo=UTC).timestamp()   # 曼谷 04:00 = 安静
TS_USER_AWAKE = datetime(2026, 7, 28, 7, 0, tzinfo=UTC).timestamp()    # 曼谷 14:00 = 清醒

# 垃圾输入语料（每个公开函数都要能吞下）
JUNK = (None, "", "   ", 0, -1, 10**18, 3.5, [], {}, {"a": 1}, ["x"], object(), True)


@pytest.fixture(autouse=True)
def _clean_stats():
    reset_stats_for_tests()
    yield
    reset_stats_for_tests()


def _hist_from_local(local_hours: dict, true_offset: int) -> dict:
    """本地小时直方图 → UTC 小时直方图（utc = local - offset）。"""
    out: dict = {}
    for lh, n in local_hours.items():
        out[(lh - true_offset) % 24] = out.get((lh - true_offset) % 24, 0) + n
    return out


def _prior_shaped_day() -> dict:
    """按活跃度先验塑形的「本地 8-24 点活跃」真实感直方图（266 样本）。"""
    return {h: max(1, round(ACTIVITY_PRIOR[h] * 300)) for h in range(8, 24)}


def _behavior_clock(offset: int) -> UserClock:
    return UserClock(
        tz_name=f"UTC{offset:+03d}:00", offset_hours=float(offset), source="behavior",
        confidence=0.6, country="", city_slug="", trust=TRUST_NARROW)


def _server_offset_hours() -> float:
    off = datetime.now().astimezone().utcoffset()
    return off.total_seconds() / 3600.0 if off else 0.0


# ---------------------------------------------------------------------------
# 常量 / 静态表完整性
# ---------------------------------------------------------------------------

def test_trust_constants_are_distinct_literals():
    assert (TRUST_REPLACE, TRUST_NARROW, TRUST_ADVISORY) == ("replace", "narrow", "advisory")
    assert len({TRUST_REPLACE, TRUST_NARROW, TRUST_ADVISORY}) == 3


def test_user_clock_frozen_and_fixed_offset_property():
    c = _behavior_clock(7)
    assert c.is_fixed_offset is True
    assert infer_from_stated_place("曼谷").is_fixed_offset is False
    with pytest.raises(Exception):
        c.tz_name = "Asia/Tokyo"  # type: ignore[misc]  # frozen dataclass


def test_country_tz_names_all_constructible():
    assert len(_COUNTRY_TZ) >= 40
    for country, tz in _COUNTRY_TZ.items():
        assert re.fullmatch(r"[A-Z]{2}", country), country
        ZoneInfo(tz)  # 非法 IANA 名会在此抛出


def test_multi_tz_countries_never_resolve_to_a_timezone():
    # 结构性不变量：多时区国不得出现在 _COUNTRY_TZ（否则国码就能推出时钟）
    for code in ("US", "CA", "AU", "ID", "RU", "BR", "MX", "KZ"):
        assert code in _MULTI_TZ_COUNTRIES, code
        assert code not in _COUNTRY_TZ, code
    assert not (_MULTI_TZ_COUNTRIES & set(_COUNTRY_TZ))
    assert "CN" not in _MULTI_TZ_COUNTRIES  # 全境统一 Asia/Shanghai，按单时区国处理


def test_phone_cc_country_table_shape_and_coverage():
    required = ("1", "7", "20", "27", "31", "32", "33", "34", "39", "44", "49",
                "52", "55", "60", "61", "62", "63", "64", "65", "66", "81", "82",
                "84", "86", "90", "91", "92", "94", "95", "852", "853", "855",
                "880", "886", "971", "977")
    missing = [cc for cc in required if cc not in PHONE_CC_COUNTRY]
    assert not missing, f"缺国码: {missing}"
    for cc, country in PHONE_CC_COUNTRY.items():
        assert cc.isdigit(), cc
        assert re.fullmatch(r"[A-Z]{2}", country), (cc, country)


def test_activity_prior_shape():
    assert len(ACTIVITY_PRIOR) == 24
    assert all(p > 0 for p in ACTIVITY_PRIOR)
    assert ACTIVITY_PRIOR.index(max(ACTIVITY_PRIOR)) == 21   # 晚间峰
    assert ACTIVITY_PRIOR.index(min(ACTIVITY_PRIOR)) == 4    # 凌晨谷


# ---------------------------------------------------------------------------
# 推断器 1：自述地点
# ---------------------------------------------------------------------------

def test_stated_place_chinese_city():
    c = infer_from_stated_place("我上个月搬到曼谷了")
    assert c is not None
    assert (c.tz_name, c.country, c.city_slug) == ("Asia/Bangkok", "TH", "bangkok")
    assert (c.source, c.confidence, c.trust) == ("stated_city", 0.92, TRUST_REPLACE)
    assert c.offset_hours == pytest.approx(7.0)


def test_stated_place_exact_name_and_english_and_region_word():
    assert infer_from_stated_place("Bangkok").city_slug == "bangkok"
    assert infer_from_stated_place("bangkok").tz_name == "Asia/Bangkok"
    assert infer_from_stated_place("Based in Manila these days").city_slug == "manila"
    assert infer_from_stated_place("我在加州").tz_name == "America/Los_Angeles"


def test_stated_place_unknown_and_garbage():
    assert infer_from_stated_place("亚特兰蒂斯") is None
    assert infer_from_stated_place("今天好累啊") is None
    for junk in JUNK:
        assert infer_from_stated_place(junk) is None


# ---------------------------------------------------------------------------
# 推断器 2：电话国码
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,country,tz", [
    ("+66 81 234 5678", "TH", "Asia/Bangkok"),
    ("639171234567@s.whatsapp.net", "PH", "Asia/Manila"),
    ("639171234567:12@s.whatsapp.net", "PH", "Asia/Manila"),
    ("+84 90 123 4567", "VN", "Asia/Ho_Chi_Minh"),
    ("+65 8123 4567", "SG", "Asia/Singapore"),
    ("+8613800138000", "CN", "Asia/Shanghai"),      # CN 全境单时区
    ("+886 912 345 678", "TW", "Asia/Taipei"),
    ("+44 7700 900123", "GB", "Europe/London"),
    ("+971 50 123 4567", "AE", "Asia/Dubai"),
    ("+91 98765 43210", "IN", "Asia/Kolkata"),
])
def test_phone_single_timezone_countries_give_replace_clock(raw, country, tz):
    c = infer_from_phone(raw)
    assert c is not None, raw
    assert (c.country, c.tz_name) == (country, tz)
    assert (c.source, c.confidence, c.trust) == ("phone_cc", 0.85, TRUST_REPLACE)
    assert c.is_fixed_offset is False


@pytest.mark.parametrize("raw,country", [
    ("+1 415 555 0123", "US"),
    ("+61 412 345 678", "AU"),
    ("+62 812 3456 789", "ID"),
    ("+7 912 345 6789", "RU"),
    ("+55 11 91234 5678", "BR"),
    ("+52 55 1234 5678", "MX"),
])
def test_phone_multi_timezone_countries_never_give_a_timezone(raw, country):
    """关键护栏：多时区国只出 country，绝不出 tz，且 trust 降到 advisory（不参与调度）。"""
    c = infer_from_phone(raw)
    assert c is not None, raw
    assert c.country == country
    assert c.tz_name == ""
    assert c.offset_hours == 0.0
    assert c.trust == TRUST_ADVISORY
    assert c.source == "phone_cc"


def test_phone_unknown_prefix_and_garbage():
    assert infer_from_phone("+999 111 2222") is None     # 未知国码
    assert infer_from_phone("12345") is None             # 太短，非 E.164
    assert infer_from_phone("0812345678") is None        # 国内格式，无国码信息
    assert infer_from_phone("1234567890123456789") is None  # 太长
    for junk in JUNK:
        assert infer_from_phone(junk) is None, junk


# ---------------------------------------------------------------------------
# 推断器 3：活跃度 MLE
# ---------------------------------------------------------------------------

def test_activity_infers_thailand_offset():
    c = infer_from_activity(_hist_from_local(_prior_shaped_day(), 7))
    assert c is not None
    assert c.tz_name == "UTC+07:00"
    assert c.offset_hours == pytest.approx(7.0)
    assert (c.source, c.trust) == ("behavior", TRUST_NARROW)
    assert c.country == "" and c.city_slug == ""
    assert 0.5 <= c.confidence <= 0.78


def test_activity_infers_california_offset():
    for true_off in (-7, -8):
        c = infer_from_activity(_hist_from_local(_prior_shaped_day(), true_off))
        assert c is not None, true_off
        assert int(c.offset_hours) in (-8, -7), c
        assert c.tz_name in ("UTC-08:00", "UTC-07:00")


def test_activity_accepts_raw_hour_sequence_equal_to_histogram():
    hist = _hist_from_local(_prior_shaped_day(), 7)
    raw = [h for h, n in hist.items() for _ in range(n)]
    assert infer_from_activity(raw) == infer_from_activity(hist)
    assert infer_from_activity(tuple(raw)) == infer_from_activity(hist)


def test_activity_confidence_never_reaches_explicit_tier():
    """行为推断上限必须 < 0.8：统计推断永远弱于显式信号（stated 0.92 / phone 0.85）。"""
    c = infer_from_activity(_hist_from_local(_prior_shaped_day(), 7))
    assert c.confidence < 0.8
    assert c.confidence == pytest.approx(0.78)  # 强信号打到上限即被夹住


def test_activity_rejects_insufficient_samples():
    small = {9: 3, 10: 3, 11: 3, 12: 3}   # 12 样本 < 默认 24
    assert infer_from_activity(small) is None
    # 门槛可调：放宽 min_samples 后同一份数据可用（说明拒绝确因样本量而非形状）
    assert infer_from_activity(small, min_samples=8) is not None


def test_activity_rejects_two_hour_concentration():
    """只集中在 2 个小时：对时区无信息却能凑出高 margin，必须被「不同小时数 ≥4」挡掉。"""
    assert infer_from_activity({3: 40, 4: 40}) is None
    assert infer_from_activity({3: 40, 4: 40, 5: 40}) is None   # 3 个小时也不够
    assert infer_from_activity([7] * 60) is None


def test_activity_rejects_uniform_no_information():
    assert infer_from_activity({h: 5 for h in range(24)}) is None
    assert infer_from_activity(list(range(24)) * 3) is None


def test_activity_rejects_when_margin_threshold_raised():
    hist = _hist_from_local(_prior_shaped_day(), 7)
    assert infer_from_activity(hist, min_margin=0.99) is None
    assert infer_from_activity(hist, min_margin=0.0) is not None


def test_activity_garbage_inputs():
    for junk in JUNK:
        assert infer_from_activity(junk) is None
    assert infer_from_activity({"x": "y", 99: 5, -3: 5, 8: "n"}) is None
    assert infer_from_activity([None, "a", 30, -1, 3.7]) is None


def test_activity_is_deterministic():
    hist = _hist_from_local(_prior_shaped_day(), 7)
    assert infer_from_activity(hist) == infer_from_activity(dict(reversed(list(hist.items()))))


@pytest.mark.parametrize("true_off,expected_tz", [(13, "UTC+13:00"), (-10, "UTC-10:00")])
def test_activity_handles_wraparound_equivalent_offsets(true_off, expected_tz):
    """候选区间 [-11,+14] 里 -11≡+13、+14≡-10（模 24 完全同解）。

    次优必须按**环形**距离排除邻居，否则「同一个解的另一个写法」会被当成次优 →
    margin 恒 0 → 新西兰夏令时/夏威夷用户永远推不出来。输出取该残差下人口占优的写法。
    """
    c = infer_from_activity(_hist_from_local(_prior_shaped_day(), true_off))
    assert c is not None
    assert c.tz_name == expected_tz
    assert c.offset_hours == pytest.approx(float(true_off))
    # 两种写法产出完全一致（确定性，不随字典序漂）
    assert c == infer_from_activity(_hist_from_local(_prior_shaped_day(), true_off - 24))


# ---------------------------------------------------------------------------
# 推断器 4：交叉验证
# ---------------------------------------------------------------------------

def test_corroborate_upgrades_behavior_with_matching_region():
    behavior = _behavior_clock(7)
    region = infer_from_phone("+66812345678")
    merged = corroborate(behavior, region, now=SUMMER_UTC)
    assert merged.tz_name == "Asia/Bangkok"        # 白拿真 IANA 名（DST 支持）
    assert merged.source == "behavior_corroborated"
    assert merged.confidence == 0.88
    assert merged.trust == TRUST_REPLACE
    assert merged.country == "TH"
    assert merged.is_fixed_offset is False


def test_corroborate_multi_timezone_region_does_not_upgrade():
    """behavior +07 × region US：美国城市偏移 -7/-4/-10 各不相同 → 歧义，不升格。"""
    behavior = _behavior_clock(7)
    merged = corroborate(behavior, infer_from_phone("+14155550123"), now=SUMMER_UTC)
    assert merged == behavior
    assert merged.trust == TRUST_NARROW
    assert merged.source == "behavior"


def test_corroborate_indonesia_ambiguous_and_offset_mismatch():
    # 印尼横跨 +7(雅加达)/+8(巴厘) → 即使 behavior=+7 也不升格
    assert corroborate(_behavior_clock(7), infer_from_language("id"), now=SUMMER_UTC).source == "behavior"
    # 偏移对不上：日本 +9 vs behavior +7
    jp = infer_from_phone("+819012345678")
    assert corroborate(_behavior_clock(7), jp, now=SUMMER_UTC).source == "behavior"


def test_corroborate_none_and_degenerate_paths():
    assert corroborate(None, infer_from_phone("+66812345678")) is None
    assert corroborate(None, None) is None
    b = _behavior_clock(7)
    assert corroborate(b, None) == b
    assert corroborate(b, _behavior_clock(7)) == b   # region 也是固定偏移 → 无真 IANA 名可用
    assert corroborate(b, "垃圾") == b               # type: ignore[arg-type]


def test_corroborate_via_language_region():
    merged = corroborate(_behavior_clock(7), infer_from_language("th"), now=SUMMER_UTC)
    assert merged.tz_name == "Asia/Bangkok"
    assert merged.trust == TRUST_REPLACE


# ---------------------------------------------------------------------------
# 推断器 5：语种默认
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang,country,tz", [
    ("th", "TH", "Asia/Bangkok"),
    ("vi", "VN", "Asia/Ho_Chi_Minh"),
    ("ko", "KR", "Asia/Seoul"),
    ("ja", "JP", "Asia/Tokyo"),
    ("zh", "CN", "Asia/Shanghai"),
    ("hi", "IN", "Asia/Kolkata"),
    ("tr", "TR", "Europe/Istanbul"),
    ("he", "IL", "Asia/Jerusalem"),
    ("de", "DE", "Europe/Berlin"),
])
def test_language_single_timezone_countries(lang, country, tz):
    c = infer_from_language(lang)
    assert c is not None, lang
    assert (c.country, c.tz_name) == (country, tz)
    assert (c.source, c.confidence, c.trust) == ("lang_default", 0.35, TRUST_ADVISORY)


@pytest.mark.parametrize("lang,country", [("id", "ID"), ("ru", "RU")])
def test_language_multi_timezone_country_only(lang, country):
    c = infer_from_language(lang)
    assert c.country == country
    assert c.tz_name == ""
    assert c.trust == TRUST_ADVISORY


@pytest.mark.parametrize("lang", ["en", "es", "pt", "fr", "en-US", "PT_BR"])
def test_language_lingua_francas_refuse_to_guess(lang):
    """跨洲通用语刻意不猜国家：猜错代价（节日全错 + 误导互证）大于收益。"""
    assert infer_from_language(lang) is None


def test_language_normalization_and_garbage():
    assert infer_from_language("zh-CN").country == "CN"
    assert infer_from_language("TH").country == "TH"
    assert infer_from_language(" ja_JP ").country == "JP"
    assert infer_from_language("xx") is None
    for junk in JUNK:
        assert infer_from_language(junk) is None


# ---------------------------------------------------------------------------
# resolve_user_clock：优先级 / 信息合并 / 计数
# ---------------------------------------------------------------------------

def test_resolve_stated_place_wins_everything():
    c = resolve_user_clock(
        stated_place="我在曼谷", phone="+14155550123",
        activity_hours=_hist_from_local(_prior_shaped_day(), -7), language="ja")
    assert c.source == "stated_city"
    assert c.tz_name == "Asia/Bangkok"
    assert dump_stats()["resolved_stated"] == 1


def test_resolve_phone_with_tz_beats_behavior():
    c = resolve_user_clock(
        phone="+66812345678",
        activity_hours=_hist_from_local(_prior_shaped_day(), -7), language="ja")
    assert c.source == "phone_cc"
    assert c.tz_name == "Asia/Bangkok"
    assert dump_stats()["resolved_phone"] == 1


def test_resolve_corroborated_path_via_language():
    c = resolve_user_clock(
        activity_hours=_hist_from_local(_prior_shaped_day(), 7),
        language="th", now=SUMMER_UTC)
    assert c.source == "behavior_corroborated"
    assert c.tz_name == "Asia/Bangkok"
    assert c.trust == TRUST_REPLACE
    assert c.country == "TH"
    assert dump_stats()["resolved_corroborated"] == 1


def test_resolve_behavior_only_stays_narrow():
    c = resolve_user_clock(activity_hours=_hist_from_local(_prior_shaped_day(), 7))
    assert c.source == "behavior"
    assert c.trust == TRUST_NARROW
    assert c.tz_name == "UTC+07:00"
    assert dump_stats()["resolved_behavior"] == 1


def test_resolve_backfills_country_onto_behavior_clock():
    """行为推断天生没国家；低优先级的 phone(仅国家) 补上 → 时钟与节日各取所长。"""
    c = resolve_user_clock(
        phone="+14155550123",
        activity_hours=_hist_from_local(_prior_shaped_day(), -7), now=SUMMER_UTC)
    assert c.source == "behavior"          # US 多时区 → 互证失败，仍是行为档
    assert c.trust == TRUST_NARROW         # trust/confidence 不因补 country 而变
    assert c.tz_name in ("UTC-07:00", "UTC-08:00")
    assert c.country == "US"               # 节日日历可用
    assert dump_stats()["resolved_behavior"] == 1


def test_resolve_phone_country_only_then_language():
    c = resolve_user_clock(phone="+14155550123", language="th")
    assert (c.source, c.country, c.tz_name) == ("phone_cc", "US", "")
    assert dump_stats()["resolved_phone"] == 1
    reset_stats_for_tests()
    c2 = resolve_user_clock(language="th")
    assert c2.source == "lang_default"
    assert dump_stats()["resolved_lang"] == 1


def test_resolve_unresolved_counts_and_garbage():
    assert resolve_user_clock() is None
    assert resolve_user_clock(stated_place="今天好累", phone="abc",
                              activity_hours=[1, 2], language="en") is None
    assert dump_stats()["unresolved"] == 2


# ---------------------------------------------------------------------------
# user_now 铁律 + DST
# ---------------------------------------------------------------------------

def test_user_now_never_returns_aware():
    clocks = [None, infer_from_stated_place("曼谷"), _behavior_clock(-8),
              infer_from_language("id"), infer_from_phone("+14155550123")]
    for clock in clocks:
        for when in (None, SUMMER_UTC, SUMMER_UTC.timestamp(), datetime(2026, 7, 15, 20, 0)):
            assert user_now(clock, when).tzinfo is None, (clock, when)


def test_user_now_fixed_offset_pseudo_zone():
    assert user_now(_behavior_clock(7), SUMMER_UTC).hour == 19       # 12:00Z + 7
    assert user_now(_behavior_clock(-8), SUMMER_UTC).hour == 4       # 12:00Z - 8
    dt = user_now(_behavior_clock(7), BKK_0612_UTC)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 28, 6, 12)


def test_user_now_iana_dst_boundaries():
    la = infer_from_stated_place("洛杉矶")
    assert la.tz_name == "America/Los_Angeles"
    s = user_now(la, SUMMER_UTC)
    assert (s.month, s.day, s.hour) == (7, 15, 5)     # PDT = -7
    w = user_now(la, WINTER_UTC)
    assert (w.month, w.day, w.hour) == (1, 15, 4)     # PST = -8
    bkk = infer_from_stated_place("曼谷")             # 泰国无 DST，两季一致 +7
    assert user_now(bkk, SUMMER_UTC).hour == 19
    assert user_now(bkk, WINTER_UTC).hour == 19


def test_user_now_unix_timestamp_input():
    bkk = infer_from_stated_place("曼谷")
    assert user_now(bkk, BKK_0612_UTC.timestamp()) == user_now(bkk, BKK_0612_UTC)


def test_user_now_none_clock_and_invalid_tz_fall_back_to_server():
    a = user_now(None)
    assert a.tzinfo is None
    assert abs((datetime.now() - a).total_seconds()) < 2.0
    bad = UserClock(tz_name="Mars/Olympus", offset_hours=3.0, source="stated_city",
                    confidence=0.9, country="", city_slug="", trust=TRUST_REPLACE)
    assert abs((datetime.now() - user_now(bad)).total_seconds()) < 2.0
    # 只有国家没时区（advisory）→ 服务器钟
    country_only = infer_from_phone("+14155550123")
    expect = SUMMER_UTC.astimezone().replace(tzinfo=None)
    assert user_now(country_only, SUMMER_UTC) == expect
    for junk in JUNK:
        assert user_now(infer_from_stated_place("曼谷"), junk).tzinfo is None


def test_user_local_hour_day_key_month_day():
    bkk = infer_from_stated_place("曼谷")
    assert user_local_hour(bkk, BKK_0612_UTC) == 6
    assert user_day_key(bkk, BKK_0612_UTC) == "20260728"
    assert user_month_day(bkk, BKK_0612_UTC) == "07-28"
    # 同一瞬间在服务器钟（UTC+8）与用户钟（+7）可能跨日 → 节日/日键必须按用户钟
    la = infer_from_stated_place("洛杉矶")
    assert user_day_key(la, BKK_0612_UTC) == "20260727"
    assert user_month_day(la, BKK_0612_UTC) == "07-27"
    assert 0 <= user_local_hour(None) <= 23
    assert re.fullmatch(r"\d{8}", user_day_key(None))
    assert re.fullmatch(r"\d{2}-\d{2}", user_month_day(None))


# ---------------------------------------------------------------------------
# user_time_line
# ---------------------------------------------------------------------------

def test_user_time_line_zh_exact():
    line = user_time_line(infer_from_stated_place("曼谷"), "zh", BKK_0612_UTC)
    assert line == (
        "【对方当地时间（内部事实）】对方那边现在约 2026-07-28 周二 06:12（清晨）。"
        "别报时间戳，只在自然相关时体现作息；这是推断值，不要当确定信息复述。"
    )


def test_user_time_line_en_and_narrow_clock():
    line = user_time_line(infer_from_stated_place("曼谷"), "en", BKK_0612_UTC)
    assert "2026-07-28" in line and "Tue" in line and "06:12" in line
    assert "early morning" in line
    assert "inference" in line
    # narrow（行为推断）仍进 prompt，只是时区是固定偏移伪名
    assert "06:12" in user_time_line(_behavior_clock(7), "zh", BKK_0612_UTC)


def test_user_time_line_suppressed_for_weak_signals():
    assert user_time_line(None, "zh", BKK_0612_UTC) == ""
    assert user_time_line(infer_from_language("th"), "zh", BKK_0612_UTC) == ""   # advisory
    assert user_time_line(infer_from_phone("+14155550123"), "zh", BKK_0612_UTC) == ""
    for junk in JUNK:
        assert isinstance(user_time_line(infer_from_stated_place("曼谷"), "zh", junk), str)


# ---------------------------------------------------------------------------
# in_quiet_hours 三档策略真值表（本模块的安全核心）
# ---------------------------------------------------------------------------

QUIET_START, QUIET_END = 23, 8       # 跨午夜窗口
SERVER_QUIET_HOUR = 3                # 服务器钟安静
SERVER_AWAKE_HOUR = 14               # 服务器钟清醒


def _clock_of(kind: str):
    return {
        "none": None,
        "advisory": infer_from_language("th"),          # 泰国 +7，但只是弱信号
        "narrow": _behavior_clock(7),
        "replace": infer_from_stated_place("曼谷"),     # 泰国 +7，显式信号
    }[kind]


@pytest.mark.parametrize("kind,server_hour,ts,expected", [
    # 无时钟 / advisory：完全的旧行为——只看服务器钟
    ("none", SERVER_QUIET_HOUR, TS_USER_AWAKE, True),
    ("none", SERVER_AWAKE_HOUR, TS_USER_QUIET, False),
    ("advisory", SERVER_QUIET_HOUR, TS_USER_AWAKE, True),
    ("advisory", SERVER_AWAKE_HOUR, TS_USER_QUIET, False),
    # narrow：服务器钟 or 用户钟安静 都算安静（只收窄，绝不新开）
    ("narrow", SERVER_QUIET_HOUR, TS_USER_AWAKE, True),
    ("narrow", SERVER_AWAKE_HOUR, TS_USER_QUIET, True),
    ("narrow", SERVER_QUIET_HOUR, TS_USER_QUIET, True),
    ("narrow", SERVER_AWAKE_HOUR, TS_USER_AWAKE, False),
    # replace：只看用户钟（服务器钟退场）
    ("replace", SERVER_QUIET_HOUR, TS_USER_AWAKE, False),
    ("replace", SERVER_AWAKE_HOUR, TS_USER_QUIET, True),
    ("replace", SERVER_QUIET_HOUR, TS_USER_QUIET, True),
    ("replace", SERVER_AWAKE_HOUR, TS_USER_AWAKE, False),
])
def test_in_quiet_hours_truth_table(kind, server_hour, ts, expected):
    assert in_quiet_hours(
        _clock_of(kind), ts, quiet_start=QUIET_START, quiet_end=QUIET_END,
        server_hour=server_hour) is expected


def test_narrow_only_narrows_never_opens_a_new_window():
    """显式钉死不变量：narrow 档在「服务器安静 + 用户清醒」时**仍安静**——
    错误的行为推断因此不可能造出新的凌晨骚扰。"""
    assert in_quiet_hours(_behavior_clock(7), TS_USER_AWAKE, quiet_start=23,
                          quiet_end=8, server_hour=SERVER_QUIET_HOUR) is True
    # 而同样场景下 replace 档可以发（显式信号有权让服务器钟退场）
    assert in_quiet_hours(infer_from_stated_place("曼谷"), TS_USER_AWAKE,
                          quiet_start=23, quiet_end=8,
                          server_hour=SERVER_QUIET_HOUR) is False


def test_in_quiet_hours_cross_midnight_and_normal_window():
    bkk = infer_from_stated_place("曼谷")   # replace 档，只看用户钟
    # 跨午夜 23..8：曼谷 04:00 在窗内、14:00 在窗外
    assert in_quiet_hours(bkk, TS_USER_QUIET, quiet_start=23, quiet_end=8, server_hour=12) is True
    assert in_quiet_hours(bkk, TS_USER_AWAKE, quiet_start=23, quiet_end=8, server_hour=12) is False
    # 普通窗口 12..18：曼谷 14:00 在窗内
    assert in_quiet_hours(bkk, TS_USER_AWAKE, quiet_start=12, quiet_end=18, server_hour=3) is True
    assert in_quiet_hours(bkk, TS_USER_QUIET, quiet_start=12, quiet_end=18, server_hour=3) is False
    # 边界闭开：[start, end)
    assert in_quiet_hours(None, TS_USER_AWAKE, quiet_start=14, quiet_end=18, server_hour=14) is True
    assert in_quiet_hours(None, TS_USER_AWAKE, quiet_start=14, quiet_end=18, server_hour=18) is False


def test_in_quiet_hours_start_equals_end_means_no_quiet_period():
    for kind in ("none", "advisory", "narrow", "replace"):
        assert in_quiet_hours(_clock_of(kind), TS_USER_QUIET, quiet_start=0,
                              quiet_end=0, server_hour=3) is False
    assert in_quiet_hours(None, TS_USER_QUIET, quiet_start=23, quiet_end=23, server_hour=23) is False


def test_in_quiet_hours_default_server_hour_uses_localtime():
    ts = TS_USER_QUIET
    expected_hour = time.localtime(ts).tm_hour
    got = in_quiet_hours(None, ts, quiet_start=expected_hour,
                         quiet_end=(expected_hour + 1) % 24)
    assert got is True
    assert in_quiet_hours(None, ts, quiet_start=(expected_hour + 2) % 24,
                          quiet_end=(expected_hour + 3) % 24) is False


def test_in_quiet_hours_stats_and_garbage():
    in_quiet_hours(_behavior_clock(7), TS_USER_QUIET, quiet_start=23, quiet_end=8,
                   server_hour=SERVER_AWAKE_HOUR)      # 收窄生效
    in_quiet_hours(_behavior_clock(7), TS_USER_AWAKE, quiet_start=23, quiet_end=8,
                   server_hour=SERVER_AWAKE_HOUR)      # 两钟都清醒，不算收窄
    in_quiet_hours(infer_from_stated_place("曼谷"), TS_USER_QUIET, quiet_start=23,
                   quiet_end=8, server_hour=SERVER_AWAKE_HOUR)
    stats = dump_stats()
    assert stats["quiet_narrowed"] == 1
    assert stats["quiet_replaced"] == 1
    for junk in JUNK:
        assert isinstance(
            in_quiet_hours(_behavior_clock(7), junk, quiet_start=23, quiet_end=8,
                           server_hour=3), bool)


# ---------------------------------------------------------------------------
# schedule_clock / shift_hours_to_clock
# ---------------------------------------------------------------------------

def test_schedule_clock_uses_user_clock_for_replace_and_narrow():
    ts = BKK_0612_UTC.timestamp()
    for clock in (infer_from_stated_place("曼谷"), _behavior_clock(7)):
        hour, day_key, offset = schedule_clock(clock, ts)
        assert (hour, day_key) == (6, "20260728")
        assert offset == pytest.approx(7.0)


def test_schedule_clock_falls_back_to_server_for_weak_signals():
    ts = BKK_0612_UTC.timestamp()
    server_local = BKK_0612_UTC.astimezone().replace(tzinfo=None)
    for clock in (None, infer_from_language("th"), infer_from_phone("+14155550123")):
        hour, day_key, offset = schedule_clock(clock, ts)
        assert hour == server_local.hour
        assert day_key == server_local.strftime("%Y%m%d")
        assert offset == 0.0


def test_schedule_clock_offset_is_dst_aware_and_garbage_safe():
    la = infer_from_stated_place("洛杉矶")
    assert schedule_clock(la, SUMMER_UTC.timestamp())[2] == pytest.approx(-7.0)
    assert schedule_clock(la, WINTER_UTC.timestamp())[2] == pytest.approx(-8.0)
    for junk in JUNK:
        hour, day_key, offset = schedule_clock(la, junk)  # type: ignore[arg-type]
        assert 0 <= hour <= 23 and re.fullmatch(r"\d{8}", day_key)
        assert isinstance(offset, float)


def test_shift_hours_to_clock_maps_to_local_hours():
    assert shift_hours_to_clock([0, 1, 16, 23], _behavior_clock(7)) == [7, 8, 23, 6]
    assert shift_hours_to_clock([0, 12], _behavior_clock(-8)) == [16, 4]
    assert shift_hours_to_clock([2], infer_from_stated_place("曼谷")) == [9]
    # 半小时时区向下取整到所在整点（印度 +5:30）
    assert shift_hours_to_clock([3], infer_from_phone("+919876543210")) == [8]


def test_shift_hours_to_clock_none_equals_server_local():
    server_off = int(_server_offset_hours())
    hours = [0, 5, 13, 23]
    assert shift_hours_to_clock(hours, None) == [(h + server_off) % 24 for h in hours]
    # 只有国家没时区的弱信号 → 同样退回服务器本地（不敢当 UTC 用）
    assert shift_hours_to_clock(hours, infer_from_phone("+14155550123")) == \
        shift_hours_to_clock(hours, None)


def test_shift_hours_to_clock_garbage():
    clock = _behavior_clock(7)
    assert shift_hours_to_clock([25, -3, "a", None, 3.9], clock) == [10]  # 仅 3 合法
    assert shift_hours_to_clock({"0": 1}, clock) == []
    assert shift_hours_to_clock("0123", clock) == []
    for junk in JUNK:
        assert isinstance(shift_hours_to_clock(junk, clock), list)


# ---------------------------------------------------------------------------
# 观测
# ---------------------------------------------------------------------------

def test_dump_stats_keys_and_reset():
    expected = {"resolved_stated", "resolved_phone", "resolved_behavior",
                "resolved_corroborated", "resolved_lang", "unresolved",
                "quiet_narrowed", "quiet_replaced"}
    assert set(dump_stats()) == expected
    assert all(v == 0 for v in dump_stats().values())
    resolve_user_clock(stated_place="曼谷")
    resolve_user_clock()
    snapshot = dump_stats()
    assert snapshot["resolved_stated"] == 1 and snapshot["unresolved"] == 1
    snapshot["resolved_stated"] = 999            # 返回的是拷贝，改不动内部状态
    assert dump_stats()["resolved_stated"] == 1
    reset_stats_for_tests()
    assert all(v == 0 for v in dump_stats().values())


def test_no_public_function_raises_on_garbage():
    """热路径铁律：任何垃圾输入都不得抛，最坏也只能退化成等价旧行为。"""
    for junk in JUNK:
        infer_from_stated_place(junk)
        infer_from_phone(junk)
        infer_from_activity(junk)
        infer_from_language(junk)
        corroborate(junk, junk)  # type: ignore[arg-type]
        resolve_user_clock(stated_place=junk, phone=junk, activity_hours=junk,
                           language=junk, min_samples=junk, min_margin=junk)  # type: ignore[arg-type]
        user_now(junk, junk)     # type: ignore[arg-type]
        user_local_hour(junk, junk)  # type: ignore[arg-type]
        user_day_key(junk, junk)     # type: ignore[arg-type]
        user_month_day(junk, junk)   # type: ignore[arg-type]
        user_time_line(junk, junk, junk)  # type: ignore[arg-type]
        in_quiet_hours(junk, junk, quiet_start=junk, quiet_end=junk, server_hour=junk)  # type: ignore[arg-type]
        schedule_clock(junk, junk)   # type: ignore[arg-type]
        shift_hours_to_clock(junk, junk)  # type: ignore[arg-type]
    assert math.isfinite(1.0)  # 走到这里即证明全程无异常
