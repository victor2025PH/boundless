"""八字排盘引擎门禁：已知命例校验 + 边界不变量。全部离线可跑，零网络。

命例校验口径：四柱以**立春**分年（八字标准）；金标案例经 lunar-python 与公开排盘
工具交叉核对（1995-03-05 乙亥/戊寅/乙未 + 08:30 庚辰）。
"""

from __future__ import annotations

import pytest

from src.companion.bazi_engine import (
    BirthInfo,
    bazi_available,
    compute_bazi,
    format_chart_summary,
    liunian_ganzhi,
    reset_chart_cache,
)

pytestmark = pytest.mark.skipif(
    not bazi_available(), reason="lunar_python 未安装（引擎软失败路径另有测试）")

_GAN = set("甲乙丙丁戊己庚辛壬癸")
_ZHI = set("子丑寅卯辰巳午未申酉戌亥")


@pytest.fixture(autouse=True)
def _clean():
    reset_chart_cache()
    yield
    reset_chart_cache()


def _valid_ganzhi(gz: str) -> bool:
    return len(gz) == 2 and gz[0] in _GAN and gz[1] in _ZHI


# ── 金标命例 ─────────────────────────────────────────────────────────────────

def test_golden_case_1995_solar():
    c = compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))
    assert c is not None
    p = c["pillars"]
    assert p["year"]["ganzhi"] == "乙亥"
    assert p["month"]["ganzhi"] == "戊寅"
    assert p["day"]["ganzhi"] == "乙未"
    assert p["time"]["ganzhi"] == "庚辰"
    assert c["day_master"] == "乙木"
    assert c["shengxiao"] == "猪"
    # 十神：乙日主 → 年干乙=比肩、月干戊=正财、时干庚=正官
    assert p["year"]["shishen_gan"] == "比肩"
    assert p["month"]["shishen_gan"] == "正财"
    assert p["time"]["shishen_gan"] == "正官"


def test_lichun_year_boundary():
    """立春前一天仍属旧年柱（八字以立春分年，非正月初一）。"""
    before = compute_bazi(BirthInfo(1995, 2, 3, 12, 0))
    after = compute_bazi(BirthInfo(1995, 2, 5, 12, 0))
    assert before["pillars"]["year"]["ganzhi"] == "甲戌"
    assert after["pillars"]["year"]["ganzhi"] == "乙亥"


def test_lunar_input_maps_to_solar():
    """农历 1995-02-05 = 公历 1995-03-05（同一盘）。"""
    via_lunar = compute_bazi(BirthInfo(1995, 2, 5, 8, 30, is_lunar=True))
    via_solar = compute_bazi(BirthInfo(1995, 3, 5, 8, 30))
    assert via_lunar["solar_date"] == "1995-03-05"
    assert via_lunar["pillars"] == via_solar["pillars"]


# ── 诚实边界 ─────────────────────────────────────────────────────────────────

def test_hour_unknown_no_time_pillar():
    c = compute_bazi(BirthInfo(1995, 3, 5))
    assert c["hour_known"] is False
    assert "time" not in c["pillars"]
    assert "时辰未知" in format_chart_summary(c)


def test_gender_unknown_no_dayun():
    c = compute_bazi(BirthInfo(1995, 3, 5, 8, 30))
    assert c["dayun"] == []
    assert c["current_dayun"] is None


def test_gender_direction_differs():
    """阳男阴女顺排：乙亥年（阴年）女命顺排、男命逆排 → 首步大运不同。"""
    f = compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))
    m = compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="male"))
    assert f["dayun"] and m["dayun"]
    assert f["dayun"][0]["ganzhi"] != m["dayun"][0]["ganzhi"]


def test_current_dayun_progresses_with_now_ts():
    """now_ts 落在不同年份 → 当前大运随之推进。"""
    import calendar
    c2010 = compute_bazi(
        BirthInfo(1995, 3, 5, 8, 30, gender="female"),
        now_ts=calendar.timegm((2010, 6, 1, 0, 0, 0, 0, 0, 0)))
    reset_chart_cache()  # now_ts 不在缓存键内，换时间需清缓存
    c2026 = compute_bazi(
        BirthInfo(1995, 3, 5, 8, 30, gender="female"),
        now_ts=calendar.timegm((2026, 6, 1, 0, 0, 0, 0, 0, 0)))
    assert c2010["current_dayun"]["ganzhi"] != c2026["current_dayun"]["ganzhi"]
    assert c2010["now_liunian"]["year"] == 2010
    assert c2026["now_liunian"]["year"] == 2026


# ── 结构不变量 ────────────────────────────────────────────────────────────────

def test_all_pillars_valid_ganzhi_and_wuxing_totals():
    c = compute_bazi(BirthInfo(1988, 8, 8, 20, 0, gender="male"))
    for name in ("year", "month", "day", "time"):
        assert _valid_ganzhi(c["pillars"][name]["ganzhi"]), name
    # 五行计数：8 字 + 月令双计 = 9
    assert sum(c["wuxing_counts"].values()) == pytest.approx(9.0)
    assert c["strength"]["verdict"] in ("偏强", "偏弱", "中和")


def test_wuxing_total_without_hour():
    c = compute_bazi(BirthInfo(1988, 8, 8))
    # 6 字 + 月令双计 = 7
    assert sum(c["wuxing_counts"].values()) == pytest.approx(7.0)


def test_invalid_inputs_return_none():
    assert compute_bazi(BirthInfo(1850, 1, 1)) is None      # 年超界
    assert compute_bazi(BirthInfo(1995, 13, 5)) is None     # 月非法
    assert compute_bazi(BirthInfo(1995, 3, 5, 8, 99)) is None  # 分非法
    assert compute_bazi(None) is None                       # type: ignore[arg-type]


def test_chart_cache_hits_same_object():
    a = compute_bazi(BirthInfo(1995, 3, 5, 8, 30))
    b = compute_bazi(BirthInfo(1995, 3, 5, 8, 30))
    assert a is b


def test_liunian_ganzhi_2026():
    assert liunian_ganzhi(2026, 7, 12) == "丙午"


# ── 十神纯函数（Phase 2：流年细节/灵签共用） ───────────────────────────────────

def test_shishen_between_golden_rules():
    from src.companion.bazi_engine import shishen_between
    # 甲（阳木）视角的十组经典关系
    assert shishen_between("甲", "甲") == "比肩"
    assert shishen_between("甲", "乙") == "劫财"
    assert shishen_between("甲", "丙") == "食神"
    assert shishen_between("甲", "丁") == "伤官"
    assert shishen_between("甲", "戊") == "偏财"
    assert shishen_between("甲", "己") == "正财"
    assert shishen_between("甲", "庚") == "七杀"
    assert shishen_between("甲", "辛") == "正官"
    assert shishen_between("甲", "壬") == "偏印"
    assert shishen_between("甲", "癸") == "正印"
    assert shishen_between("甲", "?") == ""
    assert shishen_between("", "甲") == ""


def test_shishen_between_cross_verified_with_lunar():
    """与 lunar_python 的 getShiShenGan 全盘交叉验证（两套实现口径必须一致）。"""
    from src.companion.bazi_engine import shishen_between
    c = compute_bazi(BirthInfo(1988, 8, 8, 20, 0, gender="male"))
    day_gan = c["day_master"][0]
    for name in ("year", "month", "time"):
        p = c["pillars"][name]
        assert shishen_between(day_gan, p["gan"]) == p["shishen_gan"], name


def test_liunian_detail_2027_for_yi_master():
    """2027 丁未：对乙日主，丁=食神（我生同性）、未主气己=偏财（我克同性）。"""
    from src.companion.bazi_engine import format_liunian_line, liunian_detail
    d = liunian_detail("乙", 2027)
    assert d["ganzhi"] == "丁未"
    assert d["gan_shishen"] == "食神"
    assert d["zhi_shishen"] == "偏财"
    line = format_liunian_line(d)
    assert "2027" in line and "丁未" in line and "食神" in line
    assert format_liunian_line(None) == ""


def test_day_ganzhi_and_jieqi_known_date():
    import calendar
    from src.companion.bazi_engine import current_jieqi, day_ganzhi
    ts = calendar.timegm((2026, 7, 12, 2, 0, 0, 0, 0, 0))  # 东八区 2026-07-12 10:00
    assert day_ganzhi(ts) == "丁亥"
    assert current_jieqi(ts) == "小暑"


# ── 摘要格式 ─────────────────────────────────────────────────────────────────

def test_summary_contains_key_facts():
    c = compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))
    s = format_chart_summary(c)
    assert "乙亥 戊寅 乙未 庚辰" in s
    assert "日主：乙木" in s
    assert "喜用候选" in s
    assert "当前大运" in s
    assert "流年" in s


def test_summary_marks_missing_wuxing():
    """1995-03-05 08:30 盘面缺火 → 摘要点名（常见话题「五行缺什么」）。"""
    c = compute_bazi(BirthInfo(1995, 3, 5, 8, 30))
    assert "缺火" in format_chart_summary(c)


def test_summary_empty_for_bad_chart():
    assert format_chart_summary({}) == ""
    assert format_chart_summary(None) == ""  # type: ignore[arg-type]
