"""weather_state 离线门禁：WMO 映射 / 缓存 TTL / 事实块 / 主动钩子 / 场景冲突。"""

from __future__ import annotations

import time

import pytest

from src.companion.persona_location import resolve_persona_place
from src.companion.weather_state import (
    clear_cache_for_tests,
    dump_stats,
    fetch_weather,
    reset_stats_for_tests,
    scene_conflicts_with_weather,
    weather_chat_note,
    weather_proactive_hook,
    wmo_label,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_cache_for_tests()
    reset_stats_for_tests()
    yield
    clear_cache_for_tests()
    reset_stats_for_tests()


def _place():
    return resolve_persona_place({"location": "vancouver"})


def _payload(code=61, temp=12.3, precip=1.2):
    return {
        "current": {
            "temperature_2m": temp,
            "weather_code": code,
            "relative_humidity_2m": 80,
            "wind_speed_10m": 10,
            "precipitation": precip,
        }
    }


def test_wmo_label_buckets():
    assert wmo_label(0, "zh")[1] == "clear"
    assert wmo_label(65, "zh")[0] == "大雨"
    assert wmo_label(95, "en")[1] == "storm"
    assert wmo_label(999, "zh")[1] == "unknown"


def test_fetch_weather_and_cache_hit():
    calls = {"n": 0}

    def transport(url: str):
        calls["n"] += 1
        assert "api.open-meteo.com" in url
        return _payload()

    p = _place()
    a = fetch_weather(p, transport=transport, now=1000.0)
    assert a is not None
    assert a.summary_zh == "小雨"
    assert a.temp_c == 12.3
    assert calls["n"] == 1
    b = fetch_weather(p, transport=transport, now=1100.0, ttl_sec=1800)
    assert b is not None
    assert calls["n"] == 1  # cache hit
    assert dump_stats()["hits"] >= 1


def test_stale_window_and_transport_fail():
    def ok(_url):
        return _payload(code=95, temp=3.0)

    p = _place()
    fetch_weather(p, transport=ok, now=1000.0)
    # TTL 过期但仍在 stale 窗 → 不刷新也可用（transport 抛错）
    def boom(_url):
        raise RuntimeError("down")

    snap = fetch_weather(
        p, transport=boom, now=1000.0 + 2000, ttl_sec=1800, max_stale_sec=10800)
    assert snap is not None
    assert snap.stale is True
    assert snap.bucket == "storm"


def test_missing_coords_returns_none():
    from src.companion.persona_location import PersonaPlace
    bare = PersonaPlace(
        slug="custom", city_zh="x", city_en="x", country="",
        tz_name="UTC", lat=None, lon=None)
    assert fetch_weather(bare, transport=lambda u: _payload()) is None


def test_weather_chat_note_and_hook():
    snap = fetch_weather(_place(), transport=lambda u: _payload(code=95, temp=2.0))
    note = weather_chat_note(snap, "zh")
    assert "当地天气" in note
    assert "雷暴" in note
    hook = weather_proactive_hook(snap, "zh")
    assert hook and "雷暴" in hook
    # 普通多云不触发主动钩子
    mild = fetch_weather(
        resolve_persona_place({"location": "tokyo"}),
        transport=lambda u: _payload(code=2, temp=22.0, precip=0),
        now=time.time() + 99999,
    )
    assert weather_proactive_hook(mild) is None


def test_scene_conflicts_with_weather():
    storm = fetch_weather(_place(), transport=lambda u: _payload(code=95, temp=18.0))
    assert scene_conflicts_with_weather("sunny beach afternoon", storm) is True
    assert scene_conflicts_with_weather("cozy bedroom soft lamp", storm) is False
    hot = fetch_weather(
        resolve_persona_place({"location": "manila"}),
        transport=lambda u: _payload(code=0, temp=32.0, precip=0),
        now=time.time() + 1e6,
    )
    assert scene_conflicts_with_weather("snowy street christmas", hot) is True
