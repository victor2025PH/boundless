"""时空接轨守卫：时段问候 vs 当地小时、错城现居断言。"""

from __future__ import annotations

from datetime import datetime, timezone

from src.companion.persona_location import resolve_persona_place, persona_now
from src.companion.world_clock_guard import (
    apply_world_clock_guard,
    detect_daypart_conflict,
    detect_venue_conflict,
    detect_wrong_place_claim,
    strip_daypart_conflicts,
    strip_venue_conflicts,
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


# ── 深夜×白天场所断言（2026-08-22 实录：凌晨 3:31「我在二手书店看书」）────────

def test_venue_bookstore_at_3am_conflicts():
    assert detect_venue_conflict("我在二手书店看书呢", 3) == "bookstore"
    assert detect_venue_conflict("我现在在图书馆自习", 2) == "bookstore"
    assert detect_venue_conflict("I'm at the library rn", 3) == "bookstore"


def test_venue_within_business_hours_ok():
    assert detect_venue_conflict("我在二手书店看书呢", 15) is None
    assert detect_venue_conflict("在健身房撸铁", 20) is None
    assert detect_venue_conflict("在办公室加班", 22) is None  # 加班到深夜前合法


def test_venue_non_now_semantics_not_stripped():
    # 回忆 / 计划 / 转述 / 否定 / 问对方——都不是对「此刻」的断言
    assert detect_venue_conflict("白天在书店看了会儿书，超治愈", 3) is None
    assert detect_venue_conflict("明天打算去商场逛逛", 3) is None
    assert detect_venue_conflict("你还在公司吗？", 3) is None
    assert detect_venue_conflict("我不在书店啦，早回家了", 3) is None
    assert detect_venue_conflict("我平时都在健身房待到很晚", 3) is None


def test_venue_24h_places_not_in_table():
    assert detect_venue_conflict("在便利店值夜班", 3) is None
    assert detect_venue_conflict("在夜市吃宵夜", 1) is None


def test_venue_unknown_hour_never_judges():
    assert detect_venue_conflict("我在二手书店看书呢", -1) is None


def test_strip_venue_keeps_other_sentences():
    text = "我在二手书店看书呢。你怎么还没睡？"
    out = strip_venue_conflicts(text, 3)
    assert "书店" not in out
    assert "还没睡" in out


def test_apply_end_to_end_bookstore_at_3am():
    """金标：2026-08-22 03:31 实录——凌晨说在二手书店，出站前必须剥。"""
    import datetime as dt
    local = dt.datetime(2026, 8, 22, 3, 31)
    raw = "我在二手书店看书呢，等下淘两本旧书。你也喜欢看书吗？"
    out, info = apply_world_clock_guard(raw, local_now=local)
    assert info["venue_conflict"] == "bookstore"
    assert info["changed"] is True
    assert "书店" not in out
    assert "喜欢看书" in out  # 问对方的句子（含「你」）保留
