"""相册「季节/地点/敏感度」单源词表与判定门禁（实施90）。

钉住三件事：① 归一化保守语义（非法值一律 ""=不设门）；② 季节冲突只认对立季；
③ 季节判定与 outfit_state.season_of 单源一致（本模块只是转发口，不许分叉）。
"""
import datetime as dt

from src.companion.media_taxonomy import (
    SEASONS,
    normalize_country,
    normalize_scene,
    normalize_season,
    normalize_tod,
    persona_geo_context,
    place_mismatch,
    row_place,
    row_season,
    season_for,
    season_tag_conflicts,
    suggest_min_bond,
)


def test_normalize_season_valid_and_junk():
    for s in SEASONS:
        assert normalize_season(s) == s
        assert normalize_season(s.upper()) == s
    for junk in ("", None, "unknown", "none", "hot", 123, "夏天"):
        assert normalize_season(junk) == ""


def test_normalize_tod():
    assert normalize_tod("day") == "day"
    assert normalize_tod("NIGHT") == "night"
    for junk in ("", None, "unknown", "dusk"):
        assert normalize_tod(junk) == ""


def test_normalize_country():
    assert normalize_country("jp") == "JP"
    assert normalize_country(" CA ") == "CA"
    for junk in ("", None, "JPN", "Japan", "1x", "j"):
        assert normalize_country(junk) == ""


def test_normalize_scene_delegates_to_canonical():
    # 单源＝persona_media.scene_class_of：几个代表词必须归到同一 canonical 类
    assert normalize_scene("beach") == "beach"
    assert normalize_scene("海边") == "beach"
    assert normalize_scene("cozy home") == "home"
    assert normalize_scene("nonsense-xyz") == ""
    # 类名本身必须自归一（VLM 按枚举作答的形态；home/night_city 不在自身词表里）
    from src.companion.persona_media import SCENE_CLASSES
    for cls in SCENE_CLASSES:
        assert normalize_scene(cls) == cls, cls
    assert normalize_scene("night city") == "night_city"


def test_row_season_and_place_tag_readers():
    row = {"tags": ["scene:beach", "season:summer", "place:JP", "tod:day"]}
    assert row_season(row) == "summer"
    assert row_place(row) == "JP"
    assert row_season({"tags": ["season:junk"]}) == ""
    assert row_place({"tags": ["place:japan"]}) == ""
    assert row_season(None) == ""
    assert row_place({}) == ""


def test_season_for_matches_outfit_state_single_source():
    from src.companion.outfit_state import season_of
    for month in range(1, 13):
        t = dt.datetime(2026, month, 15)
        for hemi in ("north", "south"):
            assert season_for(t, hemisphere=hemi) == season_of(t, hemi)
    # 抽查绝对值防两边一起漂
    assert season_for(dt.datetime(2026, 1, 15)) == "winter"
    assert season_for(dt.datetime(2026, 1, 15), hemisphere="south") == "summer"
    assert season_for(dt.datetime(2026, 8, 15)) == "summer"


def test_season_tag_conflicts_only_opposites():
    assert season_tag_conflicts("summer", "winter") is True
    assert season_tag_conflicts("winter", "summer") is True
    # 同季/过渡季/缺值全不冲突（保守）
    assert season_tag_conflicts("summer", "summer") is False
    assert season_tag_conflicts("spring", "winter") is False
    assert season_tag_conflicts("autumn", "summer") is False
    assert season_tag_conflicts("", "winter") is False
    assert season_tag_conflicts("summer", "") is False
    assert season_tag_conflicts("junk", "winter") is False


def test_scenes_equivalent_groups():
    from src.companion.media_taxonomy import scenes_equivalent
    assert scenes_equivalent("home", "home") is True
    assert scenes_equivalent("home", "bedroom") is True
    assert scenes_equivalent("kitchen", "home") is True
    assert scenes_equivalent("cafe", "restaurant") is True
    assert scenes_equivalent("park", "night_city") is False
    assert scenes_equivalent("home", "cafe") is False
    assert scenes_equivalent("", "home") is False
    assert scenes_equivalent("home", "") is False


def test_place_mismatch_needs_both_sides():
    assert place_mismatch("JP", "CA") is True
    assert place_mismatch("JP", "JP") is False
    assert place_mismatch("", "CA") is False
    assert place_mismatch("JP", "") is False
    assert place_mismatch("japan", "CA") is False   # 非法标注不判


def test_suggest_min_bond_mapping_and_clamp():
    assert suggest_min_bond(0) == 0
    assert suggest_min_bond(1) == 20
    assert suggest_min_bond(2) == 45
    assert suggest_min_bond(3) == 70
    assert suggest_min_bond("2") == 45
    assert suggest_min_bond(9) == 70    # 越界向内收敛
    assert suggest_min_bond(-1) == 0
    assert suggest_min_bond(None) == 0
    assert suggest_min_bond("junk") == 0


def test_persona_geo_context_soft_paths():
    # 不存在的人设 / 空 id → 全空且绝不抛
    empty = persona_geo_context("")
    assert empty == {"season": "", "country": "", "city": ""}
    assert persona_geo_context("no_such_pid_geo") == empty


def test_persona_geo_context_with_located_persona():
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager.get_instance()
    pm.upsert_profile("geo_tax_t", {"name": "Geo", "location": "vancouver"})
    try:
        ctx = persona_geo_context("geo_tax_t")
        assert ctx["country"] == "CA"
        assert ctx["city"] == "vancouver"
        assert ctx["season"] in SEASONS
    finally:
        pm.delete_profile("geo_tax_t")
