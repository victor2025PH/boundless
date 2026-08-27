# -*- coding: utf-8 -*-
"""天气匹配生活照 + 关怀捕获卫生 + 日期行节令指令门禁（2026-08-18 下一阶段批次）。

背景：``scene_conflicts_with_weather`` 07 月就备好了 ``weather_snap`` 形参，但
**没有任何生产调用方真的传**——过滤休眠至今（暴雨天照样轮到 beach picnic）。
本批接线件：``snap_for_persona``（各图链共用快照解析）+ ``weather_scene_suffix``
（强天气生图氛围）+ 四个图链调用点的静态接线钉；另盖关怀捕获卫生闸
（长文报告 / 自家账号对端）与回复链日期行的节令指令。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import src.companion.weather_state as ws
from src.ai.companion_selfie import ensure_time_of_day
from src.companion.weather_state import (
    snap_for_persona,
    weather_scene_suffix,
)
from src.contacts.care_capture import make_care_inbound_cb

_PERSONA = {"location": "chengdu"}
_WCFG_ON = {"enabled": True}


def _snap(bucket="rain", code=63, temp=20.0):
    return ws.WeatherSnapshot(
        temp_c=temp, weather_code=code, humidity=None, wind_kmh=None,
        precip_mm=None, fetched_at=0.0, stale=False, place_slug="chengdu",
        summary_zh="中雨", summary_en="rain", bucket=bucket)


# ── snap_for_persona ────────────────────────────────────────────────────────

def test_snap_gate_off_returns_none(monkeypatch):
    monkeypatch.setattr(ws, "fetch_weather", lambda *a, **k: _snap())
    assert snap_for_persona(_PERSONA, None) is None
    assert snap_for_persona(_PERSONA, {"enabled": False}) is None
    assert snap_for_persona(
        _PERSONA, {"enabled": True, "scene_filter": False}) is None


def test_snap_resolves_with_gate_on(monkeypatch):
    monkeypatch.setattr(ws, "fetch_weather", lambda *a, **k: _snap())
    got = snap_for_persona(_PERSONA, _WCFG_ON)
    assert got is not None and got.bucket == "rain"
    # 无坐标人设 → None（不猜城市天气）
    assert snap_for_persona({}, _WCFG_ON) is None


def test_snap_fetch_failure_soft_none(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(ws, "fetch_weather", _boom)
    assert snap_for_persona(_PERSONA, _WCFG_ON) is None


# ── weather_scene_suffix ────────────────────────────────────────────────────

def test_suffix_only_for_strong_buckets():
    assert "rainy" in weather_scene_suffix("dorm room desk", _snap("rain"))
    assert "snowy" in weather_scene_suffix("street corner", _snap("snow"))
    assert weather_scene_suffix("dorm room desk", _snap("clear")) == ""
    assert weather_scene_suffix("dorm room desk", _snap("cloudy")) == ""
    assert weather_scene_suffix("dorm room desk", None) == ""


def test_suffix_yields_to_existing_weather_words():
    # 场景已带天气词（LLM 指令「rainy window」）→ 尊重现有描述不叠加
    assert weather_scene_suffix("rainy window seat", _snap("rain")) == ""
    assert weather_scene_suffix("sunny rooftop", _snap("rain")) == ""


# ── ensure_time_of_day 天气参数 ──────────────────────────────────────────────

def test_ensure_tod_appends_weather_after_time_phrase():
    out = ensure_time_of_day("dorm room desk", weather_snap=_snap("rain"))
    assert "dorm room desk, " in out
    assert "rainy day ambience" in out


def test_ensure_tod_time_word_shortcircuit_keeps_weather():
    # 有时间词跳过补光，但天气是「此刻的」仍要注入
    out = ensure_time_of_day("cafe at dusk", weather_snap=_snap("rain"))
    assert out.startswith("cafe at dusk")
    assert "rainy" in out


def test_ensure_tod_no_snap_byte_identical_to_old():
    assert ensure_time_of_day("cafe at dusk") == "cafe at dusk"
    out = ensure_time_of_day("dorm room desk")
    assert out.startswith("dorm room desk, ") and "rain" not in out


# ── 关怀捕获卫生闸 ───────────────────────────────────────────────────────────

class _CareStore:
    def __init__(self):
        self.calls = []

    def add_from_text(self, t, **kw):
        self.calls.append(t)
        return [1]


_CARE_CFG = NS(config={"companion": {"proactive_care": {
    "enabled": True, "capture": True}}})
_CONV = {"conversation_id": "telegram:a1:100", "platform": "telegram",
         "account_id": "a1", "chat_key": "100"}


def test_capture_normal_short_text_passes():
    st = _CareStore()
    cb = make_care_inbound_cb(st, _CARE_CFG)
    cb(_CONV, "明天下午要去面试，好紧张")
    assert len(st.calls) == 1


def test_capture_report_like_long_text_skipped():
    st = _CareStore()
    cb = make_care_inbound_cb(st, _CARE_CFG)
    cb(_CONV, "🔴 坐席问题深挖（全量日志聚类）… 请检查以下事项：" + "字" * 400)
    assert st.calls == []  # 长文报告不当私人约定（垃圾捕获事故口径）


def test_capture_max_chars_zero_disables_length_gate():
    cfgm = NS(config={"companion": {"proactive_care": {
        "enabled": True, "capture": True, "capture_max_chars": 0}}})
    st = _CareStore()
    cb = make_care_inbound_cb(st, cfgm)
    cb(_CONV, "字" * 500)
    assert len(st.calls) == 1


def test_capture_owned_peer_skipped_and_failopen():
    st = _CareStore()
    cb = make_care_inbound_cb(st, _CARE_CFG, peer_filter=lambda p, a, c: True)
    cb(_CONV, "明天要去看医生")
    assert st.calls == []  # 自家账号/bot 对端不捕获

    def _boom(p, a, c):
        raise RuntimeError("registry down")
    st2 = _CareStore()
    cb2 = make_care_inbound_cb(st2, _CARE_CFG, peer_filter=_boom)
    cb2(_CONV, "明天要去看医生")
    assert len(st2.calls) == 1  # 谓词异常 fail-open


# ── 回复链日期行：节令指令 ───────────────────────────────────────────────────

def test_local_time_line_carries_seasonal_instruction():
    from src.companion.persona_location import (
        local_time_line,
        resolve_persona_place,
    )
    place = resolve_persona_place({"location": "chengdu"})
    zh = local_time_line(place, "zh")
    assert "节令" in zh and "季节" in zh
    en = local_time_line(place, "en")
    assert "season" in en.lower()


# ── 静态接线钉（休眠形参不许再断线）─────────────────────────────────────────

def test_image_chains_wire_weather_snap():
    ia = Path("src/inbox/image_autosend.py").read_text(encoding="utf-8")
    assert ia.count("snap_for_persona") >= 2      # 生成分支 + B 线场景解析
    assert "_weather_cfg" in ia                   # resolve_cfg 捎带天气配置
    sm = Path("src/skills/skill_manager.py").read_text(encoding="utf-8")
    assert sm.count("_selfie_weather_snap") >= 4  # 定义 + Stage A + directive×2
    pt = Path("src/companion/proactive_topic.py").read_text(encoding="utf-8")
    assert "snap_for_persona" in pt               # 主动生活照场景选择


def test_care_capture_wired_with_peer_filter():
    bt = Path("src/bootstrap/background_tasks.py").read_text(encoding="utf-8")
    assert "make_care_inbound_cb(care_store, assistant.config," in bt
