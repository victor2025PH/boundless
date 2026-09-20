"""locale_holidays 离线门禁：6 种规则的金标日期 / 优雅降级 / 事实块文案 / 数据自检。

本文件承担两件事：
1. **规则引擎正确性**——每种 rule.type 各钉金标日期（含农历/节气经 lunar_python 实测确认）。
2. **数据防腐**——遍历内置 YAML 断言 key 唯一/双语齐平/scope 与 type 在白名单/table 覆盖
   年限/lang_defaults 指向真实国家。节日数据腐烂是无声的（不报错，只是静默发错日子或
   什么都不发），所以必须靠门禁替运营盯着。
"""

from __future__ import annotations

import builtins
from datetime import date, datetime

import pytest

from src.companion import locale_holidays as lh


@pytest.fixture(autouse=True)
def _clean():
    lh.clear_caches_for_tests()
    yield
    lh.clear_caches_for_tests()


def _d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


# 一个自建小日历：排序/未知国家等结构性断言不该依赖真实数据的具体内容。
_TOY_CAL = {
    "version": 1,
    "lang_defaults": {"zz": "ZZ", "en": ""},
    "countries": {
        "ZZ": [
            {"key": "z_cultural", "name_zh": "民俗节", "name_en": "Cultural Day",
             "scope": "cultural", "greet": True,
             "rule": {"type": "fixed", "month": 6, "day": 1}},
            {"key": "z_public_b", "name_zh": "法定乙", "name_en": "Public B",
             "scope": "public", "greet": False,
             "rule": {"type": "fixed", "month": 6, "day": 1}},
            {"key": "z_religious", "name_zh": "宗教节", "name_en": "Religious Day",
             "scope": "religious", "greet": True,
             "rule": {"type": "fixed", "month": 6, "day": 1}},
            {"key": "z_public_a", "name_zh": "法定甲", "name_en": "Public A",
             "scope": "public", "greet": True,
             "rule": {"type": "fixed", "month": 6, "day": 1}},
        ],
    },
}


# ---------------------------------------------------------------------------
# 规则 1：fixed
# ---------------------------------------------------------------------------

def test_rule_fixed_single_and_multi_day():
    assert lh.resolve_rule({"type": "fixed", "month": 1, "day": 1}, 2026) == [_d(2026, 1, 1)]
    # 宋干节：公历 4/13 起 3 天
    assert lh.resolve_rule({"type": "fixed", "month": 4, "day": 13, "days": 3}, 2026) == [
        _d(2026, 4, 13), _d(2026, 4, 14), _d(2026, 4, 15),
    ]
    # days 跨年溢出：基准日在 year 内，展开可落到次年（查询层的三年扫描负责收口）
    assert lh.resolve_rule({"type": "fixed", "month": 12, "day": 31, "days": 3}, 2026) == [
        _d(2026, 12, 31), _d(2027, 1, 1), _d(2027, 1, 2),
    ]


def test_rule_fixed_invalid_month_day_returns_empty():
    assert lh.resolve_rule({"type": "fixed", "month": 13, "day": 1}, 2026) == []
    assert lh.resolve_rule({"type": "fixed", "month": 2, "day": 30}, 2026) == []
    assert lh.resolve_rule({"type": "fixed", "month": 2, "day": 29}, 2026) == []  # 非闰年
    assert lh.resolve_rule({"type": "fixed", "month": 2, "day": 29}, 2028) == [_d(2028, 2, 29)]


# ---------------------------------------------------------------------------
# 规则 2：lunar（金标经 lunar_python 实测确认）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_lunar_golden_dates_2026():
    """2026 农历节日公历日（lunar_python 实测值）。"""
    assert lh.resolve_rule({"type": "lunar", "month": 1, "day": 1}, 2026) == [_d(2026, 2, 17)]
    assert lh.resolve_rule({"type": "lunar", "month": 1, "day": 15}, 2026) == [_d(2026, 3, 3)]
    assert lh.resolve_rule({"type": "lunar", "month": 5, "day": 5}, 2026) == [_d(2026, 6, 19)]
    assert lh.resolve_rule({"type": "lunar", "month": 7, "day": 7}, 2026) == [_d(2026, 8, 19)]
    assert lh.resolve_rule({"type": "lunar", "month": 8, "day": 15}, 2026) == [_d(2026, 9, 25)]
    assert lh.resolve_rule({"type": "lunar", "month": 9, "day": 9}, 2026) == [_d(2026, 10, 18)]


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_lunar_golden_dates_2027_and_2030():
    assert lh.resolve_rule({"type": "lunar", "month": 1, "day": 1}, 2027) == [_d(2027, 2, 6)]
    assert lh.resolve_rule({"type": "lunar", "month": 8, "day": 15}, 2027) == [_d(2027, 9, 15)]
    assert lh.resolve_rule({"type": "lunar", "month": 1, "day": 1}, 2030) == [_d(2030, 2, 3)]
    assert lh.resolve_rule({"type": "lunar", "month": 1, "day": 15}, 2030) == [_d(2030, 2, 17)]
    assert lh.resolve_rule({"type": "lunar", "month": 5, "day": 5}, 2030) == [_d(2030, 6, 5)]
    assert lh.resolve_rule({"type": "lunar", "month": 8, "day": 15}, 2030) == [_d(2030, 9, 12)]


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_lunar_crosses_solar_year_boundary():
    """农历腊月落进**下一个**公历年——正是「必须扫三个农历年」的理由。

    农历 2026 腊月初八 = 公历 2027-01-15；故公历 2026 年的腊八来自农历 2025 年。
    """
    got_2027 = lh.resolve_rule({"type": "lunar", "month": 12, "day": 8}, 2027)
    assert got_2027 == [_d(2027, 1, 15)]
    got_2026 = lh.resolve_rule({"type": "lunar", "month": 12, "day": 8}, 2026)
    assert len(got_2026) == 1 and got_2026[0].year == 2026


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_lunar_negative_day_is_month_last_day():
    """day=-1 ＝该农历月末日（除夕）：腊月可能廿九也可能三十，写死 30 会直接抛错。"""
    # 农历 2025 腊月只有 29 天，廿九 = 公历 2026-02-16（春节 02-17 的前一天）
    assert lh.resolve_rule({"type": "lunar", "month": 12, "day": -1}, 2026) == [_d(2026, 2, 16)]
    assert lh.resolve_rule({"type": "lunar", "month": 12, "day": -1}, 2027) == [_d(2027, 2, 5)]
    # 除夕必须恰好落在春节前一天（这条不依赖硬编码日期，换年份也成立）
    for year in (2026, 2027, 2030, 2033):
        eve_y = lh.resolve_rule({"type": "lunar", "month": 12, "day": -1}, year)
        spring_y = lh.resolve_rule({"type": "lunar", "month": 1, "day": 1}, year)
        assert len(eve_y) == 1 and len(spring_y) == 1, year
        assert (spring_y[0] - eve_y[0]).days == 1, year
    # -2 ＝倒数第二天，必在 -1 之前一天
    eve = lh.resolve_rule({"type": "lunar", "month": 12, "day": -1}, 2026)[0]
    eve2 = lh.resolve_rule({"type": "lunar", "month": 12, "day": -2}, 2026)[0]
    assert (eve - eve2).days == 1


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_lunar_days_expansion():
    got = lh.resolve_rule({"type": "lunar", "month": 1, "day": 1, "days": 3}, 2026)
    assert got == [_d(2026, 2, 17), _d(2026, 2, 18), _d(2026, 2, 19)]


# ---------------------------------------------------------------------------
# 规则 3：nth_weekday
# ---------------------------------------------------------------------------

def test_rule_nth_weekday_golden_pins():
    # 美国感恩节：11 月第 4 个周四（weekday=3）
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 11, "weekday": 3, "nth": 4}, 2026,
    ) == [_d(2026, 11, 26)]
    # 加拿大感恩节：10 月第 2 个周一（weekday=0）
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 10, "weekday": 0, "nth": 2}, 2026,
    ) == [_d(2026, 10, 12)]
    # 母亲节：5 月第 2 个周日（weekday=6）
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 5, "weekday": 6, "nth": 2}, 2026,
    ) == [_d(2026, 5, 10)]
    # 结果必须真是那个星期几
    assert _d(2026, 11, 26).weekday() == 3
    assert _d(2026, 10, 12).weekday() == 0
    assert _d(2026, 5, 10).weekday() == 6


def test_rule_nth_weekday_negative_counts_from_end():
    """nth 为负＝倒数第 N 个。5 月最后一个周一（美国阵亡将士纪念日）2026 = 05-25。"""
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 5, "weekday": 0, "nth": -1}, 2026,
    ) == [_d(2026, 5, 25)]
    # 倒数第二个周一 = 05-18，正好差 7 天
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 5, "weekday": 0, "nth": -2}, 2026,
    ) == [_d(2026, 5, 18)]
    # 5 月最后一个周日（法国母亲节）2026 = 05-31
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 5, "weekday": 6, "nth": -1}, 2026,
    ) == [_d(2026, 5, 31)]
    # 8 月最后一个周一（英国夏季银行假日）2026 = 08-31
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 8, "weekday": 0, "nth": -1}, 2026,
    ) == [_d(2026, 8, 31)]


def test_rule_nth_weekday_out_of_range_and_bad_input():
    # 2 月不可能有第 5 个周一
    assert lh.resolve_rule({"type": "nth_weekday", "month": 2, "weekday": 0, "nth": 5}, 2026) == []
    assert lh.resolve_rule({"type": "nth_weekday", "month": 5, "weekday": 0, "nth": 0}, 2026) == []
    assert lh.resolve_rule({"type": "nth_weekday", "month": 13, "weekday": 0, "nth": 1}, 2026) == []
    assert lh.resolve_rule({"type": "nth_weekday", "month": 5, "weekday": 7, "nth": 1}, 2026) == []
    # 5 月一定有第 5 个周日（2026-05-31）
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 5, "weekday": 6, "nth": 5}, 2026,
    ) == [_d(2026, 5, 31)]


# ---------------------------------------------------------------------------
# 规则 4：easter_offset
# ---------------------------------------------------------------------------

def test_rule_easter_offset_gregorian_golden():
    """西方复活节 2026 = 04-05；据此 Good Friday=04-03、Easter Monday=04-06。"""
    assert lh.resolve_rule({"type": "easter_offset", "offset": 0}, 2026) == [_d(2026, 4, 5)]
    assert lh.resolve_rule({"type": "easter_offset", "offset": -2}, 2026) == [_d(2026, 4, 3)]
    assert lh.resolve_rule({"type": "easter_offset", "offset": 1}, 2026) == [_d(2026, 4, 6)]
    # 英国 Mothering Sunday = 复活节前 21 天
    assert lh.resolve_rule({"type": "easter_offset", "offset": -21}, 2026) == [_d(2026, 3, 15)]
    # 升天节 +39 / 圣灵降临节星期一 +50
    assert lh.resolve_rule({"type": "easter_offset", "offset": 39}, 2026) == [_d(2026, 5, 14)]
    assert lh.resolve_rule({"type": "easter_offset", "offset": 50}, 2026) == [_d(2026, 5, 25)]
    # 复活节永远是周日
    for year in (2026, 2027, 2028, 2030, 2033):
        got = lh.resolve_rule({"type": "easter_offset", "offset": 0}, year)
        assert len(got) == 1 and got[0].weekday() == 6, year


def test_rule_easter_offset_julian_is_orthodox_not_western():
    """东正教复活节走儒略历：2026-04-12，与西方 04-05 差一周——混用就是发错日子。"""
    assert lh.resolve_rule(
        {"type": "easter_offset", "calendar": "julian", "offset": 0}, 2026,
    ) == [_d(2026, 4, 12)]
    assert lh.resolve_rule(
        {"type": "easter_offset", "calendar": "orthodox", "offset": 0}, 2027,
    ) == [_d(2027, 5, 2)]
    west = lh.resolve_rule({"type": "easter_offset", "offset": 0}, 2026)[0]
    east = lh.resolve_rule({"type": "easter_offset", "calendar": "julian", "offset": 0}, 2026)[0]
    assert east != west
    # 谢肉节：东正教复活节前 55 天起一周
    week = lh.resolve_rule(
        {"type": "easter_offset", "calendar": "julian", "offset": -55, "days": 7}, 2026)
    assert len(week) == 7 and week[0] == _d(2026, 2, 16) and week[-1] == _d(2026, 2, 22)


# ---------------------------------------------------------------------------
# 规则 5：table
# ---------------------------------------------------------------------------

_TABLE_RULE = {"type": "table", "dates": {2026: "03-20", 2027: "03-10", 2029: "02-15"}}


def test_rule_table_hits_listed_years():
    assert lh.resolve_rule(_TABLE_RULE, 2026) == [_d(2026, 3, 20)]
    assert lh.resolve_rule(_TABLE_RULE, 2027) == [_d(2027, 3, 10)]
    assert lh.resolve_rule(_TABLE_RULE, 2029) == [_d(2029, 2, 15)]


def test_rule_table_off_table_year_returns_empty_never_guesses():
    """表尽头之后**节日消失**，绝不外推——发错开斋节比没节日严重得多。"""
    assert lh.resolve_rule(_TABLE_RULE, 2028) == []   # 表里刻意缺这一年
    assert lh.resolve_rule(_TABLE_RULE, 2040) == []
    assert lh.resolve_rule(_TABLE_RULE, 2000) == []


def test_rule_table_string_keys_lists_and_bad_entries():
    # YAML 可能把年份读成字符串键
    assert lh.resolve_rule({"type": "table", "dates": {"2026": "03-20"}}, 2026) == [_d(2026, 3, 20)]
    # 同一公历年两次（伊斯兰年比公历短 ~11 天，2033 年就有两个开斋节）
    assert lh.resolve_rule(
        {"type": "table", "dates": {2033: ["01-03", "12-23"]}}, 2033,
    ) == [_d(2033, 1, 3), _d(2033, 12, 23)]
    # 全日期形式
    assert lh.resolve_rule({"type": "table", "dates": {2026: "2026-03-20"}}, 2026) == [_d(2026, 3, 20)]
    # 单条写坏只丢这一条，不连坐整个规则
    assert lh.resolve_rule(
        {"type": "table", "dates": {2026: ["03-20", "not-a-date", "13-99"]}}, 2026,
    ) == [_d(2026, 3, 20)]
    assert lh.resolve_rule({"type": "table", "dates": "oops"}, 2026) == []
    assert lh.resolve_rule({"type": "table"}, 2026) == []


def test_rule_table_days_expansion():
    got = lh.resolve_rule({"type": "table", "dates": {2026: "03-20"}, "days": 3}, 2026)
    assert got == [_d(2026, 3, 20), _d(2026, 3, 21), _d(2026, 3, 22)]


# ---------------------------------------------------------------------------
# 规则 6：jieqi
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_jieqi_golden_dates():
    """节气公历日逐年 ±1 天漂，写死日期会年年错一次（2026 实测值）。"""
    assert lh.resolve_rule({"type": "jieqi", "name": "清明"}, 2026) == [_d(2026, 4, 5)]
    assert lh.resolve_rule({"type": "jieqi", "name": "春分"}, 2026) == [_d(2026, 3, 20)]
    assert lh.resolve_rule({"type": "jieqi", "name": "秋分"}, 2026) == [_d(2026, 9, 23)]
    # 冬至在年末，在 lunar_python 的表里以拼音键出现——键名归一必须覆盖到
    assert lh.resolve_rule({"type": "jieqi", "name": "冬至"}, 2026) == [_d(2026, 12, 22)]
    assert lh.resolve_rule({"type": "jieqi", "name": "冬至"}, 2027) == [_d(2027, 12, 22)]
    # 逐年确实会漂：2027 春分 03-21 ≠ 2026 的 03-20
    assert lh.resolve_rule({"type": "jieqi", "name": "春分"}, 2027) == [_d(2027, 3, 21)]


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_rule_jieqi_每年恰好一个_and_bad_name():
    for name in ("清明", "冬至", "春分", "秋分", "立春", "夏至"):
        for year in (2026, 2030, 2033):
            got = lh.resolve_rule({"type": "jieqi", "name": name}, year)
            assert len(got) == 1 and got[0].year == year, (name, year)
    assert lh.resolve_rule({"type": "jieqi", "name": "不存在的节气"}, 2026) == []
    assert lh.resolve_rule({"type": "jieqi", "name": ""}, 2026) == []
    # 异体字别名
    assert lh.resolve_rule({"type": "jieqi", "name": "驚蟄"}, 2026) == \
        lh.resolve_rule({"type": "jieqi", "name": "惊蛰"}, 2026)


# ---------------------------------------------------------------------------
# resolve_rule 的总体健壮性
# ---------------------------------------------------------------------------

def test_resolve_rule_never_raises_on_garbage():
    for bad in (None, "", 42, [], {}, {"type": "nope"}, {"type": "fixed"},
                {"type": "fixed", "month": "x", "day": 1},
                {"type": "lunar"}, {"type": "nth_weekday"},
                {"type": "easter_offset", "offset": "x"}):
        assert lh.resolve_rule(bad, 2026) == [], bad
    assert lh.resolve_rule({"type": "fixed", "month": 1, "day": 1}, "nope") == []


def test_resolve_rule_days_is_clamped():
    got = lh.resolve_rule({"type": "fixed", "month": 1, "day": 1, "days": 999}, 2026)
    assert len(got) == lh._MAX_RULE_DAYS
    assert lh.resolve_rule({"type": "fixed", "month": 1, "day": 1, "days": 0}, 2026) == [_d(2026, 1, 1)]
    assert lh.resolve_rule({"type": "fixed", "month": 1, "day": 1, "days": -5}, 2026) == [_d(2026, 1, 1)]


# ---------------------------------------------------------------------------
# 缺 lunar_python 时的优雅降级
# ---------------------------------------------------------------------------

def test_missing_lunar_python_degrades_lunar_and_jieqi_only(monkeypatch):
    """让 ``import lunar_python`` 真的失败：农历/节气返回 []，其余规则完全不受影响。"""
    monkeypatch.setitem(lh._LUNAR_STATE, "tried", False)
    monkeypatch.setitem(lh._LUNAR_STATE, "Lunar", None)
    monkeypatch.setitem(lh._LUNAR_STATE, "Solar", None)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "lunar_python" or str(name).startswith("lunar_python."):
            raise ImportError("simulated missing lunar_python")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    lh.clear_caches_for_tests()

    assert lh.lunar_available() is False
    # 农历/节气：静默返回空，不抛
    assert lh.resolve_rule({"type": "lunar", "month": 1, "day": 1}, 2026) == []
    assert lh.resolve_rule({"type": "lunar", "month": 12, "day": -1}, 2026) == []
    assert lh.resolve_rule({"type": "jieqi", "name": "清明"}, 2026) == []
    # 其余四种照常工作
    assert lh.resolve_rule({"type": "fixed", "month": 4, "day": 13, "days": 3}, 2026) == [
        _d(2026, 4, 13), _d(2026, 4, 14), _d(2026, 4, 15)]
    assert lh.resolve_rule(
        {"type": "nth_weekday", "month": 11, "weekday": 3, "nth": 4}, 2026) == [_d(2026, 11, 26)]
    assert lh.resolve_rule({"type": "easter_offset", "offset": -2}, 2026) == [_d(2026, 4, 3)]
    assert lh.resolve_rule(_TABLE_RULE, 2026) == [_d(2026, 3, 20)]
    # 整链仍可用：美国感恩节照样查得到（美国没有农历节日）
    assert [h.key for h in lh.holidays_on(_d(2026, 11, 26), "US")] == ["us_thanksgiving"]
    # 中国春节查不到，但**不抛**且当天其他节日不受影响
    assert lh.holidays_on(_d(2026, 2, 17), "CN") == []
    assert [h.key for h in lh.holidays_on(_d(2026, 1, 1), "CN")] == ["cn_new_year"]


# ---------------------------------------------------------------------------
# country_for_language
# ---------------------------------------------------------------------------

def test_country_for_language_basic():
    assert lh.country_for_language("th") == "TH"
    assert lh.country_for_language("vi") == "VN"
    assert lh.country_for_language("ja") == "JP"
    assert lh.country_for_language("zh") == "CN"
    assert lh.country_for_language("he") == "IL"
    assert lh.country_for_language("TH") == "TH"          # 大小写不敏感
    assert lh.country_for_language("  th  ") == "TH"      # 空白容错


def test_country_for_language_pan_continental_deliberately_blank():
    """en/es/pt/fr 刻意不猜：猜错（给美国人发西班牙国庆）代价远大于猜对的收益。"""
    for lang in ("en", "es", "pt", "fr", "EN", "Fr"):
        assert lh.country_for_language(lang) == "", lang


def test_country_for_language_unknown_and_garbage():
    for bad in ("xx", "zzz", "", None, 0, [], {}, "  "):
        assert lh.country_for_language(bad) == "", bad


def test_country_for_language_explicit_region_subtag_wins():
    """显式区域子标签是调用方给的**数据**，不是猜测，故优先于语种默认。"""
    assert lh.country_for_language("zh-HK") == "HK"
    assert lh.country_for_language("zh_TW") == "TW"
    assert lh.country_for_language("en-US") == "US"
    assert lh.country_for_language("en-AU") == "AU"
    # 未策展的区域码不采纳，回落语种默认（en → ""）
    assert lh.country_for_language("en-XX") == ""
    # 有区域但语种默认更弱时仍取区域
    assert lh.country_for_language("pt-BR") == ""   # BR 未策展 → 回落 pt 的空值


# ---------------------------------------------------------------------------
# holidays_on
# ---------------------------------------------------------------------------

def test_holidays_on_real_calendar_golden():
    assert [h.key for h in lh.holidays_on(_d(2026, 11, 26), "US")] == ["us_thanksgiving"]
    assert [h.key for h in lh.holidays_on(_d(2026, 10, 12), "CA")] == ["ca_thanksgiving"]
    assert [h.key for h in lh.holidays_on(_d(2026, 4, 3), "CA")] == ["ca_good_friday"]
    assert [h.key for h in lh.holidays_on(_d(2026, 4, 5), "CA")] == ["ca_easter"]
    assert [h.key for h in lh.holidays_on(_d(2026, 5, 25), "US")] == ["us_memorial_day"]
    assert [h.key for h in lh.holidays_on(_d(2026, 4, 12), "RU")] == ["ru_orthodox_easter"]
    assert [h.key for h in lh.holidays_on(_d(2026, 3, 20), "ID")] == ["id_eid_al_fitr"]
    assert [h.key for h in lh.holidays_on(_d(2026, 9, 12), "IL")] == ["il_rosh_hashanah"]
    # 宋干节三天都命中同一个节日
    for day in (13, 14, 15):
        assert [h.key for h in lh.holidays_on(_d(2026, 4, day), "TH")] == ["th_songkran"]
    assert lh.holidays_on(_d(2026, 4, 16), "TH") == []


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_holidays_on_lunar_backed_entries():
    assert [h.key for h in lh.holidays_on(_d(2026, 2, 17), "CN")] == ["cn_spring_festival"]
    assert [h.key for h in lh.holidays_on(_d(2026, 2, 16), "CN")] == ["cn_chuxi"]
    assert [h.key for h in lh.holidays_on(_d(2026, 2, 17), "VN")] == ["vn_tet"]
    assert [h.key for h in lh.holidays_on(_d(2026, 2, 17), "KR")] == ["kr_seollal"]
    assert [h.key for h in lh.holidays_on(_d(2026, 9, 25), "CN")] == ["cn_mid_autumn"]
    assert [h.key for h in lh.holidays_on(_d(2026, 9, 25), "KR")] == ["kr_chuseok"]
    assert [h.key for h in lh.holidays_on(_d(2026, 12, 22), "CN")] == ["cn_dongzhi"]
    # 越南 Tết 与中国春节同一天、同规则，但是**两条独立条目**（文化归属不同）
    cn = lh.holidays_on(_d(2026, 2, 17), "CN")[0]
    vn = lh.holidays_on(_d(2026, 2, 17), "VN")[0]
    assert cn.key != vn.key and cn.name_zh != vn.name_zh
    assert cn.country == "CN" and vn.country == "VN"


def test_holidays_on_sorted_public_then_religious_then_cultural():
    got = lh.holidays_on(_d(2026, 6, 1), "ZZ", calendar=_TOY_CAL)
    assert [h.key for h in got] == ["z_public_a", "z_public_b", "z_religious", "z_cultural"]
    assert [h.scope for h in got] == ["public", "public", "religious", "cultural"]


def test_holidays_on_real_multi_holiday_days_are_sorted():
    """真实日历里同一天撞多节的情形也必须有序（数据一变就先在这里红）。"""
    cal = lh.load_calendar()
    order = {s: i for i, s in enumerate(lh.SCOPES)}
    checked = 0
    for country in cal.get("countries", {}):
        for year in range(2026, 2034):
            for day, items in lh._index_for(str(country), year).items():
                if len(items) < 2:
                    continue
                checked += 1
                ranks = [(order[h.scope], h.key) for h in items]
                assert ranks == sorted(ranks), f"{country} {day} 排序不稳定"
    # 防本用例悄悄退化成空转：真实日历里必然有撞日（如开斋节遇上阿拉伯母亲节）
    assert checked >= 5, f"只检到 {checked} 个撞日，本用例可能已失去意义"


def test_holidays_on_unknown_country_and_bad_input():
    assert lh.holidays_on(_d(2026, 1, 1), "ZZ") == []       # 内置日历没有 ZZ
    assert lh.holidays_on(_d(2026, 1, 1), "") == []
    assert lh.holidays_on(_d(2026, 1, 1), None) == []
    assert lh.holidays_on(_d(2026, 1, 1), "USA") == []      # 必须是 alpha-2
    assert lh.holidays_on(_d(2026, 1, 1), 42) == []
    assert lh.holidays_on("2026-01-01", "US") == []          # 只认 date/datetime
    assert lh.holidays_on(None, "US") == []
    assert lh.holidays_on(_d(2026, 1, 1), "us") == \
        lh.holidays_on(_d(2026, 1, 1), "US")                # 国家码大小写不敏感


def test_holidays_on_empty_calendar_and_datetime_input():
    assert lh.holidays_on(_d(2026, 11, 26), "US", calendar={}) == []
    assert lh.holidays_on(_d(2026, 11, 26), "US", calendar={"countries": {}}) == []
    assert lh.holidays_on(_d(2026, 11, 26), "US", calendar="garbage") == []
    # datetime 也接（是 date 的子类，取其 date 部分）
    got = lh.holidays_on(datetime(2026, 11, 26, 23, 30), "US")
    assert [h.key for h in got] == ["us_thanksgiving"]


def test_holiday_dataclass_is_frozen_and_typed():
    holiday = lh.holidays_on(_d(2026, 11, 26), "US")[0]
    assert isinstance(holiday, lh.Holiday)
    assert holiday.country == "US" and holiday.scope == "public" and holiday.greet is True
    assert holiday.name_zh == "感恩节" and holiday.name_en == "Thanksgiving"
    with pytest.raises(Exception):
        holiday.key = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# upcoming_holidays
# ---------------------------------------------------------------------------

def test_upcoming_holidays_delta_days():
    # within_days=1 只看 11-24/25 两天 → 感恩节（11-26）还没进窗
    assert lh.upcoming_holidays(_d(2026, 11, 24), "US", within_days=1) == []
    # within_days=2 含 delta 0..2 → 11-26 进窗，距今 2 天
    got = lh.upcoming_holidays(_d(2026, 11, 24), "US", within_days=2)
    assert [(h.key, n) for h, n in got] == [("us_thanksgiving", 2)]
    got = lh.upcoming_holidays(_d(2026, 11, 25), "US", within_days=2)
    assert [(h.key, n) for h, n in got] == [("us_thanksgiving", 1)]
    got = lh.upcoming_holidays(_d(2026, 11, 26), "US", within_days=0)
    assert [(h.key, n) for h, n in got] == [("us_thanksgiving", 0)]


def test_upcoming_holidays_crosses_month_and_year():
    # 跨月：10-30 起两天内命中 10-31 万圣夜
    got = lh.upcoming_holidays(_d(2026, 10, 30), "US", within_days=2)
    assert [(h.key, n) for h, n in got] == [("us_halloween", 1)]
    # 跨年：12-30 起三天内命中 12-31 跨年夜（+1）与次年 01-01 元旦（+2）
    got = lh.upcoming_holidays(_d(2026, 12, 30), "US", within_days=3)
    assert [(h.key, n) for h, n in got] == [("us_new_year_eve", 1), ("us_new_year", 2)]
    assert got[1][0].country == "US"


def test_upcoming_holidays_sorted_by_delta_then_scope():
    got = lh.upcoming_holidays(_d(2026, 5, 31), "US", within_days=2)
    assert [(h.key, n) for h, n in got] == []   # 5/31-6/2 无节
    # 自建日历：同一天多节按 scope 排，跨天按天数排
    cal = {"countries": {"ZZ": [
        {"key": "b_cultural", "name_zh": "乙", "name_en": "B", "scope": "cultural",
         "greet": True, "rule": {"type": "fixed", "month": 6, "day": 1}},
        {"key": "a_public", "name_zh": "甲", "name_en": "A", "scope": "public",
         "greet": True, "rule": {"type": "fixed", "month": 6, "day": 1}},
        {"key": "c_later", "name_zh": "丙", "name_en": "C", "scope": "public",
         "greet": True, "rule": {"type": "fixed", "month": 6, "day": 3}},
    ]}}
    got = lh.upcoming_holidays(_d(2026, 6, 1), "ZZ", within_days=5, calendar=cal)
    assert [(h.key, n) for h, n in got] == [
        ("a_public", 0), ("b_cultural", 0), ("c_later", 2)]


def test_upcoming_holidays_bad_input():
    assert lh.upcoming_holidays(_d(2026, 1, 1), "", within_days=5) == []
    assert lh.upcoming_holidays(None, "US", within_days=5) == []
    assert lh.upcoming_holidays(_d(2026, 12, 31), "US", within_days=-1) == []
    assert lh.upcoming_holidays(_d(2026, 12, 31), "US", within_days="x") == []
    # 极大窗口被夹住且不抛
    assert isinstance(lh.upcoming_holidays(_d(2026, 1, 1), "US", within_days=99999), list)


# ---------------------------------------------------------------------------
# holiday_fact_line
# ---------------------------------------------------------------------------

def _songkran() -> lh.Holiday:
    return lh.holidays_on(_d(2026, 4, 13), "TH")[0]


def _canada_day() -> lh.Holiday:
    return lh.holidays_on(_d(2026, 7, 1), "CA")[0]


def test_holiday_fact_line_user_side_zh():
    line = lh.holiday_fact_line(_songkran(), "zh", side="user")
    assert "【对方那边的节日（内部事实）】" in line
    assert "今天是泰国的宋干节（泼水节）。" in line
    assert "只在自然相关时提起" in line
    assert "不要像百科播报" in line
    assert "不要编造习俗细节" in line
    assert "你所在地" not in line          # 不能串到人设侧文案


def test_holiday_fact_line_persona_side_zh():
    line = lh.holiday_fact_line(_canada_day(), "zh", side="persona")
    assert "【你所在地的节日（内部事实）】" in line
    assert "今天是加拿大国庆日。" in line
    assert "当作你自己的生活背景" in line
    assert "街上的气氛" in line
    assert "对方那边" not in line
    # 人设侧不点国家前缀（人就住那儿）
    assert "加拿大的加拿大国庆日" not in line


def test_holiday_fact_line_user_side_en():
    line = lh.holiday_fact_line(_songkran(), "en", side="user")
    assert "[Holiday where they are - internal fact]" in line
    assert "Today is Songkran in Thailand." in line
    assert "don't sound like an encyclopedia" in line
    assert "宋干" not in line


def test_holiday_fact_line_persona_side_en():
    line = lh.holiday_fact_line(_canada_day(), "en", side="persona")
    assert "[Holiday where you live - internal fact]" in line
    assert "Today is Canada Day." in line
    assert "your own everyday backdrop" in line


def test_holiday_fact_line_accepts_lists_and_upcoming_tuples():
    # 裸列表（holidays_on 的产物）
    line = lh.holiday_fact_line(lh.holidays_on(_d(2026, 4, 13), "TH"), "zh")
    assert "今天是泰国的宋干节（泼水节）。" in line
    # (Holiday, 天数) 元组（upcoming_holidays 的产物）直接可喂
    pairs = lh.upcoming_holidays(_d(2026, 11, 24), "US", within_days=3)
    line = lh.holiday_fact_line(pairs, "zh")
    assert "再过2天是美国的感恩节。" in line
    line_en = lh.holiday_fact_line(pairs, "en")
    assert "In 2 days it's Thanksgiving in the US." in line_en
    # 明天
    tomorrow = lh.upcoming_holidays(_d(2026, 10, 30), "US", within_days=1)
    assert "明天是美国的万圣夜。" in lh.holiday_fact_line(tomorrow, "zh")
    assert "Tomorrow is Halloween in the US." in lh.holiday_fact_line(tomorrow, "en")


def test_holiday_fact_line_multiple_same_day_joined():
    items = lh.holidays_on(_d(2026, 6, 1), "ZZ", calendar=_TOY_CAL)
    line = lh.holiday_fact_line(items, "zh", side="persona")
    assert "法定甲、法定乙、宗教节、民俗节" in line
    line_en = lh.holiday_fact_line(items, "en", side="persona")
    assert "Public A, Public B, Religious Day, Cultural Day" in line_en


def test_holiday_fact_line_empty_and_garbage():
    for bad in ([], (), None, "", 0, {}, "some string", [None], [42]):
        assert lh.holiday_fact_line(bad) == "", bad
    assert lh.holiday_fact_line([], "en", side="persona") == ""


def test_holiday_fact_line_lang_falls_back_to_zh():
    """内部事实块按仓库惯例二元判定：en* → 英文，其余（含 th/ja）→ 中文。"""
    line = lh.holiday_fact_line(_songkran(), "th")
    assert "今天是泰国的宋干节（泼水节）。" in line
    assert lh.holiday_fact_line(_songkran(), None) == lh.holiday_fact_line(_songkran(), "zh")
    assert lh.holiday_fact_line(_songkran(), "en-GB") == lh.holiday_fact_line(_songkran(), "en")


def test_holiday_fact_line_caps_item_count():
    items = lh.holidays_on(_d(2026, 6, 1), "ZZ", calendar=_TOY_CAL)
    line = lh.holiday_fact_line(items * 5, "zh", side="persona")
    assert line.count("法定甲") <= lh._MAX_FACT_ITEMS


# ---------------------------------------------------------------------------
# load_calendar
# ---------------------------------------------------------------------------

def test_load_calendar_builtin_shape():
    cal = lh.load_calendar()
    assert isinstance(cal, dict) and cal
    assert cal.get("version") == 1
    assert isinstance(cal.get("lang_defaults"), dict)
    assert isinstance(cal.get("countries"), dict)


def test_load_calendar_missing_and_bad_file(tmp_path):
    assert lh.load_calendar(str(tmp_path / "nope.yaml")) == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("countries: [unclosed\n  : :\n", encoding="utf-8")
    assert lh.load_calendar(str(bad)) == {}
    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- a\n- b\n", encoding="utf-8")
    assert lh.load_calendar(str(not_mapping)) == {}


def test_load_calendar_path_is_cwd_independent():
    """内置路径相对本文件定位——生产以服务方式启动，cwd 不可预期。"""
    assert lh.DEFAULT_CALENDAR_PATH.endswith("locale_holidays.yaml")
    assert "config" in lh.DEFAULT_CALENDAR_PATH.replace("\\", "/")
    from pathlib import Path
    assert Path(lh.DEFAULT_CALENDAR_PATH).is_absolute()
    assert Path(lh.DEFAULT_CALENDAR_PATH).is_file()


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def test_index_cache_avoids_recomputing_rules(monkeypatch):
    """``holidays_on`` 在主动触达 tick 里逐会话调用——同 (country, year) 必须只展开一次。"""
    calls = {"n": 0}
    real_resolve = lh.resolve_rule

    def counting(rule, year):
        calls["n"] += 1
        return real_resolve(rule, year)

    monkeypatch.setattr(lh, "resolve_rule", counting)

    first = lh.holidays_on(_d(2026, 11, 26), "US")
    after_first = calls["n"]
    assert after_first > 0                       # 首次真的展开了规则

    second = lh.holidays_on(_d(2026, 11, 26), "US")
    assert calls["n"] == after_first             # 第二次是纯 dict 查找
    assert [h.key for h in second] == [h.key for h in first]

    # 同年不同天仍命中同一索引
    assert [h.key for h in lh.holidays_on(_d(2026, 12, 25), "US")] == ["us_christmas"]
    assert calls["n"] == after_first

    # 换年份才重算
    lh.holidays_on(_d(2027, 11, 25), "US")
    assert calls["n"] > after_first
    after_year = calls["n"]

    # 换国家也重算
    lh.holidays_on(_d(2026, 10, 12), "CA")
    assert calls["n"] > after_year


def test_clear_caches_forces_rebuild(monkeypatch):
    calls = {"n": 0}
    real_resolve = lh.resolve_rule

    def counting(rule, year):
        calls["n"] += 1
        return real_resolve(rule, year)

    monkeypatch.setattr(lh, "resolve_rule", counting)
    lh.holidays_on(_d(2026, 11, 26), "US")
    n1 = calls["n"]
    lh.clear_caches_for_tests()
    lh.holidays_on(_d(2026, 11, 26), "US")
    assert calls["n"] > n1


def test_injected_calendar_is_not_cached():
    """显式注入的日历不进缓存（身份无法安全做键），改了立刻生效。"""
    cal_a = {"countries": {"ZZ": [
        {"key": "a", "name_zh": "甲", "name_en": "A", "scope": "public", "greet": True,
         "rule": {"type": "fixed", "month": 6, "day": 1}}]}}
    cal_b = {"countries": {"ZZ": [
        {"key": "b", "name_zh": "乙", "name_en": "B", "scope": "public", "greet": True,
         "rule": {"type": "fixed", "month": 6, "day": 1}}]}}
    assert [h.key for h in lh.holidays_on(_d(2026, 6, 1), "ZZ", calendar=cal_a)] == ["a"]
    assert [h.key for h in lh.holidays_on(_d(2026, 6, 1), "ZZ", calendar=cal_b)] == ["b"]


# ---------------------------------------------------------------------------
# YAML 数据自检（防数据腐烂——这一节最重要）
# ---------------------------------------------------------------------------

def _all_entries():
    cal = lh.load_calendar()
    for country, rows in (cal.get("countries") or {}).items():
        for row in rows:
            yield str(country), row


def test_yaml_keys_globally_unique():
    seen: dict = {}
    for country, row in _all_entries():
        key = row.get("key")
        assert key, f"{country} 有条目缺 key"
        assert key not in seen, f"key 重复：{key}（{seen.get(key)} 与 {country}）"
        seen[key] = country
    assert len(seen) >= 200, f"条目总数意外偏少：{len(seen)}"


def test_yaml_names_bilingual_and_nonempty():
    for country, row in _all_entries():
        for field in ("name_zh", "name_en"):
            value = str(row.get(field) or "").strip()
            assert value, f"{country}/{row.get('key')} 缺 {field}"


def test_yaml_scope_and_rule_type_in_whitelist():
    for country, row in _all_entries():
        assert row.get("scope") in lh.SCOPES, f"{country}/{row.get('key')} scope 非法"
        rule = row.get("rule")
        assert isinstance(rule, dict), f"{country}/{row.get('key')} 缺 rule"
        assert rule.get("type") in lh.RULE_TYPES, \
            f"{country}/{row.get('key')} rule.type={rule.get('type')} 不在白名单"


def test_yaml_greet_is_bool():
    for country, row in _all_entries():
        assert isinstance(row.get("greet"), bool), f"{country}/{row.get('key')} greet 不是布尔"


def test_yaml_lang_defaults_point_at_real_countries():
    cal = lh.load_calendar()
    countries = set(cal.get("countries") or {})
    defaults = cal.get("lang_defaults") or {}
    assert defaults, "lang_defaults 不应为空"
    for lang, code in defaults.items():
        text = str(code or "").strip()
        if not text:
            continue      # 刻意留空的跨洲通用语
        assert text in countries, f"lang_defaults[{lang}]={text} 在 countries 里不存在"
    # 跨洲通用语必须留空（写死会给错国家的节日）
    for lang in ("en", "es", "pt", "fr"):
        assert str(defaults.get(lang) or "").strip() == "", f"{lang} 不该有默认国家"


def test_yaml_required_countries_present():
    countries = set(lh.load_calendar().get("countries") or {})
    required = {"TH", "VN", "PH", "ID", "MY", "SG", "KH", "JP", "KR", "CN", "TW", "HK",
                "IN", "AE", "TR", "RU", "US", "CA", "GB", "FR", "DE", "IT", "AU", "NZ", "IL"}
    assert required <= countries, f"缺少国家：{sorted(required - countries)}"


def test_yaml_every_country_has_enough_entries():
    for country, rows in (lh.load_calendar().get("countries") or {}).items():
        assert isinstance(rows, list)
        assert len(rows) >= 4, f"{country} 只有 {len(rows)} 条，覆盖太薄"


def test_yaml_country_labels_cover_all_countries():
    """事实块要说「今天是**泰国**的…」，每个策展国家都得有中英文国名。"""
    for country in (lh.load_calendar().get("countries") or {}):
        assert str(country) in lh.COUNTRY_LABELS, f"COUNTRY_LABELS 缺 {country}"
        zh, en = lh.COUNTRY_LABELS[str(country)]
        assert zh.strip() and en.strip(), country


def test_yaml_table_rules_cover_through_2029():
    """table 型到表尽头就静默消失，所以表必须够长——短了要在门禁里先红。"""
    found = 0
    for country, row in _all_entries():
        rule = row.get("rule") or {}
        if rule.get("type") != "table":
            continue
        found += 1
        years = {int(y) for y in (rule.get("dates") or {})}
        assert years, f"{country}/{row.get('key')} table 没有任何年份"
        assert max(years) >= 2029, \
            f"{country}/{row.get('key')} table 只到 {max(years)}，需覆盖到 2029 以后"
        assert min(years) <= 2026, f"{country}/{row.get('key')} table 起始年 {min(years)} 太晚"
    assert found >= 8, f"table 型条目意外偏少：{found}"


def test_yaml_all_rules_resolve_without_raising():
    """把每条规则在 2026 与 2030 各跑一遍：不抛、返回 list、日期落在目标年附近。"""
    checked = 0
    for country, row in _all_entries():
        rule = row.get("rule")
        for year in (2026, 2030):
            got = lh.resolve_rule(rule, year)
            assert isinstance(got, list), f"{country}/{row.get('key')} {year}"
            for day in got:
                assert isinstance(day, date)
                assert year - 1 <= day.year <= year + 1, \
                    f"{country}/{row.get('key')} {year} 解出越界日期 {day}"
            checked += 1
    assert checked >= 400, f"检查覆盖面意外偏少：{checked}"


@pytest.mark.skipif(not lh.lunar_available(), reason="需要 lunar_python")
def test_yaml_non_table_rules_actually_produce_dates():
    """非 table 规则（规则引擎的本体）在任一年份都必须真解出日期——空＝规则写错了。"""
    for country, row in _all_entries():
        rule = row.get("rule") or {}
        if rule.get("type") == "table":
            continue      # table 到期消失是设计，不是 bug
        for year in (2026, 2030, 2033):
            assert lh.resolve_rule(rule, year), \
                f"{country}/{row.get('key')} 在 {year} 解不出日期"


def test_yaml_political_and_mourning_days_are_not_greetable():
    """国庆/独立日/主权日/哀悼日一律 greet=false——宁可少祝贺，绝不在国难日道喜。"""
    must_be_false = {
        "us_independence_day", "ca_canada_day", "cn_national_day", "vn_national_day",
        "id_independence_day", "in_independence_day", "my_national_day", "sg_national_day",
        "ae_national_day", "au_australia_day", "tr_republic_day", "ru_russia_day",
        "fr_bastille_day", "it_republic_day", "us_memorial_day", "ca_remembrance_day",
        "au_anzac_day", "nz_anzac_day", "ru_victory_day", "kr_memorial_day",
        "th_king_bhumibol_memorial", "tw_peace_memorial_day", "il_yom_kippur",
        "ae_commemoration_day", "tr_ataturk_memorial", "ph_undas", "fr_all_saints",
    }
    seen = set()
    for _country, row in _all_entries():
        key = row.get("key")
        if key in must_be_false:
            seen.add(key)
            assert row.get("greet") is False, f"{key} 不该可主动祝贺"
    assert seen == must_be_false, f"清单里的 key 已不存在：{sorted(must_be_false - seen)}"


def test_yaml_emotional_holidays_are_greetable():
    """情感型节日必须 greet=true——这是「记得对方的节日」这个能力的价值本身。"""
    must_be_true = {
        "cn_spring_festival", "vn_tet", "kr_seollal", "th_songkran", "us_thanksgiving",
        "ca_thanksgiving", "us_mothers_day", "gb_mothering_sunday", "id_eid_al_fitr",
        "ae_eid_al_fitr", "cn_mid_autumn", "kr_chuseok", "il_hanukkah", "ru_womens_day",
        "us_christmas", "de_christmas_eve", "kr_parents_day", "vn_vu_lan",
    }
    seen = set()
    for _country, row in _all_entries():
        key = row.get("key")
        if key in must_be_true:
            seen.add(key)
            assert row.get("greet") is True, f"{key} 应该可主动祝贺"
    assert seen == must_be_true, f"清单里的 key 已不存在：{sorted(must_be_true - seen)}"


def test_yaml_shared_anchor_rules_expand_correctly():
    """YAML 锚点 + 合并键（``<<``）：同一份伊斯兰历表被多国引用，days 各自覆盖。"""
    cal = lh.load_calendar()
    by_key = {row["key"]: row for _c, row in _all_entries()}
    fitr_id = by_key["id_eid_al_fitr"]["rule"]
    fitr_sg = by_key["sg_eid_al_fitr"]["rule"]
    assert fitr_id["type"] == "table" and fitr_sg["type"] == "table"
    assert fitr_id["dates"][2026] == fitr_sg["dates"][2026] == "03-20"   # 共享同一份表
    assert fitr_id.get("days") == 2 and fitr_sg.get("days") is None      # 天数各国自定
    assert by_key["ae_eid_al_fitr"]["rule"].get("days") == 3
    assert by_key["tr_eid_al_adha"]["rule"].get("days") == 4
    # 展开后的天数确实不同
    assert len(lh.resolve_rule(fitr_id, 2026)) == 2
    assert len(lh.resolve_rule(fitr_sg, 2026)) == 1
    assert len(lh.resolve_rule(by_key["ae_eid_al_fitr"]["rule"], 2026)) == 3
    assert cal.get("_shared_rules"), "共享锚点段应存在（供运营集中维护日期表）"


def test_yaml_islamic_table_regresses_about_eleven_days_per_year():
    """伊斯兰年比公历短 ~11 天：日期必须逐年前移，不能出现「原地不动」的抄错。"""
    by_key = {row["key"]: row for _c, row in _all_entries()}
    dates = by_key["sg_eid_al_fitr"]["rule"]["dates"]
    resolved = []
    for year in sorted(int(y) for y in dates):
        resolved.extend(lh.resolve_rule({"type": "table", "dates": dates}, year))
    resolved.sort()
    assert len(resolved) >= 8
    for prev, nxt in zip(resolved, resolved[1:]):
        gap = (nxt - prev).days
        assert 340 <= gap <= 360, f"{prev} → {nxt} 间隔 {gap} 天，不符合伊斯兰年长度"


# ---------------------------------------------------------------------------
# 模块契约
# ---------------------------------------------------------------------------

def test_public_api_surface():
    for name in lh.__all__:
        assert hasattr(lh, name), f"__all__ 里的 {name} 不存在"
    for name in ("Holiday", "load_calendar", "country_for_language", "holidays_on",
                 "upcoming_holidays", "holiday_fact_line", "resolve_rule"):
        assert name in lh.__all__, f"{name} 应在 __all__ 里"
    assert lh.SCOPES == ("public", "religious", "cultural")
    assert set(lh.RULE_TYPES) == {"fixed", "lunar", "nth_weekday", "easter_offset",
                                  "table", "jieqi"}
