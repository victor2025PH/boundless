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


# ── 占位句防复读（2026-07-26：实例池空 → 单句「稍等我一下哈～」4 分钟连发 3 次）──
def test_filler_cooldown_suppresses_second_filler():
    from src.client.sender import pick_text_first_filler
    # 冷却窗内（180s 默认）→ None＝本轮不发占位
    assert pick_text_first_filler(
        [], last_text="稍等我一下哈～", last_ts=1000.0, now=1100.0) is None
    # 出窗 → 正常出句
    assert pick_text_first_filler(
        [], last_text="稍等我一下哈～", last_ts=1000.0, now=1300.0)


def test_filler_avoids_repeating_last_wording():
    from src.client.sender import pick_text_first_filler
    pool = ["来啦来啦～", "稍等哈～"]
    for _ in range(20):
        out = pick_text_first_filler(
            pool, last_text="稍等哈～", last_ts=0.0, now=1000.0)
        assert out == "来啦来啦～"          # 唯一不同措辞必选


def test_filler_empty_pool_uses_builtin_variety():
    from src.client.sender import (_TF_FILLER_DEFAULTS,
                                   pick_text_first_filler)
    out = pick_text_first_filler([], last_text="", last_ts=0.0, now=1.0)
    assert out in _TF_FILLER_DEFAULTS
    # 内置池也避开上一条措辞
    outs = {pick_text_first_filler([], last_text=_TF_FILLER_DEFAULTS[0],
                                   last_ts=0.0, now=1.0) for _ in range(30)}
    assert _TF_FILLER_DEFAULTS[0] not in outs


def test_filler_cooldown_zero_restores_old_behavior():
    from src.client.sender import pick_text_first_filler
    assert pick_text_first_filler(
        ["稍等哈～"], last_text="", last_ts=999.0, now=1000.0,
        cooldown_sec=0) == "稍等哈～"


def test_filler_single_item_pool_never_none_out_of_cooldown():
    from src.client.sender import pick_text_first_filler
    # 池只有一句且与上一条相同：出窗时仍要出句（宁复读不悬空）
    assert pick_text_first_filler(
        ["稍等哈～"], last_text="稍等哈～", last_ts=0.0, now=1000.0) == "稍等哈～"
