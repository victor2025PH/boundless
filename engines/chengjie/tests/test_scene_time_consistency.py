# -*- coding: utf-8 -*-
"""Phase19 时间一致性：场景-时段硬冲突过滤 + LLM 场景时间兜底。

事故面：凌晨要图出"afternoon light"校园照/深夜 LLM 场景漏时间词出正午烈日照
——同一张脸也救不了时间穿帮。
"""
import datetime as dt

import pytest

from src.ai.companion_selfie import (
    ensure_time_of_day,
    pick_scene_hint,
    scene_conflicts_with_hour,
)


def _at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 7, 14, hour, 30)


# ── scene_conflicts_with_hour ────────────────────────────────────────────────
@pytest.mark.parametrize("scene,hour,conflict", [
    ("campus walkway, afternoon light", 3, True),    # 深夜 vs 下午
    ("beach at sunset, golden hour", 2, True),       # 深夜 vs 黄昏
    ("city night lights bokeh", 10, True),           # 上午 vs 夜景
    ("morning light kitchen", 19, True),             # 傍晚 vs 清晨
    ("campus walkway, afternoon light", 15, False),  # 下午 vs 下午 ✓
    ("city night lights bokeh", 23, False),          # 深夜 vs 夜景 ✓
    ("cozy dorm room, warm lamp light", 3, False),   # 中性光线词不剔
    ("convenience store, evening shift", 20, False), # 20 点属夜段，evening 不剔
    ("", 3, False),
])
def test_scene_conflicts_with_hour(scene, hour, conflict):
    assert scene_conflicts_with_hour(scene, hour) is conflict


# ── pick_scene_hint 时段过滤 ─────────────────────────────────────────────────
_POOL = [
    "university campus walkway, afternoon light",
    "cozy dorm room desk, warm lamp light",
    "convenience store, evening shift",
    "matcha cafe, soft window light",
]


def test_pick_scene_late_night_avoids_daytime():
    p = {"selfie_scenes": _POOL}
    for salt in range(8):  # 任意 salt 轮换都不许出白天场景
        sc = pick_scene_hint(p, now=_at(3), salt=salt)
        assert "afternoon" not in sc


def test_pick_scene_daytime_pool_unfiltered_keeps_rotation():
    p = {"selfie_scenes": _POOL}
    sc = pick_scene_hint(p, now=_at(15), salt=0)
    assert sc in _POOL  # 白天全池可用（无夜景词）


def test_pick_scene_all_conflicting_falls_back_to_full_pool():
    p = {"selfie_scenes": ["sunny day park, noon", "morning light kitchen"]}
    sc = pick_scene_hint(p, now=_at(2), salt=0)
    assert sc  # 全池冲突 → 回退原池，仍给场景（不空转）


def test_pick_scene_deterministic_same_bucket():
    p = {"selfie_scenes": _POOL}
    assert pick_scene_hint(p, now=_at(3), salt=1) == \
        pick_scene_hint(p, now=_at(4), salt=1)  # 同深夜段取值稳定


# ── ensure_time_of_day ───────────────────────────────────────────────────────
def test_ensure_time_appends_when_missing():
    out = ensure_time_of_day("cozy dorm room, hoodie", now=_at(3))
    assert "late night" in out and out.startswith("cozy dorm room")


@pytest.mark.parametrize("hour,expect", [
    (6, "early morning"), (9, "morning light"), (12, "midday"),
    (15, "afternoon"), (18, "golden hour"), (21, "warm indoor"),
    (23, "late night"), (2, "late night"),
])
def test_ensure_time_phrase_by_hour(hour, expect):
    assert expect in ensure_time_of_day("plain scene", now=_at(hour))


def test_ensure_time_respects_existing_time_words():
    for sc in ("library, afternoon light", "street, night bokeh",
               "beach at sunset"):
        assert ensure_time_of_day(sc, now=_at(3)) == sc  # 已带时间词不动


def test_ensure_time_empty_passthrough():
    assert ensure_time_of_day("", now=_at(3)) == ""
