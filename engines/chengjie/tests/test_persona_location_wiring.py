"""人设本地时钟接线门禁：时间行 / 场景注入 / 衣着半球走 persona_now。"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.ai.ai_client import build_time_context_line
from src.ai.companion_selfie import resolve_current_scene, resolve_meal_state
from src.companion.outfit_state import season_of
from src.companion.persona_location import (
    resolve_persona_now,
    resolve_place_with_fallback,
)


SUMMER_UTC = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
VAN = {"id": "lin_jiaxin", "location": "vancouver", "name": "林佳欣"}
SYD = {"id": "test_syd", "location": "sydney", "name": "悉尼测试"}


def test_build_time_context_line_uses_persona_local_now_and_label():
    local = resolve_persona_now(VAN, SUMMER_UTC)  # 05:00 PDT
    line = build_time_context_line(local, place_label="加拿大·温哥华")
    assert "05:00" in line
    assert "清晨" in line
    assert "加拿大·温哥华" in line
    assert "作息合理性" in line  # 清晨硬约束


def test_scene_and_meal_follow_persona_local_clock():
    scfg = {
        "scene_hint": "cozy bedroom, soft lamp",
        "scene_rotation": [
            "sunny park afternoon",
            "cozy bedroom, soft lamp",
            "night city street neon",
        ],
    }
    local = resolve_persona_now(VAN, SUMMER_UTC)  # 05:00 → 清晨桶
    scene = resolve_current_scene(VAN, scfg, now=local)
    # 清晨应滤掉 afternoon 硬冲突场景（Phase19）；具体取值随池变化，但不应含 afternoon
    assert "afternoon" not in scene.lower()
    meal = resolve_meal_state("lin_jiaxin", now=local)
    # 05:00 当地：深夜/刚起床口径，不应是「刚吃过午饭」
    assert "午饭" not in meal


def test_southern_hemisphere_from_persona_place():
    place = resolve_place_with_fallback(SYD)
    assert place is not None
    assert place.hemisphere == "south"
    # 7 月北半球夏天 → 南半球冬天
    local = resolve_persona_now(SYD, SUMMER_UTC)
    assert season_of(local, place.hemisphere) == "winter"
    assert season_of(local, "north") == "summer"


def test_inject_scene_state_writes_local_time_keys():
    """skill_manager._inject_scene_state 应写入当地时间 context 键。"""
    from src.skills.skill_manager import SkillManager

    sm = MagicMock(spec=SkillManager)
    sm._selfie_cfg = MagicMock(return_value={
        "enabled": True,
        "scene_in_chat": True,
        "scene_itinerary": True,
        "meal_state_in_chat": True,
        "scene_rotation": ["cozy home kitchen, warm light"],
    })
    sm._selfie_persona_for_prompt = MagicMock(return_value=VAN)
    sm._selfie_album_key = MagicMock(return_value="lin_jiaxin")
    sm._last_sent_media_series = MagicMock(return_value="")
    sm.logger = MagicMock()
    sm.config = MagicMock()
    sm.config.config = {"companion": {"weather": {"enabled": False}}}
    # 时空接地 2026-08-02 从 scene_state 解耦成独立方法后经 self 委托——
    # spec-mock 会把该委托吞成 no-op（时间键从此不写=测试名存实亡），
    # 绑回真方法才是本测试想钉的链路（stale-red 对齐 2026-08-18）。
    sm._inject_time_grounding = SkillManager._inject_time_grounding.__get__(sm)

    ctx: dict = {}
    # 绑定真实方法
    SkillManager._inject_scene_state(sm, ctx)
    assert ctx.get("_persona_place_label") == "加拿大·温哥华"
    assert "_persona_local_now" in ctx
    _lt = ctx.get("_persona_local_time_line") or ""
    assert "温哥华" in _lt
    # 当地墙钟与时间行必须同小时（防二次换算把温哥华深夜拧成 UTC+8 正午）
    _local = ctx["_persona_local_now"]
    assert f"{_local.hour:02d}:" in _lt
    assert "UTC+8" in _lt
    assert ctx.get("_current_scene_note")
