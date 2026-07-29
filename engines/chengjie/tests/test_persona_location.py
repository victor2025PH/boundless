"""persona_location 单元测试：预置表完整性 / location 解析链 / 背景文本推断 / 本地时钟换算 / 提示行。

全部离线（tzdata 本地包），不触网、不依赖生产服务；时钟断言用固定 aware UTC 输入保证确定性。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.companion.persona_location import (
    CITY_PRESETS,
    PersonaPlace,
    daypart_label,
    infer_place_from_text,
    local_time_line,
    persona_local_hour,
    persona_now,
    resolve_persona_now,
    resolve_persona_place,
    resolve_place_with_fallback,
    time_gap_line,
    tz_offset_hours,
)

UTC = timezone.utc
SUMMER_UTC = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)   # 温哥华 PDT(-7) -> 05:00
WINTER_UTC = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)   # 温哥华 PST(-8) -> 04:00
MON_0612_VAN = datetime(2026, 7, 27, 13, 12, tzinfo=UTC)  # 温哥华 2026-07-27 周一 06:12

REQUIRED_SLUGS = (
    "vancouver", "toronto", "los_angeles", "san_francisco", "seattle", "new_york",
    "honolulu", "london", "paris", "dubai", "tokyo", "osaka", "seoul", "bangkok",
    "chiang_mai", "phuket", "hanoi", "ho_chi_minh", "phnom_penh", "singapore",
    "kuala_lumpur", "jakarta", "bali", "manila", "cebu", "taipei", "hong_kong",
    "macau", "shanghai", "beijing", "shenzhen", "chengdu", "sydney", "melbourne",
    "auckland",
)


def _place(slug: str) -> PersonaPlace:
    p = resolve_persona_place({"location": slug})
    assert p is not None, f"预置 slug 应可解析: {slug}"
    return p


def _server_offset_hours() -> float:
    off = datetime.now().astimezone().utcoffset()
    return off.total_seconds() / 3600.0 if off else 0.0


# ---------------------------------------------------------------------------
# 预置表完整性
# ---------------------------------------------------------------------------

def test_presets_every_tz_constructible():
    for slug, meta in CITY_PRESETS.items():
        ZoneInfo(meta["tz"])  # 非法 IANA 名会在此抛出


def test_presets_fields_and_latlon_ranges():
    for slug, meta in CITY_PRESETS.items():
        assert {"city_zh", "city_en", "country", "tz", "lat", "lon"} <= set(meta), slug
        assert meta["city_zh"] and meta["city_en"], slug
        assert re.fullmatch(r"[A-Z]{2}", meta["country"]), slug
        assert -90.0 <= meta["lat"] <= 90.0, slug
        assert -180.0 <= meta["lon"] <= 180.0, slug
        assert re.fullmatch(r"[a-z0-9_]+", slug), slug


def test_presets_required_cities_and_no_dup():
    missing = [s for s in REQUIRED_SLUGS if s not in CITY_PRESETS]
    assert not missing, f"缺预置城市: {missing}"
    assert len(CITY_PRESETS) >= 30
    # slug 为 dict 键天然唯一；再校验中文名不撞（防复制粘贴出重复条目）
    zh_names = [m["city_zh"] for m in CITY_PRESETS.values()]
    assert len(set(zh_names)) == len(zh_names)


# ---------------------------------------------------------------------------
# resolve_persona_place
# ---------------------------------------------------------------------------

def test_resolve_by_slug_and_display():
    p = _place("vancouver")
    assert p.slug == "vancouver"
    assert p.tz_name == "America/Vancouver"
    assert p.country == "CA"
    assert p.hemisphere == "north"
    assert p.display("zh") == "加拿大·温哥华"
    assert p.display("en") == "Vancouver, Canada"


def test_resolve_by_names_case_insensitive():
    assert resolve_persona_place({"location": "温哥华"}).slug == "vancouver"
    assert resolve_persona_place({"location": "VANCOUVER"}).slug == "vancouver"
    assert resolve_persona_place({"location": "Los Angeles"}).slug == "los_angeles"
    assert resolve_persona_place({"location": "曼谷"}).slug == "bangkok"


def test_resolve_contains_match():
    assert resolve_persona_place({"location": "加拿大温哥华"}).slug == "vancouver"
    assert resolve_persona_place({"location": "住在东京都内"}).slug == "tokyo"


def test_resolve_dict_custom_valid_tz():
    p = resolve_persona_place({"location": {
        "city": "Whistler", "timezone": "America/Vancouver",
        "lat": 50.12, "lon": -122.95, "country": "ca",
    }})
    assert p is not None
    assert p.slug == "custom"
    assert p.tz_name == "America/Vancouver"
    assert p.country == "CA"
    assert p.lat == pytest.approx(50.12)
    assert p.lon == pytest.approx(-122.95)


def test_resolve_dict_pure_timezone_no_coords():
    p = resolve_persona_place({"location": {"tz": "Asia/Bangkok"}})
    assert p is not None
    assert p.slug == "custom"
    assert p.lat is None and p.lon is None
    assert p.hemisphere == "north"  # lat 未知按北半球


def test_resolve_dict_city_hits_preset_completion():
    p = resolve_persona_place({"location": {"city": "曼谷", "tz": "Asia/Bangkok"}})
    assert p is not None
    assert p.slug == "bangkok"
    assert p.city_en == "Bangkok"
    assert p.country == "TH"
    assert p.lat == pytest.approx(13.76)


def test_resolve_dict_invalid_or_missing_tz():
    assert resolve_persona_place({"location": {"city": "X", "timezone": "Mars/Olympus"}}) is None
    assert resolve_persona_place({"location": {"city": "曼谷"}}) is None  # timezone 必填


def test_resolve_disabled_none_off():
    assert resolve_persona_place({"location": "none"}) is None
    assert resolve_persona_place({"location": "OFF"}) is None
    assert resolve_persona_place({"location": " None "}) is None


def test_resolve_missing_and_garbage_inputs():
    assert resolve_persona_place({}) is None
    assert resolve_persona_place(None) is None
    assert resolve_persona_place(42) is None
    assert resolve_persona_place({"location": 123}) is None
    assert resolve_persona_place({"location": ["vancouver"]}) is None
    assert resolve_persona_place({"location": "亚特兰蒂斯"}) is None


# ---------------------------------------------------------------------------
# infer_place_from_text（真实人设背景片段）
# ---------------------------------------------------------------------------

def test_infer_region_word_beats_country_word():
    # 加州（区域词）先于香港（城市词）出现，且都优先于「美籍」类国家线索
    assert infer_place_from_text("美籍华裔，长居加州，拥有香港资产") == "los_angeles"


def test_infer_first_city_wins():
    assert infer_place_from_text("生长于温哥华，香港/菲律宾混血，频繁往返香港") == "vancouver"


def test_infer_pasay_maps_manila():
    assert infer_place_from_text("被骗到菲律宾马尼拉帕赛") == "manila"


def test_infer_cebu():
    assert infer_place_from_text("2022 年搬到宿务，白天在线") == "cebu"


def test_infer_gangshen_hits_hong_kong():
    assert infer_place_from_text("在港深两地工作逾 15 年") == "hong_kong"


def test_infer_country_word_fallback():
    assert infer_place_from_text("目前人在泰国生活") == "bangkok"
    assert infer_place_from_text("长期在越南做外贸") == "ho_chi_minh"


def test_infer_english_word_boundary():
    assert infer_place_from_text("Based in Vancouver for 5 years") == "vancouver"
    assert infer_place_from_text("manilausx is not a city word") is None  # 无词边界不命中


def test_infer_no_location_returns_none():
    assert infer_place_from_text("每天喝咖啡撸猫，喜欢摄影") is None
    assert infer_place_from_text("") is None
    assert infer_place_from_text(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_place_with_fallback
# ---------------------------------------------------------------------------

def test_fallback_explicit_location_beats_background():
    p = resolve_place_with_fallback({"location": "tokyo", "background": "生长于温哥华"})
    assert p is not None and p.slug == "tokyo"


def test_fallback_none_suppresses_infer():
    assert resolve_place_with_fallback({"location": "none", "background": "生长于温哥华"}) is None


def test_fallback_infers_from_role_and_background():
    persona = {"role": "咖啡店老板娘", "background": "美籍华裔，长居加州"}
    p = resolve_place_with_fallback(persona)
    assert p is not None and p.slug == "los_angeles"
    assert resolve_place_with_fallback(persona, auto_infer=False) is None
    assert resolve_place_with_fallback({}) is None
    assert resolve_place_with_fallback(42) is None


# ---------------------------------------------------------------------------
# persona_now / persona_local_hour
# ---------------------------------------------------------------------------

def test_persona_now_none_matches_datetime_now():
    a = persona_now(None)
    b = datetime.now()
    assert a.tzinfo is None
    assert abs((b - a).total_seconds()) < 2.0


def test_persona_now_vancouver_dst_and_winter_naive():
    van = _place("vancouver")
    s = persona_now(van, SUMMER_UTC)
    assert s.tzinfo is None  # 铁律：绝不返回 aware
    assert (s.year, s.month, s.day, s.hour, s.minute) == (2026, 7, 15, 5, 0)   # PDT -7
    w = persona_now(van, WINTER_UTC)
    assert w.tzinfo is None
    assert (w.year, w.month, w.day, w.hour, w.minute) == (2026, 1, 15, 4, 0)   # PST -8


def test_persona_now_invalid_tz_falls_back():
    bad = PersonaPlace(slug="custom", city_zh="坏城", city_en="Bad", country="",
                       tz_name="Mars/Olympus", lat=None, lon=None)
    a = persona_now(bad)
    assert a.tzinfo is None
    assert abs((datetime.now() - a).total_seconds()) < 2.0


def test_persona_local_hour():
    van = _place("vancouver")
    assert persona_local_hour(van, SUMMER_UTC) == 5
    assert persona_local_hour(_place("bangkok"), SUMMER_UTC) == 19
    assert 0 <= persona_local_hour(None) <= 23


def test_resolve_persona_now_from_persona_dict():
    dt = resolve_persona_now({"location": "vancouver"}, SUMMER_UTC)
    assert dt.tzinfo is None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 7, 15, 5)
    # 无 location 时从 background 推断
    dt2 = resolve_persona_now(
        {"background": "生长于温哥华的护士"}, SUMMER_UTC)
    assert dt2.hour == 5
    # 空人设回落服务器时钟（naive）
    assert resolve_persona_now({}).tzinfo is None


# ---------------------------------------------------------------------------
# local_time_line / daypart / time_gap_line
# ---------------------------------------------------------------------------

def test_local_time_line_zh_exact():
    line = local_time_line(_place("vancouver"), "zh", MON_0612_VAN)
    assert line.startswith(
        "你人在加拿大·温哥华，当地时间 2026-07-27 周一 06:12（清晨）。"
    )
    assert "UTC+8" in line  # 时空钉：禁按中国/菲律宾时间


def test_local_time_line_en_and_bad_place():
    line = local_time_line(_place("vancouver"), "en", MON_0612_VAN)
    assert "Vancouver, Canada" in line
    assert "2026-07-27" in line and "Mon" in line and "06:12" in line
    assert "early morning" in line
    assert "UTC+8" in line
    assert local_time_line(None) == ""  # type: ignore[arg-type]  # 热路径不 raise


def test_local_time_line_naive_local_not_reconverted():
    """生产接线把 persona_now 的 naive 当地再喂给 local_time_line，不得二次换算。

    旧 bug：温哥华 05:00 PDT 被当成服务器 UTC+8 再转一次 → 伪「中午」穿帮。
    """
    van = _place("vancouver")
    local = persona_now(van, SUMMER_UTC)  # 05:00 naive
    assert (local.hour, local.minute) == (5, 0)
    # 幂等：naive 当地再进 persona_now 仍是 05:00
    assert persona_now(van, local).hour == 5
    line = local_time_line(van, "zh", local)
    assert "05:00" in line and "清晨" in line
    assert "11:" not in line and "中午" not in line


def test_daypart_thresholds():
    assert daypart_label(4) == "深夜"
    assert daypart_label(6) == "清晨"
    assert daypart_label(12) == "中午"
    assert daypart_label(18) == "傍晚"
    assert daypart_label(23) == "深夜"
    assert daypart_label(12, "en") == "noon"


def test_tz_offset_hours_vancouver_summer():
    off = tz_offset_hours(_place("vancouver"), SUMMER_UTC)
    assert off == pytest.approx(-7.0 - _server_offset_hours())
    assert tz_offset_hours(None) == 0.0


def test_time_gap_line_threshold_bangkok_vs_vancouver():
    bkk, van = _place("bangkok"), _place("vancouver")
    off_bkk = tz_offset_hours(bkk, SUMMER_UTC)
    off_van = tz_offset_hours(van, SUMMER_UTC)
    gap_bkk = time_gap_line(bkk, "zh", SUMMER_UTC)
    gap_van = time_gap_line(van, "zh", SUMMER_UTC)
    assert (gap_bkk is None) == (abs(off_bkk) < 3.0)
    assert (gap_van is None) == (abs(off_van) < 3.0)
    if datetime.now().astimezone().utcoffset() == timedelta(hours=8):
        # 生产机 = UTC+8：曼谷 -1h 不出提示；温哥华夏令时 -15h 必出
        assert gap_bkk is None
        assert gap_van is not None
        assert "-15" in gap_van and "小时时差" in gap_van
        gap_en = time_gap_line(van, "en", SUMMER_UTC)
        assert gap_en is not None and "-15" in gap_en
    assert time_gap_line(None) is None


# ---------------------------------------------------------------------------
# hemisphere / display 补充
# ---------------------------------------------------------------------------

def test_hemisphere_south_and_north():
    assert _place("sydney").hemisphere == "south"
    assert _place("jakarta").hemisphere == "south"
    assert _place("tokyo").hemisphere == "north"


def test_display_city_state_dedupe():
    assert _place("singapore").display("zh") == "新加坡"
    assert _place("singapore").display("en") == "Singapore"
    assert _place("hong_kong").display("zh") == "香港"
    assert _place("taipei").display("zh") == "台湾·台北"
