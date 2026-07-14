"""主动开场发送前活跃复核（修「刚聊过几小时却发好久不见」的第二道防线）。

- should_skip_recent_active 纯函数语义
- CompanionProactiveLoop.run_once：发送前用最新 last_ts 复核，近期活跃跳过、不发不记冷却
- 仪式问候（ritual_key）不受此限
"""
from __future__ import annotations

import asyncio

import pytest

from src.integrations.companion_proactive import (
    CompanionProactiveLoop,
    should_skip_recent_active,
)


NOW = 1_000_000.0


class TestShouldSkipRecentActive:
    def test_recent_within_window_skips(self):
        # 2 小时前活跃，阈值 24h → 跳过
        assert should_skip_recent_active(
            NOW - 2 * 3600, now=NOW, min_silent_hours=24) is True

    def test_old_beyond_window_ok(self):
        # 48 小时前，阈值 24h → 不跳过
        assert should_skip_recent_active(
            NOW - 48 * 3600, now=NOW, min_silent_hours=24) is False

    def test_unknown_ts_does_not_skip(self):
        assert should_skip_recent_active(0, now=NOW, min_silent_hours=24) is False

    def test_zero_threshold_does_not_skip(self):
        assert should_skip_recent_active(
            NOW - 60, now=NOW, min_silent_hours=0) is False


class _CD:
    def __init__(self):
        self.marks = {}

    def snapshot(self):
        return dict(self.marks)

    def mark(self, k, ts):
        self.marks[k] = ts


def _loop(*, plans, fresh_map, sent_box, **kw):
    """构造一个 loop，opener 直接产出计划（绕过筛选），只测发送前复核。"""
    # conversations_provider 返回空；我们直接 monkeypatch plan_proactive_sends via opener
    # 更简单：用一个 conversations_provider + opener_fn 造出计划。
    convs = [{
        "conversation_id": cid, "platform": "telegram", "account_id": "default",
        "chat_key": cid, "last_ts": NOW - 100 * 3600,  # 快照里够沉默
        "last_direction": "out", "memory_key": cid, "stage": "", "intimacy": 50.0,
    } for cid in plans]

    def _opener(**kwargs):
        return {"mode": "follow_up", "directive": "关心一下", "context_facts": ["x"]}

    async def _send(p):
        sent_box.append(p["conversation_id"])
        return True

    return CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener,
        send_fn=_send,
        cooldown_store=_CD(),
        min_silent_hours=24.0,
        cooldown_hours=0.0,
        max_per_tick=10,
        quiet_start_hour=0, quiet_end_hour=0,   # 关闭安静时段
        fresh_activity_provider=lambda cid: fresh_map.get(cid, 0.0),
        now=lambda: NOW,
        **kw,
    )


@pytest.mark.asyncio
async def test_recent_active_skipped_at_send_time():
    """快照里够沉默，但发送前最新 last_ts 显示刚聊过 → 跳过，不发。"""
    sent = []
    loop = _loop(
        plans=["c_active", "c_silent"],
        fresh_map={
            "c_active": NOW - 2 * 3600,     # 2h 前刚聊 → 跳过
            "c_silent": NOW - 200 * 3600,   # 仍很久 → 发
        },
        sent_box=sent,
    )
    res = await loop.run_once()
    assert "c_active" not in sent
    assert "c_silent" in sent
    assert res["sent"] == 1


@pytest.mark.asyncio
async def test_no_provider_keeps_legacy():
    """无 fresh_activity_provider → 不复核，全部照发（旧行为）。"""
    sent = []
    loop = _loop(plans=["c1", "c2"], fresh_map={}, sent_box=sent)
    loop._fresh_activity_provider = None
    res = await loop.run_once()
    assert res["sent"] == 2
