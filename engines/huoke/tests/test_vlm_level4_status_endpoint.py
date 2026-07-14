# -*- coding: utf-8 -*-
"""`/facebook/vlm/level4/status` endpoint — 暴露 VLM Level 4 fallback
运行时状态 (provider / swap flag / counter / last_error / budget)。2026-04-24
补 ops 观察性缺口, 替代 B_OPERATIONS_GUIDE §12.5 的 REPL snippet。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset():
    """每测 reset 模块 globals 避免跨测污染。"""
    import src.app_automation.facebook as fb
    orig = {
        "inst": fb._vision_fallback_instance,
        "swap": fb._vlm_provider_swapped,
        "fail": fb._vlm_consecutive_failures,
        "att": fb._vision_fallback_init_attempted,
    }
    fb._vision_fallback_instance = None
    fb._vlm_provider_swapped = False
    fb._vlm_consecutive_failures = 0
    fb._vision_fallback_init_attempted = False
    yield
    fb._vision_fallback_instance = orig["inst"]
    fb._vlm_provider_swapped = orig["swap"]
    fb._vlm_consecutive_failures = orig["fail"]
    fb._vision_fallback_init_attempted = orig["att"]


def _ep():
    from src.host.routers.facebook import fb_vlm_level4_status
    return fb_vlm_level4_status


def test_unininitialized_returns_defaults():
    r = _ep()()
    assert r["provider"] is None
    assert r["vision_model"] is None
    assert r["swapped"] is False
    assert r["consecutive_failures"] == 0
    assert r["last_error_code"] is None
    assert r["last_error_body"] == ""
    assert r["budget"] == {}
    assert r["init_attempted"] is False


def test_init_attempted_flag_surfaces():
    import src.app_automation.facebook as fb
    fb._vision_fallback_init_attempted = True
    r = _ep()()
    assert r["init_attempted"] is True


def test_populated_instance_exposes_provider_and_budget():
    import src.app_automation.facebook as fb
    client = SimpleNamespace(
        config=SimpleNamespace(provider="gemini",
                                 vision_model="gemini-2.5-flash"),
        last_error_code=None, last_error_body="",
    )
    vf = MagicMock()
    vf._client = client
    vf.stats.return_value = {
        "hourly_used": 3, "hourly_budget": 20,
        "budget_remaining": 17, "cache_size": 2}
    fb._vision_fallback_instance = vf
    fb._vision_fallback_init_attempted = True
    r = _ep()()
    assert r["provider"] == "gemini"
    assert r["vision_model"] == "gemini-2.5-flash"
    assert r["budget"]["hourly_used"] == 3
    assert r["budget"]["budget_remaining"] == 17
    assert r["init_attempted"] is True


def test_swap_state_true_when_swapped():
    import src.app_automation.facebook as fb
    fb._vlm_provider_swapped = True
    fb._vlm_consecutive_failures = 0  # reset 后
    r = _ep()()
    assert r["swapped"] is True


def test_consecutive_failures_surfaces():
    import src.app_automation.facebook as fb
    fb._vlm_consecutive_failures = 2
    r = _ep()()
    assert r["consecutive_failures"] == 2
    assert r["swapped"] is False  # 还没 hit threshold


def test_last_error_exposed_and_body_truncated():
    import src.app_automation.facebook as fb
    client = SimpleNamespace(
        config=SimpleNamespace(provider="gemini",
                                 vision_model="gemini-2.5-flash"),
        last_error_code=503,
        last_error_body="A" * 300,  # 会被截 120
    )
    vf = MagicMock(); vf._client = client
    vf.stats.return_value = {}
    fb._vision_fallback_instance = vf
    r = _ep()()
    assert r["last_error_code"] == 503
    assert len(r["last_error_body"]) == 120


def test_timeout_last_error_body_surfaces():
    """LLMClient 把 timeout 存为 body='timeout' / code=None (P5c)"""
    import src.app_automation.facebook as fb
    client = SimpleNamespace(
        config=SimpleNamespace(provider="gemini",
                                 vision_model="gemini-2.5-flash"),
        last_error_code=None, last_error_body="timeout",
    )
    vf = MagicMock(); vf._client = client
    vf.stats.return_value = {}
    fb._vision_fallback_instance = vf
    r = _ep()()
    assert r["last_error_code"] is None
    assert r["last_error_body"] == "timeout"


def test_vf_without_client_returns_partial():
    """极端: vf instance 存在但 _client=None → 不崩, 返 partial 数据。"""
    import src.app_automation.facebook as fb
    vf = MagicMock(); vf._client = None
    vf.stats.return_value = {"hourly_used": 0, "hourly_budget": 20,
                              "budget_remaining": 20, "cache_size": 0}
    fb._vision_fallback_instance = vf
    r = _ep()()
    assert r["provider"] is None
    assert r["budget"]["hourly_budget"] == 20


def test_stats_exception_ignored():
    """vf.stats() 抛 → budget={}, 不崩。"""
    import src.app_automation.facebook as fb
    vf = MagicMock()
    vf._client = SimpleNamespace(
        config=SimpleNamespace(provider="ollama", vision_model="llava"),
        last_error_code=None, last_error_body="",
    )
    vf.stats = MagicMock(side_effect=RuntimeError("stats boom"))
    fb._vision_fallback_instance = vf
    r = _ep()()
    assert r["provider"] == "ollama"
    assert r["budget"] == {}
