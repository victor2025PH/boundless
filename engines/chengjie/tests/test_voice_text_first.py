"""A1「先文字后语音」编排单测（2026-07-15 阶段A：3 分钟静默的解药）。

语义矩阵：
- 预算内成功 → True（语音已发，无占位、无兜底）——与旧链完全一致
- 预算内失败 → False（调用方发文字）——与旧链完全一致
- 超预算后成功 → 占位文字先发 + True；语音后台送达，不发兜底
- 超预算后失败 → 占位文字先发 + True；补发完整文字——对话绝不悬空
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from src.client.sender import TelegramSenderMixin

_LOG = logging.getLogger("test_text_first")


class _Probe:
    def __init__(self):
        self.filler_sent = 0
        self.fallback_sent = 0

    async def send_filler(self):
        self.filler_sent += 1

    async def send_fallback(self):
        self.fallback_sent += 1


async def _race(flow, budget, probe):
    task = asyncio.create_task(flow())
    return await TelegramSenderMixin.race_voice_with_text_first(
        task, budget_sec=budget, send_filler=probe.send_filler,
        send_fallback_text=probe.send_fallback, logger=_LOG)


@pytest.mark.asyncio
async def test_fast_success_identical_to_old_behavior():
    probe = _Probe()

    async def flow():
        return True

    assert await _race(flow, 1.0, probe) is True
    await asyncio.sleep(0.02)
    assert probe.filler_sent == 0 and probe.fallback_sent == 0


@pytest.mark.asyncio
async def test_fast_failure_returns_false_caller_sends_text():
    probe = _Probe()

    async def flow():
        return False

    assert await _race(flow, 1.0, probe) is False
    await asyncio.sleep(0.02)
    assert probe.filler_sent == 0 and probe.fallback_sent == 0


@pytest.mark.asyncio
async def test_slow_success_sends_filler_then_voice_no_fallback():
    probe = _Probe()

    async def flow():
        await asyncio.sleep(0.15)
        return True

    ok = await _race(flow, 0.05, probe)
    assert ok is True                       # 已接管：调用方不再发文字
    assert probe.filler_sent == 1           # 占位先行
    await asyncio.sleep(0.25)               # 等后台看护跑完
    assert probe.fallback_sent == 0         # 语音成功 → 不补文字


@pytest.mark.asyncio
async def test_slow_failure_sends_filler_then_full_text():
    probe = _Probe()

    async def flow():
        await asyncio.sleep(0.15)
        return False

    ok = await _race(flow, 0.05, probe)
    assert ok is True                       # 已接管
    assert probe.filler_sent == 1
    await asyncio.sleep(0.25)
    assert probe.fallback_sent == 1         # 语音失败 → 补完整文字，对话不悬空


@pytest.mark.asyncio
async def test_slow_flow_exception_treated_as_failure():
    probe = _Probe()

    async def flow():
        await asyncio.sleep(0.15)
        raise RuntimeError("boom")

    ok = await _race(flow, 0.05, probe)
    assert ok is True
    await asyncio.sleep(0.25)
    assert probe.fallback_sent == 1         # 异常同失败：补文字


@pytest.mark.asyncio
async def test_filler_disabled_none_still_watches():
    async def flow():
        await asyncio.sleep(0.1)
        return True

    task = asyncio.create_task(flow())
    ok = await TelegramSenderMixin.race_voice_with_text_first(
        task, budget_sec=0.02, send_filler=None,
        send_fallback_text=None, logger=_LOG)
    assert ok is True
    await asyncio.sleep(0.2)                # 看护不因 None 回调而崩
