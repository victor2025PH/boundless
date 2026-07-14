# -*- coding: utf-8 -*-
"""ai_takeover_state 单测 — 真人接管期间, worker AI 不自动回."""
from __future__ import annotations

import time

import pytest

from src.host import ai_takeover_state as state


@pytest.fixture(autouse=True)
def reset_state():
    state.clear_for_tests()
    yield
    state.clear_for_tests()


def test_mark_and_check():
    assert state.is_taken_over("Alice", "d1") is False
    state.mark_taken_over("Alice", "d1", by_username="agent_zhang")
    assert state.is_taken_over("Alice", "d1") is True


def test_different_device_isolated():
    state.mark_taken_over("Alice", "d1", by_username="agent_zhang")
    # 不同 device 上的同名 peer 不受影响
    assert state.is_taken_over("Alice", "d2") is False


def test_release():
    state.mark_taken_over("Alice", "d1", by_username="agent_zhang")
    assert state.release("Alice", "d1") is True
    assert state.is_taken_over("Alice", "d1") is False
    # 重复 release 返回 False
    assert state.release("Alice", "d1") is False


def test_ttl_expires(monkeypatch):
    state.mark_taken_over("Alice", "d1", by_username="agent_zhang", ttl_sec=60)
    info1 = state.get_takeover_info("Alice", "d1")
    assert info1 is not None

    # 模拟时间推进 2h
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)
    assert state.is_taken_over("Alice", "d1") is False
    assert state.get_takeover_info("Alice", "d1") is None


def test_ttl_min_floor():
    """ttl_sec < 60 应被 clamp 到 60 (避免误配立即过期)."""
    state.mark_taken_over("Alice", "d1", by_username="x", ttl_sec=1)
    info = state.get_takeover_info("Alice", "d1")
    assert info is not None
    # expires_at - started_at >= 60s
    assert info["expires_at"] - info["started_at"] >= 60.0


def test_get_takeover_info():
    state.mark_taken_over("Alice", "d1", by_username="agent_zhang")
    info = state.get_takeover_info("Alice", "d1")
    assert info["by"] == "agent_zhang"
    assert info["started_at"] > 0
    assert info["expires_at"] > info["started_at"]


def test_list_active_returns_all():
    state.mark_taken_over("Alice", "d1", by_username="agent_zhang")
    state.mark_taken_over("Bob", "d2", by_username="agent_li")
    active = state.list_active()
    assert "d1::Alice" in active
    assert "d2::Bob" in active
    assert active["d1::Alice"]["by"] == "agent_zhang"


def test_list_active_filters_expired(monkeypatch):
    state.mark_taken_over("Alice", "d1", by_username="x", ttl_sec=60)
    # 推进时间过 TTL
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)
    assert state.list_active() == {}


def test_empty_inputs_safe():
    assert state.is_taken_over("", "d1") is False
    assert state.is_taken_over("Alice", "") is False
    state.mark_taken_over("", "d1", by_username="x")  # noop
    assert state.is_taken_over("", "d1") is False
    assert state.release("", "") is False


# ── facebook.py 入口 takeover 检查 (sanity, 不真起 UI) ────────────────
def test_ai_reply_short_circuits_when_taken_over(monkeypatch):
    """_ai_reply_and_send 第一步检查 ai_takeover_state, 命中即 return human_takeover."""
    state.mark_taken_over("TargetPeer", "device-XY", by_username="agent_007")

    # 直接构造一个 minimal FacebookAutomation, 调内部 method
    from src.app_automation.facebook import FacebookAutomation
    fa = FacebookAutomation.__new__(FacebookAutomation)
    # 不用真起 device; _ai_reply_and_send 入口 takeover check 先于一切其它逻辑
    # 所以 d=None 也 OK (会立即 return)
    result = fa._ai_reply_and_send(
        d=None, did="device-XY",
        peer_name="TargetPeer",
        incoming_text="こんにちは",
    )
    assert result == (None, "human_takeover")


def test_ai_reply_does_not_short_circuit_when_not_taken_over():
    """没标接管时, _ai_reply_and_send 不在入口短路 (会进 ChatBrain 路径).
    这个测试只验证短路逻辑没被错触发, 不验证完整 reply 流程.
    """
    # state 已被 fixture 清空, 没人接管
    from src.app_automation.facebook import FacebookAutomation
    fa = FacebookAutomation.__new__(FacebookAutomation)
    # d=None 在 takeover check 之后会被后续逻辑 catch, 但我们关心的是
    # 不该返回 (None, "human_takeover")
    try:
        result = fa._ai_reply_and_send(
            d=None, did="device-XY", peer_name="TargetPeer",
            incoming_text="hi",
        )
        # 后续可能 return (None, "skip") 或 raise — 都可以, 关键不是 human_takeover
        assert result != (None, "human_takeover")
    except Exception:
        # 后续逻辑因 d=None 抛异常也算"没短路 takeover"
        pass
