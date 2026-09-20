# -*- coding: utf-8 -*-
"""Q-6 C/D（#263 #266）：会话级 media_claim_blocked 10 轮 + spiral 阈值 2。"""
from __future__ import annotations

from src.inbox.media_claim_block import (
    BLOCK_TURNS,
    SPIRAL_THRESHOLD,
    blocked_addendum,
    is_blocked,
    note_lie_caught,
    reset_for_tests,
    spiral_count,
    tick_outbound,
)


def setup_function():
    reset_for_tests()


def test_first_complaint_blocks_ten_turns():
    rec = note_lie_caught("wa:a:c1")
    assert rec["spiral_n"] == 1
    assert rec["turns_left"] == BLOCK_TURNS
    assert is_blocked("wa:a:c1")
    assert not is_blocked("wa:a:other")
    hint = blocked_addendum("wa:a:c1")
    assert hint and "media_claim_blocked" in hint
    assert "加载" in hint or "稍后" in hint


def test_second_complaint_same_conv_hits_spiral_threshold():
    note_lie_caught("tg:b:c2")
    rec = note_lie_caught("tg:b:c2")
    assert rec["spiral_n"] == SPIRAL_THRESHOLD
    hint = blocked_addendum("tg:b:c2")
    assert "转人工" in hint
    assert spiral_count("tg:b:c2") >= 2


def test_tick_expires_block_without_clearing_spiral():
    note_lie_caught("line:x:c3")
    still = True
    for _ in range(BLOCK_TURNS):
        still = tick_outbound("line:x:c3")
    assert still is False
    assert is_blocked("line:x:c3") is False
    assert spiral_count("line:x:c3") == 1


def test_empty_conv_is_noop():
    assert note_lie_caught("")["spiral_n"] == 0
    assert is_blocked("") is False
    assert blocked_addendum("") == ""
    assert tick_outbound("") is False
