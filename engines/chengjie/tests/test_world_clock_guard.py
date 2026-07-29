"""时空接轨守卫：时段问候 vs 当地小时、错城现居断言。"""

from __future__ import annotations

from datetime import datetime, timezone

from src.companion.persona_location import resolve_persona_place, persona_now
from src.companion.world_clock_guard import (
    apply_world_clock_guard,
    detect_daypart_conflict,
    detect_wrong_place_claim,
    strip_daypart_conflicts,
    strip_wrong_place_claims,
    world_clock_prompt_nail,
)

SUMMER_UTC = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # Van 05:00
VAN = resolve_persona_place({"location": "vancouver"})
CEBU = resolve_persona_place({"location": "cebu"})


def test_morning_greeting_conflicts_at_vancouver_night():
    # 温哥华 02:00 说「下午好」必冲突；「刚吃完午饭」同理
    assert detect_daypart_conflict("下午好呀，在干嘛", 2) == "afternoon"
    assert detect_daypart_conflict("刚吃完午饭好撑", 2) == "noon"
    assert detect_daypart_conflict("早上好", 2) == "morning"
    # 当地真下午不冲突
    assert detect_daypart_conflict("下午好呀", 15) is None


def test_strip_keeps_non_conflict_sentences():
    text = "下午好呀。我在看剧呢，你呢？"
    out = strip_daypart_conflicts(text, 2)
    assert "下午" not in out
    assert "看剧" in out


def test_wrong_city_claim_vancouver_vs_cebu():
    assert detect_wrong_place_claim("我现在在宿务开店忙", VAN) == "cebu"
    assert detect_wrong_place_claim("I'm in Cebu today", VAN) == "cebu"
    # 本城 OK
    assert detect_wrong_place_claim("我在温哥华刚下班", VAN) is None
    # 遗产闲聊不误伤（无「我在菲律宾」现居结构）
    assert detect_wrong_place_claim("妈妈是菲律宾混血，会几句 Tagalog", VAN) is None


def test_strip_wrong_place_claim():
    text = "我现在在马尼拉呢。今天好热。"
    out = strip_wrong_place_claims(text, VAN)
    assert "马尼拉" not in out
    assert "好热" in out


def test_apply_end_to_end_vancouver_nurse_bug():
    """金标：温哥华当地清晨，出站却按菲/中国正午口径 + 错城。"""
    local = persona_now(VAN, SUMMER_UTC)  # 05:00
    raw = "下午好呀！我现在在宿务，刚吃完午饭。"
    out, info = apply_world_clock_guard(
        raw, place=VAN, local_now=local)
    assert info["changed"] is True
    assert info["daypart_conflict"] in ("afternoon", "noon")
    assert info["wrong_place"] == "cebu"
    assert "下午" not in out and "午饭" not in out and "宿务" not in out


def test_prompt_nail_mentions_place():
    nail = world_clock_prompt_nail(VAN, 2)
    assert "温哥华" in nail and "菲律宾" in nail


def test_cebu_persona_can_claim_cebu():
    assert detect_wrong_place_claim("我在宿务店里忙", CEBU) is None
    assert detect_wrong_place_claim("我现在在温哥华旅游", CEBU) == "vancouver"
