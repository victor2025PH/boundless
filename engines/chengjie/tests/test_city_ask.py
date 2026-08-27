"""城市主动采集闸门 + 短答抽取（overseas-like，不是人人都问）。"""

from src.companion.user_clock import (
    TRUST_NARROW,
    TRUST_REPLACE,
    UserClock,
    city_ask_eligible,
)
from src.companion.user_clock_resolver import (
    stated_place_from_inbound_text,
    stated_place_from_reply_text,
)


def _clk(*, trust=TRUST_NARROW, source="behavior", offset=-5.0, country="CN"):
    return UserClock(
        tz_name=f"UTC{int(offset):+03d}:00",
        offset_hours=float(offset),
        source=source,
        confidence=0.78,
        country=country,
        city_slug="",
        trust=trust,
    )


# ── city_ask_eligible ───────────────────────────────────────────────────

def test_replace_clock_never_asks():
    clock = _clk(trust=TRUST_REPLACE, source="stated_city", offset=7.0, country="TH")
    assert city_ask_eligible(clock, language="en") == (False, "already_replace")
    assert city_ask_eligible(clock, language="zh") == (False, "already_replace")


def test_phone_cc_replace_never_asks():
    clock = _clk(trust=TRUST_REPLACE, source="phone_cc", offset=8.0, country="PH")
    assert city_ask_eligible(clock, language="")[0] is False


def test_foreign_lang_asks_without_clock():
    assert city_ask_eligible(None, language="en") == (True, "foreign_lang")
    assert city_ask_eligible(None, language="ja") == (True, "foreign_lang")
    assert city_ask_eligible(None, language="pt-BR") == (True, "foreign_lang")


def test_zh_without_clock_does_not_ask():
    assert city_ask_eligible(None, language="zh") == (False, "not_overseas")
    assert city_ask_eligible(None, language="zh-CN") == (False, "not_overseas")
    assert city_ask_eligible(None, language="") == (False, "not_overseas")
    assert city_ask_eligible(None, language="unknown") == (False, "not_overseas")


def test_behavior_shift_asks_even_if_zh_and_cn():
    """8/19 晚安事故：behavior UTC-5 + country=CN + 中文 → 该问城市。"""
    clock = _clk(offset=-5.0, country="CN")
    assert city_ask_eligible(clock, language="zh") == (True, "behavior_shift")


def test_behavior_plus8_does_not_ask():
    clock = _clk(offset=8.0, country="CN")
    assert city_ask_eligible(clock, language="zh") == (False, "not_overseas")


def test_behavior_shift_under_3h_does_not_ask():
    clock = _clk(offset=7.0, country="CN")  # vs +8 = 1h
    assert city_ask_eligible(clock, language="zh") == (False, "not_overseas")


def test_behavior_corroborated_shift_asks():
    clock = _clk(source="behavior_corroborated", offset=-2.0, country="CN")
    assert city_ask_eligible(clock, language="") == (True, "behavior_shift")


def test_foreign_lang_wins_over_plus8_behavior():
    clock = _clk(offset=8.0, country="")
    assert city_ask_eligible(clock, language="en") == (True, "foreign_lang")


def test_eligible_junk_safe():
    assert city_ask_eligible("bad")[0] is False  # type: ignore[arg-type]


# ── stated_place_from_reply_text ────────────────────────────────────────

def test_reply_short_city_whitelist():
    assert stated_place_from_reply_text("曼谷") == "曼谷"
    assert stated_place_from_reply_text("Bangkok")
    assert stated_place_from_reply_text("曼谷。") == "曼谷"


def test_reply_live_in_sentence():
    assert stated_place_from_reply_text("我在曼谷") == "曼谷"
    assert stated_place_from_inbound_text("我在曼谷") == "曼谷"


def test_reply_travel_skipped():
    assert stated_place_from_reply_text("明天去纽约") == ""
    assert stated_place_from_reply_text("I'm from Manila") == ""


def test_reply_passing_mention_skipped():
    assert stated_place_from_reply_text("曼谷那家店味道不错啊") == ""


def test_reply_unknown_city_skipped():
    assert stated_place_from_reply_text("不知道") == ""
    assert stated_place_from_reply_text("小县城") == ""
