"""发送前拟人节奏协作器（humanize.run_presend_humanization）单测。

锁定：已读先于打字/延迟；打字按 refresh 分片续挂；delay<=0 不挂打字；
回调异常不抛不阻断；on_marked/on_typing 仅成功时触发。
"""
from __future__ import annotations

import asyncio

import pytest

from src.inbox.humanize import (
    compute_pacing_delay,
    estimate_thinking_delay,
    resolve_pacing,
    run_presend_humanization,
)


def _run(coro):
    return asyncio.run(coro)


async def _noop_sleep(_s):
    return None


@pytest.mark.asyncio
async def test_mark_read_before_typing_and_delay():
    events = []

    async def _mr():
        events.append("read")

    async def _tp(action):
        events.append(("typing", action))

    async def _sleep(s):
        events.append(("sleep", s))

    await run_presend_humanization(
        delay=10.0, action="typing", mark_read=_mr, typing=_tp,
        sleep=_sleep, refresh_sec=4.0)
    # 已读第一个
    assert events[0] == "read"
    # 10s → typing/sleep(4) ×2 + typing/sleep(2)
    typings = [e for e in events if isinstance(e, tuple) and e[0] == "typing"]
    sleeps = [e for e in events if isinstance(e, tuple) and e[0] == "sleep"]
    assert len(typings) == 3
    assert [s for _, s in sleeps] == [4.0, 4.0, 2.0]


@pytest.mark.asyncio
async def test_short_delay_below_guard_skips_typing():
    events = []

    async def _tp(action):
        events.append("typing")

    async def _sleep(s):
        events.append(("sleep", s))

    # delay=0.4 < min_typing_delay=1.0 → 只静默睡完，不挂打字
    await run_presend_humanization(
        delay=0.4, typing=_tp, sleep=_sleep, min_typing_delay=1.0)
    assert events == [("sleep", 0.4)]


@pytest.mark.asyncio
async def test_delay_above_guard_shows_typing():
    events = []

    async def _tp(action):
        events.append("typing")

    async def _sleep(s):
        events.append("sleep")

    # delay=3 ≥ 阈值 → 挂打字
    await run_presend_humanization(
        delay=3.0, typing=_tp, sleep=_sleep, min_typing_delay=1.0, refresh_sec=4.0)
    assert "typing" in events


@pytest.mark.asyncio
async def test_no_delay_skips_typing():
    events = []

    async def _mr():
        events.append("read")

    async def _tp(action):
        events.append("typing")

    await run_presend_humanization(
        delay=0.0, mark_read=_mr, typing=_tp, sleep=_noop_sleep)
    assert events == ["read"]        # 只已读，无打字


@pytest.mark.asyncio
async def test_record_audio_action_propagated():
    actions = []

    async def _tp(action):
        actions.append(action)

    await run_presend_humanization(
        delay=3.0, action="record_audio", typing=_tp,
        sleep=_noop_sleep, refresh_sec=4.0)
    assert actions == ["record_audio"]


@pytest.mark.asyncio
async def test_callbacks_optional():
    # 全缺省：只睡延迟，不抛
    await run_presend_humanization(delay=2.0, sleep=_noop_sleep, refresh_sec=4.0)


@pytest.mark.asyncio
async def test_mark_read_exception_does_not_raise_and_no_on_marked():
    marked = []

    async def _mr():
        raise RuntimeError("read boom")

    await run_presend_humanization(
        delay=0.0, mark_read=_mr, sleep=_noop_sleep,
        on_marked=lambda: marked.append(1))
    assert marked == []              # 失败不触发计数


@pytest.mark.asyncio
async def test_typing_exception_does_not_break_delay():
    slept = []

    async def _tp(action):
        raise RuntimeError("typing boom")

    async def _sleep(s):
        slept.append(s)

    await run_presend_humanization(
        delay=4.0, typing=_tp, sleep=_sleep, refresh_sec=4.0)
    assert slept == [4.0]            # typing 抛了照样睡完


def _norng(a, b):
    return (a + b) / 2.0  # 确定性：取区间中点，消除 jitter 随机


class TestPacingMetrics:
    def _reset(self):
        import src.integrations.humanize_metrics as hm
        hm.reset()
        return hm

    def test_enabled_sampled_and_averaged(self):
        hm = self._reset()
        blk = {"min_sec": 0, "max_sec": 60, "adaptive": True,
               "base_sec": 2.0, "per_char_sec": 0.0, "jitter": 0}
        hm.record_pacing("autosend", resolve_pacing(
            blk, text="a", arousal=0.5, elapsed_sec=0.5, rng=_norng))
        hm.record_pacing("autosend", resolve_pacing(
            blk, text="a", arousal=0.5, elapsed_sec=0.0, rng=_norng))
        snap = hm.pacing_snapshot()["autosend"]
        assert snap["count"] == 2
        assert snap["adaptive_count"] == 2
        assert snap["avg_target"] == pytest.approx(2.0, abs=1e-6)
        # delay: (1.5 + 2.0)/2 = 1.75
        assert snap["avg_delay"] == pytest.approx(1.75, abs=1e-6)
        hm.reset()

    def test_disabled_not_recorded(self):
        hm = self._reset()
        hm.record_pacing("autosend", resolve_pacing({"min_sec": 0, "max_sec": 0}, text="x"))
        assert hm.pacing_snapshot() == {}
        hm.reset()


class TestEstimateThinkingDelay:
    def test_longer_text_takes_longer(self):
        short = estimate_thinking_delay("嗯", per_char_sec=0.1, jitter=0, max_sec=60)
        long = estimate_thinking_delay("这是一段很长的回复" * 5, per_char_sec=0.1,
                                       jitter=0, max_sec=60)
        assert long > short

    def test_clamped_to_max(self):
        d = estimate_thinking_delay("字" * 1000, per_char_sec=0.1, jitter=0, max_sec=8)
        assert d == 8.0

    def test_clamped_to_min(self):
        d = estimate_thinking_delay("", base_sec=0.1, min_sec=2.0, jitter=0, max_sec=10)
        assert d == 2.0

    def test_high_arousal_faster_than_low(self):
        # 高激活度（急切/兴奋）打字更快，低激活度（平静/斟酌）更慢
        fast = estimate_thinking_delay("你怎么这样", per_char_sec=0.2, jitter=0,
                                       max_sec=60, arousal=0.9)
        slow = estimate_thinking_delay("你怎么这样", per_char_sec=0.2, jitter=0,
                                       max_sec=60, arousal=0.1)
        mid = estimate_thinking_delay("你怎么这样", per_char_sec=0.2, jitter=0,
                                      max_sec=60, arousal=0.5)
        assert fast < mid < slow

    def test_arousal_none_no_scale(self):
        a = estimate_thinking_delay("abcd", per_char_sec=0.1, jitter=0, max_sec=60)
        b = estimate_thinking_delay("abcd", per_char_sec=0.1, jitter=0, max_sec=60,
                                    arousal=0.5)
        assert a == b            # None 与中性 0.5 都不缩放

    def test_max_zero_returns_zero(self):
        assert estimate_thinking_delay("x", max_sec=0) == 0.0


class TestComputePacingDelay:
    def test_disabled_when_max_zero(self):
        assert compute_pacing_delay({"min_sec": 0, "max_sec": 0}, text="hi") == 0.0
        assert compute_pacing_delay(None, text="hi") == 0.0

    def test_non_adaptive_uniform(self):
        d = compute_pacing_delay(
            {"min_sec": 3, "max_sec": 9}, text="whatever", rng=_norng)
        assert d == 6.0           # 中点（非自适应=uniform）

    def test_adaptive_uses_length(self):
        blk = {"min_sec": 0.5, "max_sec": 30, "adaptive": True,
               "base_sec": 1.0, "per_char_sec": 0.1, "jitter": 0}
        short = compute_pacing_delay(blk, text="嗨", rng=_norng)
        long = compute_pacing_delay(blk, text="这段话要长很多很多很多", rng=_norng)
        assert long > short
        assert short >= 0.5       # 夹在 min 以上

    def test_adaptive_deducts_elapsed(self):
        blk = {"min_sec": 0, "max_sec": 30, "adaptive": True,
               "base_sec": 2.0, "per_char_sec": 0.1, "jitter": 0}
        # 目标 ≈ 2.0 + 0.1*4 = 2.4s
        full = compute_pacing_delay(blk, text="四个字啊", rng=_norng)
        # 已耗 1.0s → 还需 ≈ 1.4s
        after = compute_pacing_delay(blk, text="四个字啊", elapsed_sec=1.0, rng=_norng)
        assert after == pytest.approx(full - 1.0, abs=1e-6)

    def test_adaptive_elapsed_over_target_returns_zero(self):
        blk = {"min_sec": 0, "max_sec": 30, "adaptive": True,
               "base_sec": 1.0, "per_char_sec": 0.05, "jitter": 0}
        # 已耗时远超目标 → 立即发（0），不再等
        assert compute_pacing_delay(
            blk, text="短", elapsed_sec=100.0, rng=_norng) == 0.0

    def test_non_adaptive_ignores_elapsed(self):
        # 非自适应=纯随机延迟，语义不变，不扣 elapsed
        blk = {"min_sec": 5, "max_sec": 5, "adaptive": False}
        assert compute_pacing_delay(blk, elapsed_sec=100.0, rng=_norng) == 5.0

    def test_compute_delay_matches_resolve_delay(self):
        # compute_pacing_delay 是 resolve_pacing(...).delay 的薄封装（等价）
        blk = {"min_sec": 2, "max_sec": 8, "adaptive": True, "jitter": 0}
        assert compute_pacing_delay(blk, text="你好", rng=_norng) == \
            resolve_pacing(blk, text="你好", rng=_norng).delay


class TestResolvePacing:
    def test_disabled_when_max_zero(self):
        r = resolve_pacing({"min_sec": 0, "max_sec": 0}, text="x")
        assert r.enabled is False and r.delay == 0.0

    def test_non_adaptive_fields(self):
        r = resolve_pacing({"min_sec": 4, "max_sec": 4, "adaptive": False}, rng=_norng)
        assert r.enabled is True and r.adaptive is False
        assert r.delay == 4.0 and r.target == 4.0 and r.elapsed == 0.0

    def test_adaptive_exposes_target_and_elapsed(self):
        blk = {"min_sec": 0, "max_sec": 60, "adaptive": True,
               "base_sec": 2.0, "per_char_sec": 0.1, "jitter": 0}
        # arousal=0.5（中性）→ 不缩放，target=base+per_char×字数=2.0+0.1×4=2.4
        r = resolve_pacing(blk, text="四个字啊", arousal=0.5, elapsed_sec=1.0, rng=_norng)
        assert r.adaptive is True and r.enabled is True
        assert r.target == pytest.approx(2.4, abs=1e-6)
        assert r.elapsed == 1.0
        assert r.delay == pytest.approx(1.4, abs=1e-6)   # target - elapsed

    def test_adaptive_auto_derives_arousal_from_calm_reply(self):
        # 不显式给 arousal → 从回复文本自动估（平静回复 arousal 低 → 更慢）；
        # 显式高 arousal → 更快。验证自动派生通道联通（平静 ≥ 激动）。
        blk = {"min_sec": 0, "max_sec": 60, "adaptive": True,
               "per_char_sec": 0.3, "jitter": 0}
        calm = compute_pacing_delay(blk, text="嗯嗯我在的，你慢慢说，我一直都在", rng=_norng)
        excited = compute_pacing_delay(
            blk, text="嗯嗯我在的，你慢慢说，我一直都在", arousal=0.95, rng=_norng)
        assert calm >= excited


class TestPersonaOverrides:
    def test_override_changes_per_char_speed(self):
        blk = {"min_sec": 0, "max_sec": 60, "adaptive": True, "per_char_sec": 0.1,
               "jitter": 0,
               "persona_overrides": {
                   "fast": {"per_char_sec": 0.02},
                   "slow": {"per_char_sec": 0.3}}}
        base = resolve_pacing(blk, text="十个字的回复内容啊", arousal=0.5, rng=_norng).delay
        fast = resolve_pacing(blk, text="十个字的回复内容啊", arousal=0.5,
                              persona_id="fast", rng=_norng).delay
        slow = resolve_pacing(blk, text="十个字的回复内容啊", arousal=0.5,
                              persona_id="slow", rng=_norng).delay
        assert fast < base < slow

    def test_override_can_enable_adaptive_per_persona(self):
        # 顶层 adaptive=false（随机 uniform 中点=3），某人设覆盖为 adaptive=true
        # （base=2, per_char=0, arousal=0.5 中性不缩放 → target=2，夹在 [0,10] 不裁）
        blk = {"min_sec": 0, "max_sec": 10, "adaptive": False,
               "persona_overrides": {"p1": {"adaptive": True, "per_char_sec": 0.0,
                                            "base_sec": 2.0, "jitter": 0}}}
        r_default = resolve_pacing(blk, text="x", rng=_norng)
        r_p1 = resolve_pacing(blk, text="x", arousal=0.5, persona_id="p1", rng=_norng)
        assert r_default.adaptive is False and r_default.delay == 5.0  # uniform(0,10) 中点
        assert r_p1.adaptive is True and r_p1.target == pytest.approx(2.0, abs=1e-6)

    def test_unknown_persona_uses_top_level(self):
        blk = {"min_sec": 3, "max_sec": 3, "adaptive": False,
               "persona_overrides": {"other": {"max_sec": 99}}}
        # persona_id 不在 overrides → 顶层默认
        assert resolve_pacing(blk, persona_id="nope", rng=_norng).delay == 3.0


@pytest.mark.asyncio
async def test_on_marked_and_on_typing_counters():
    counters = {"marked": 0, "typing": 0}

    async def _mr():
        return None

    async def _tp(action):
        return None

    await run_presend_humanization(
        delay=8.0, mark_read=_mr, typing=_tp, sleep=_noop_sleep, refresh_sec=4.0,
        on_marked=lambda: counters.__setitem__("marked", counters["marked"] + 1),
        on_typing=lambda: counters.__setitem__("typing", counters["typing"] + 1))
    assert counters["marked"] == 1
    assert counters["typing"] == 2   # 8s / 4s = 2 次续挂
