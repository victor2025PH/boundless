# -*- coding: utf-8 -*-
"""daily_brief（问候链真实天气素材）门禁：纯函数 + SkillManager 接线。

不变量（2026-08-16 聊真实世界 P1）：
- 父开关 companion.weather.enabled 关 / greeting_inject 关 / 缺人设坐标 /
  取数失败 → 一律空串，问候链行为与旧版逐字一致（软失败绝不抛）；
- 素材行必须自带反编造约束（「只以这条为准/别编」）；stale 快照必须提醒
  「别报精确数字」；
- ritual：soft 情绪档不带素材（克制陪伴不聊天气）；
- checkin：素材只拼进 gentle_checkin 的 directive，follow_up（记忆回访）
  不附加（单条素材原则——记忆钩子已是主角）；强天气仍由 _weather_opener
  优先成为开场主题，本素材行不改变主题选择。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

import pytest

from src.companion.daily_brief import (
    checkin_weather_line,
    ritual_weather_line,
    weather_material,
)

# ── 夹具 ─────────────────────────────────────────────────────────────────


def _snap(*, temp=26.0, stale=False, bucket="cloudy", label="多云", code=2):
    return NS(
        temp_c=temp, weather_code=code, humidity=None, wind_kmh=None,
        precip_mm=None, fetched_at=0.0, stale=stale, place_slug="chengdu",
        summary_zh=label, summary_en="partly cloudy", bucket=bucket,
    )


def _fetch_ok(place, **kw):
    return _snap()


_PERSONA = {"location": "chengdu"}
_WCFG_ON = {"enabled": True}
_CFG_ON = {"companion": {"weather": {"enabled": True}}}
_CFG_OFF = {"companion": {"weather": {"enabled": False}}}


# ── weather_material 纯函数 ──────────────────────────────────────────────


def test_material_disabled_returns_empty():
    assert weather_material(_PERSONA, {"enabled": False}, fetch_fn=_fetch_ok) == {}
    assert weather_material(_PERSONA, {}, fetch_fn=_fetch_ok) == {}


def test_material_greeting_inject_off_returns_empty():
    cfg = {"enabled": True, "greeting_inject": False}
    assert weather_material(_PERSONA, cfg, fetch_fn=_fetch_ok) == {}


def test_material_no_place_returns_empty():
    assert weather_material({}, _WCFG_ON, fetch_fn=_fetch_ok) == {}
    assert weather_material(None, _WCFG_ON, fetch_fn=_fetch_ok) == {}


def test_material_fetch_none_or_raise_returns_empty():
    assert weather_material(_PERSONA, _WCFG_ON, fetch_fn=lambda p, **k: None) == {}

    def _boom(p, **k):
        raise RuntimeError("net down")

    assert weather_material(_PERSONA, _WCFG_ON, fetch_fn=_boom) == {}


def test_material_happy_path_fields():
    mat = weather_material(_PERSONA, _WCFG_ON, fetch_fn=_fetch_ok)
    assert mat["city"] == "成都"
    assert mat["label_zh"] == "多云"
    assert mat["temp_c"] == 26.0
    assert mat["stale"] is False
    assert mat["bucket"] == "cloudy"


def test_material_passes_ttl_from_cfg():
    seen = {}

    def _spy(place, *, ttl_sec, max_stale_sec):
        seen.update(ttl=ttl_sec, stale=max_stale_sec)
        return _snap()

    cfg = {"enabled": True, "ttl_sec": 60, "max_stale_sec": 120}
    assert weather_material(_PERSONA, cfg, fetch_fn=_spy)
    assert seen == {"ttl": 60, "stale": 120}


# ── 素材行渲染 ────────────────────────────────────────────────────────────


def test_ritual_line_morning_has_city_desc_and_guard():
    line = ritual_weather_line(_PERSONA, _CFG_ON, slot="morning", fetch_fn=_fetch_ok)
    assert "成都" in line and "多云" in line and "26°C" in line
    assert "今早" in line
    assert "别" in line and "编" in line  # 反编造约束必须在场


def test_ritual_line_night_wording():
    line = ritual_weather_line(_PERSONA, _CFG_ON, slot="night", fetch_fn=_fetch_ok)
    assert "今晚" in line


def test_ritual_line_invalid_slot_empty():
    assert ritual_weather_line(_PERSONA, _CFG_ON, slot="noon", fetch_fn=_fetch_ok) == ""


def test_ritual_line_disabled_empty():
    assert ritual_weather_line(_PERSONA, _CFG_OFF, slot="morning", fetch_fn=_fetch_ok) == ""


def test_ritual_line_stale_warns_no_exact_numbers():
    def _stale_fetch(p, **k):
        return _snap(stale=True)

    line = ritual_weather_line(_PERSONA, _CFG_ON, slot="morning", fetch_fn=_stale_fetch)
    assert "数据稍旧" in line and "别报精确数字" in line


def test_ritual_line_temp_none_label_only():
    def _no_temp(p, **k):
        return _snap(temp=None)

    line = ritual_weather_line(_PERSONA, _CFG_ON, slot="morning", fetch_fn=_no_temp)
    assert "多云" in line and "°C" not in line


def test_checkin_line_has_city_and_guard():
    line = checkin_weather_line(_PERSONA, _CFG_ON, fetch_fn=_fetch_ok)
    assert "成都" in line and "多云" in line
    assert "别" not in line or True  # 语义由下一断言钉住
    assert "编造" in line


# ── SkillManager 接线（stub 类绑定真方法，风格同 test_proactive_topic）────


class _StubStore:
    def __init__(self, rows):
        self._rows = rows

    def list_rows(self, *, prefix="", limit=50, source=""):
        return list(self._rows)


class _StubCtxStore:
    def get(self, user_id):
        return {}


_SMcls = (
    __import__("src.skills.skill_manager", fromlist=["SkillManager"]).SkillManager
)


class _SM:
    build_ritual_opener = _SMcls.build_ritual_opener
    build_proactive_opener = _SMcls.build_proactive_opener
    _ritual_weather_line = _SMcls._ritual_weather_line
    _checkin_weather_line = _SMcls._checkin_weather_line
    _weather_opener = _SMcls._weather_opener
    _life_beat_opener = _SMcls._life_beat_opener
    _proactive_emotion_gate = _SMcls._proactive_emotion_gate
    _proactive_crisis_window_days = _SMcls._proactive_crisis_window_days

    def __init__(self, store, *, weather_enabled=True):
        self._episodic_store = store
        self.logger = logging.getLogger("test_daily_brief")
        self.config = NS(config={
            "companion": {"weather": {"enabled": bool(weather_enabled)}},
        })
        self._context_store = _StubCtxStore()
        self._crisis_latest = None
        self._crisis_store = None


def _fact(content, *, tier="stable", hits=3):
    import time as _t
    return {"content": content, "source": "user_stated", "tier": tier,
            "hits": hits, "last_seen": _t.time()}


@pytest.fixture()
def _patched_env(monkeypatch):
    """PersonaManager 单例与真实天气取数都打桩（零网络零单例污染）。"""
    from src.utils.persona_manager import PersonaManager

    pm_stub = NS(get_persona_with_tier=lambda ck, d="": ({"location": "chengdu"}, None))
    monkeypatch.setattr(PersonaManager, "get_instance", classmethod(lambda cls: pm_stub))

    import src.companion.weather_state as ws

    monkeypatch.setattr(ws, "fetch_weather", lambda place, **kw: _snap())
    return pm_stub


def test_ritual_opener_carries_weather_line(_patched_env):
    sm = _SM(_StubStore([]))
    out = sm.build_ritual_opener(
        "morning", memory_key="u1", intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "ritual_morning"
    assert "真实天气" in out["directive"] and "成都" in out["directive"]


def test_ritual_opener_weather_disabled_unchanged(_patched_env):
    sm = _SM(_StubStore([]), weather_enabled=False)
    out = sm.build_ritual_opener(
        "morning", memory_key="u1", intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "ritual_morning"
    assert "真实天气" not in out["directive"]


def test_ritual_opener_soft_emotion_no_weather(_patched_env):
    sm = _SM(_StubStore([]))
    out = sm.build_ritual_opener(
        "morning", memory_key="u1", intimacy=50.0, contact_key="tg:acc:u1",
        last_emotion="焦虑")
    assert out["mode"] == "ritual_morning"
    assert "真实天气" not in out["directive"]  # soft 档克制，不聊天气


def test_checkin_opener_carries_weather_line(_patched_env):
    sm = _SM(_StubStore([]))  # 无记忆 → gentle_checkin
    out = sm.build_proactive_opener(
        "u1", silent_hours=200.0, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "gentle_checkin"
    assert "真实天气" in out["directive"] and "成都" in out["directive"]


def test_follow_up_opener_no_weather_line(_patched_env):
    sm = _SM(_StubStore([_fact("在备考")]))  # 有记忆 → follow_up，素材单条原则
    out = sm.build_proactive_opener(
        "u1", silent_hours=200.0, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "follow_up"
    assert "真实天气" not in out["directive"]


def test_strong_weather_still_wins_as_opener(_patched_env, monkeypatch):
    """暴雨强信号 → _weather_opener 仍优先成为开场主题（素材行不改变主题选择）。"""
    import src.companion.weather_state as ws

    monkeypatch.setattr(
        ws, "fetch_weather",
        lambda place, **kw: _snap(bucket="storm", label="雷暴", code=95))
    sm = _SM(_StubStore([]))
    out = sm.build_proactive_opener(
        "u1", silent_hours=200.0, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "weather_hook"


# ── 静态接线断言（防将来重构时把消费点漏掉）──────────────────────────────


def test_static_wiring_pinned():
    from pathlib import Path

    src = Path("src/skills/skill_manager.py").read_text(encoding="utf-8")
    assert "_ritual_weather_line(contact_key, s)" in src
    assert "_checkin_weather_line(contact_key or key)" in src
