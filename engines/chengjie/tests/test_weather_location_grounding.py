"""#82 天气/地点接地：天气事实必须点名「是人设自己所在城市的」，对方自述城市与人设
不同城时必须明说，LLM 才不会把人设这边的天气套到对方头上（"你那边也下雨了吧"）。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.companion import user_clock_resolver as ucr
from src.companion.persona_location import resolve_persona_place
from src.companion.user_clock import (
    TRUST_REPLACE,
    UserClock,
    infer_from_stated_place,
    peer_place_line,
)
from src.companion.weather_state import (
    clear_cache_for_tests,
    fetch_weather,
    weather_chat_note,
)

VAN = {"id": "lin_jiaxin", "location": "vancouver", "name": "林佳欣"}


@pytest.fixture(autouse=True)
def _clean():
    clear_cache_for_tests()
    ucr.clear_cache_for_tests()
    yield
    clear_cache_for_tests()
    ucr.clear_cache_for_tests()


def _payload(code=61, temp=12.0, precip=3.0):
    return {"current": {"weather_code": code, "temperature_2m": temp,
                        "precipitation": precip, "time": "2026-09-21T09:00"}}


def _snap():
    return fetch_weather(
        resolve_persona_place(VAN), transport=lambda u: _payload(),
        now=time.time())


# ── weather_chat_note 归属 ──────────────────────────────────────────────────

def test_weather_note_names_persona_city_and_disclaims_peer_side():
    note = weather_chat_note(_snap(), "zh", place_label="加拿大·温哥华")
    assert "温哥华" in note
    assert "你自己这边" in note
    assert "对方那边的天气你不知道" in note
    assert "当地天气" in note  # 旧断言兼容


def test_weather_note_without_label_still_says_it_is_own_side():
    note = weather_chat_note(_snap(), "zh")
    assert "你自己所在的城市" in note
    assert "不是对方那边的" in note


def test_weather_note_en_disclaims_peer_side():
    note = weather_chat_note(_snap(), "en", place_label="Vancouver, Canada")
    assert "Vancouver" in note
    assert "YOUR side" in note
    assert "never assume it's the same where they are" in note


# ── peer_place_line ────────────────────────────────────────────────────────

def test_peer_place_line_different_city_is_explicit():
    clock = infer_from_stated_place("我在曼谷")
    assert clock is not None and clock.source == "stated_city"
    line = peer_place_line(clock, resolve_persona_place(VAN), "zh")
    assert "曼谷" in line and "温哥华" in line
    assert "不同城" in line
    assert "你那边也下雨了吧" in line  # 反例被点名


def test_peer_place_line_same_city_allows_we():
    clock = infer_from_stated_place("我住在温哥华")
    assert clock is not None
    line = peer_place_line(clock, resolve_persona_place(VAN), "zh")
    assert "同城" in line
    assert "不同城" not in line


def test_peer_place_line_only_from_stated_city():
    phone_clock = UserClock(
        tz_name="Asia/Bangkok", offset_hours=7, source="phone_cc",
        confidence=0.85, country="TH", city_slug="bangkok", trust=TRUST_REPLACE)
    assert peer_place_line(phone_clock, resolve_persona_place(VAN)) == ""
    assert peer_place_line(None, None) == ""


def test_peer_place_line_en():
    clock = infer_from_stated_place("I'm in Bangkok")
    assert clock is not None
    line = peer_place_line(clock, resolve_persona_place(VAN), "en")
    assert "Bangkok" in line and "Vancouver" in line
    assert "different cities" in line


# ── 接线：skill_manager → context → ai_client ──────────────────────────────

class _Episodic:
    def __init__(self, rows):
        self.rows = rows

    def list_rows(self, prefix="", limit=100, source=""):
        return list(self.rows)


def _sm(episodic):
    from src.skills.skill_manager import SkillManager

    sm = MagicMock(spec=SkillManager)
    sm.logger = MagicMock()
    sm.config = MagicMock()
    sm.config.config = {"companion": {"user_clock": {"enabled": True}}}
    sm._episodic_store = episodic
    sm._episodic_storage_key = MagicMock(return_value="telegram:acct:peer1")
    sm._selfie_persona_for_prompt = MagicMock(return_value=VAN)
    return sm


def test_inject_peer_locale_writes_peer_place_line_and_clears_it():
    from src.skills.skill_manager import SkillManager

    sm = _sm(_Episodic([{"content": "用户住在曼谷", "created_at": 100.0}]))
    ctx: dict = {"_peer_place_line": "旧的"}
    SkillManager._inject_peer_locale(sm, ctx, "u1", "peer1", "telegram")
    line = ctx.get("_peer_place_line") or ""
    assert "曼谷" in line and "不同城" in line

    sm_off = _sm(None)
    sm_off.config.config = {"companion": {}}
    SkillManager._inject_peer_locale(sm_off, ctx, "u1", "peer1", "telegram")
    assert "_peer_place_line" not in ctx


def test_time_grounding_passes_place_label_to_weather_note():
    import inspect

    from src.skills.skill_manager import SkillManager

    src = inspect.getsource(SkillManager._inject_time_grounding)
    assert "place_label=_place.display" in src


def test_ai_client_consumes_peer_place_line():
    import inspect

    from src.ai import ai_client

    assert "_peer_place_line" in inspect.getsource(ai_client)
