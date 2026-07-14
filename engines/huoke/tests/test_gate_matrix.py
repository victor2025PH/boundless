# -*- coding: utf-8 -*-
"""门禁矩阵单元测试（无 ADB）。"""

import pytest

from src.host.gate_matrix import (
    get_effective_gate_matrix,
    resolve_gate_mode,
    resolve_requirements,
    resolve_task_tier,
)


def test_resolve_tier_longest_prefix():
    policy = {
        "default_tier": "L2",
        "tier_by_prefix": {
            "tiktok": "L1",
            "tiktok_warmup": "L2",
        },
    }
    assert resolve_task_tier("tiktok_warmup", policy) == "L2"
    assert resolve_task_tier("tiktok_follow", policy) == "L1"


def test_balanced_l1_network_only():
    policy = {
        "gate_mode": "balanced",
        "default_tier": "L2",
        "tier_by_prefix": {"tiktok_status": "L1"},
    }
    gm, tier, pf, geo, _ = resolve_requirements(policy, "tiktok_status")
    assert gm == "balanced"
    assert tier == "L1"
    assert pf == "network_only"
    assert geo is False


def test_strict_l1_still_full_preflight():
    policy = {
        "gate_mode": "strict",
        "tier_by_prefix": {"tiktok_status": "L1"},
    }
    _, tier, pf, geo, _ = resolve_requirements(policy, "tiktok_status")
    assert tier == "L1"
    assert pf == "full"
    assert geo is False


def test_unknown_gate_mode_fallback():
    policy = {"gate_mode": "nope"}
    assert resolve_gate_mode(policy) == "strict"


def test_effective_matrix_has_modes():
    m = get_effective_gate_matrix({})
    assert "balanced" in m and "L1" in m["balanced"]


def test_gate_evaluation_hint_code_serializes():
    from src.host.task_dispatch_gate import GateEvaluation, resolve_gate_hint_message

    g = GateEvaluation(False, "[gate] x", hint_code="geo_country_mismatch")
    d = g.to_dict()
    assert d.get("hint_code") == "geo_country_mismatch"
    assert d.get("hint_message") == resolve_gate_hint_message("geo_country_mismatch")
    g3 = GateEvaluation(False, "x", hint_code="preflight_network", hint_message="自定义")
    assert g3.to_dict().get("hint_message") == "自定义"
    g2 = GateEvaluation(True, "")
    assert "hint_code" not in g2.to_dict()
    assert "hint_message" not in g2.to_dict()
