# -*- coding: utf-8 -*-
"""proactive_pacing 自适应节奏单测（Phase14/15）。"""
import time

from src.integrations.companion_proactive import plan_proactive_sends


def _noon_today() -> float:
    """当天中午 12 点时间戳——planner 含安静时段(23-8)过滤，now=time.time()
    的用例在深夜必挂（2026-07-13 实锤），故钉死在安静时段之外。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                        lt.tm_wday, lt.tm_yday, -1))
from src.utils.proactive_pacing import (
    combined_pacing_score,
    effective_cooldown_hours,
    effective_min_silent_hours,
    parse_adaptive_pacing_cfg,
    stage_pacing_score,
)


def test_effective_min_silent_scales_with_intimacy():
    cfg = parse_adaptive_pacing_cfg({
        "min_silent_hours": 4,
        "adaptive_pacing": {
            "enabled": True,
            "min_silent_hours": {"base": 4, "at_intimacy_0": 8, "at_intimacy_100": 2},
        },
    })
    assert effective_min_silent_hours(0, base_hours=4, pacing_cfg=cfg) == 8.0
    assert effective_min_silent_hours(100, base_hours=4, pacing_cfg=cfg) == 2.0
    assert effective_min_silent_hours(50, base_hours=4, pacing_cfg=cfg) == 5.0


def test_stage_boosts_low_intimacy_warming():
    cfg = parse_adaptive_pacing_cfg({
        "min_silent_hours": 4,
        "adaptive_pacing": {
            "enabled": True,
            "blend": "max",
            "min_silent_hours": {"at_intimacy_0": 8, "at_intimacy_100": 2},
        },
    })
    pure_zero = effective_min_silent_hours(0, stage="", base_hours=4, pacing_cfg=cfg)
    with_warming = effective_min_silent_hours(
        0, stage="warming", base_hours=4, pacing_cfg=cfg)
    assert with_warming < pure_zero
    assert stage_pacing_score("warming") == 28.0
    assert combined_pacing_score(0, "warming", blend="max") == 28.0


def test_adaptive_pacing_filters_shallow_relationship():
    now = _noon_today()
    convs = [{
        "conversation_id": "tg:a:1", "platform": "telegram", "account_id": "a",
        "chat_key": "1", "last_ts": now - 5 * 3600.0, "last_direction": "out",
        "archived": False, "memory_key": "1", "intimacy": 0.0,
    }]
    pacing = parse_adaptive_pacing_cfg({
        "min_silent_hours": 4,
        "adaptive_pacing": {
            "enabled": True,
            "min_silent_hours": {"at_intimacy_0": 8, "at_intimacy_100": 2},
        },
    })

    def _op(**kw):
        return {"mode": "gentle_checkin", "directive": "hi", "fact": ""}

    assert plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_op, now=now,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=3,
        quiet_start_hour=23, quiet_end_hour=8, pacing_cfg=pacing) == []

    convs[0]["intimacy"] = 100.0
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_op, now=now,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=3,
        quiet_start_hour=23, quiet_end_hour=8, pacing_cfg=pacing)
    assert len(plans) == 1
    assert plans[0]["effective_min_silent_hours"] == 2.0


def test_warming_stage_allows_sooner_than_initial():
    now = _noon_today()
    pacing = parse_adaptive_pacing_cfg({
        "min_silent_hours": 4,
        "adaptive_pacing": {
            "enabled": True,
            "blend": "max",
            "min_silent_hours": {"at_intimacy_0": 8, "at_intimacy_100": 2},
        },
    })

    def _op(**kw):
        return {"mode": "gentle_checkin", "directive": "hi", "fact": ""}

    base = {
        "conversation_id": "tg:a:1", "platform": "telegram", "account_id": "a",
        "chat_key": "1", "last_ts": now - 7 * 3600.0, "last_direction": "out",
        "archived": False, "memory_key": "1", "intimacy": 0.0,
    }
    assert plan_proactive_sends(
        [{**base, "stage": "initial"}], cooldown_map={}, opener_fn=_op, now=now,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=3,
        quiet_start_hour=23, quiet_end_hour=8, pacing_cfg=pacing) == []
    plans = plan_proactive_sends(
        [{**base, "stage": "warming"}], cooldown_map={}, opener_fn=_op, now=now,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=3,
        quiet_start_hour=23, quiet_end_hour=8, pacing_cfg=pacing)
    assert len(plans) == 1


def test_effective_cooldown_disabled_uses_base():
    cfg = parse_adaptive_pacing_cfg({"min_silent_hours": 4, "adaptive_pacing": {"enabled": False}})
    assert effective_cooldown_hours(50, base_hours=6, pacing_cfg=cfg) == 6.0
