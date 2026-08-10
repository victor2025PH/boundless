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
    resolve_following_delay_block,
    resolve_pacing,
    run_presend_humanization,
)


# ── 单一节奏源收口（2026-08-07）：三条自动回复链共用的 follow 判定 ─────────
# 此前 sender（A 线）/ protocol_autoreply（协议链）/ reply_pacing_settings
# （coverage 自检）各写一份 follow 逻辑 → 改规则三处漂移。收口到本函数后，
# 这批断言是三条链共同的行为契约；下方 test_follow_resolver_is_single_source
# 另钉「三个消费方都真的在调它」，任一改回内联实现即红。

_SLIDER = {"min_sec": 8, "max_sec": 20, "adaptive": True}


class TestFollowingDelayBlock:
    def test_own_absent_follows_slider(self):
        blk, following = resolve_following_delay_block(None, _SLIDER)
        assert blk == _SLIDER and following is True

    def test_own_empty_follows_slider(self):
        blk, following = resolve_following_delay_block({}, _SLIDER)
        assert blk == _SLIDER and following is True

    def test_own_zeroed_follows_slider(self):
        # 显式 0/0 = 「没意见」而非「要秒回」→ 仍跟随（不能按「块存在」判定）
        blk, following = resolve_following_delay_block(
            {"min_sec": 0, "max_sec": 0}, _SLIDER)
        assert blk == _SLIDER and following is True

    def test_own_configured_wins(self):
        blk, following = resolve_following_delay_block(
            {"min_sec": 2, "max_sec": 5}, _SLIDER)
        assert blk == {"min_sec": 2, "max_sec": 5} and following is False

    def test_follow_false_keeps_own_even_if_empty(self):
        # 逃生阀：显式 follow:false → 用 own（空块＝resolve_pacing 解析为 0 秒回）
        blk, following = resolve_following_delay_block(
            {"follow": False}, _SLIDER)
        assert blk == {"follow": False} and following is False
        assert resolve_pacing(blk, text="x").delay == 0.0

    def test_returned_block_is_a_copy(self):
        # 返回拷贝——消费方 mutate 不得污染调用方传入的 slider/own
        blk, _ = resolve_following_delay_block({}, _SLIDER)
        blk["min_sec"] = 999
        assert _SLIDER["min_sec"] == 8

    def test_bad_types_degrade_to_follow(self):
        blk, following = resolve_following_delay_block("nope", _SLIDER)
        assert blk == _SLIDER and following is True
        blk2, following2 = resolve_following_delay_block({}, None)
        assert blk2 == {} and following2 is True


def test_follow_resolver_is_single_source():
    """三条自动回复链都必须调用共享 follow 判定——防有人改回内联实现再漂移。

    源码级断言（import 关系）：sender / protocol_autoreply / reply_pacing_settings
    都引用 resolve_following_delay_block。任一处改回自写 follow 逻辑即红。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("src/client/sender.py",
                "src/integrations/protocol_autoreply.py",
                "src/inbox/reply_pacing_settings.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "resolve_following_delay_block" in src, (
            f"{rel} 未使用共享 follow 判定——follow 规则有再次漂移的风险")


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


@pytest.mark.asyncio
async def test_typing_lead_silent_head_then_typing_tail():
    # 两段式：delay=20、lead=4 → 先静默 sleep(16)，再打字续挂 4s（typing 只出现在尾段）
    events = []

    async def _tp(action):
        events.append("typing")

    async def _sleep(s):
        events.append(("sleep", s))

    await run_presend_humanization(
        delay=20.0, typing=_tp, sleep=_sleep, refresh_sec=4.0,
        typing_lead_sec=4.0)
    assert events[0] == ("sleep", 16.0)          # 静默思考段无 typing
    assert events[1] == "typing"                 # 打字段才挂气泡
    tail = events[1:]
    assert tail.count("typing") == 1
    assert [e for e in tail if isinstance(e, tuple)] == [("sleep", 4.0)]


@pytest.mark.asyncio
async def test_typing_lead_longer_than_delay_types_whole():
    # lead ≥ delay → 无静默段，整段打字（自适应扣耗时后延迟很短的场景）
    events = []

    async def _tp(action):
        events.append("typing")

    async def _sleep(s):
        events.append(("sleep", s))

    await run_presend_humanization(
        delay=3.0, typing=_tp, sleep=_sleep, refresh_sec=4.0,
        typing_lead_sec=10.0)
    assert events == ["typing", ("sleep", 3.0)]


@pytest.mark.asyncio
async def test_typing_lead_none_keeps_legacy_full_typing():
    # 缺省 None → 旧行为：全程打字续挂（向后兼容锚）
    events = []

    async def _tp(action):
        events.append("typing")

    async def _sleep(s):
        events.append(("sleep", s))

    await run_presend_humanization(
        delay=8.0, typing=_tp, sleep=_sleep, refresh_sec=4.0)
    assert events == ["typing", ("sleep", 4.0), "typing", ("sleep", 4.0)]


@pytest.mark.asyncio
async def test_typing_lead_tiny_tail_stays_silent():
    # 打字段 < min_typing_delay → 静默补完，不闪气泡
    events = []

    async def _tp(action):
        events.append("typing")

    async def _sleep(s):
        events.append(("sleep", s))

    await run_presend_humanization(
        delay=10.0, typing=_tp, sleep=_sleep, refresh_sec=4.0,
        min_typing_delay=1.0, typing_lead_sec=0.5)
    assert "typing" not in events
    assert [s for _, s in events] == [9.5, 0.5]


class TestTypingLead:
    def test_longer_text_longer_lead(self):
        from src.inbox.humanize import estimate_typing_lead
        short = estimate_typing_lead("好", per_char_sec=0.08)
        long = estimate_typing_lead("好" * 40, per_char_sec=0.08)
        assert long > short

    def test_clamped_to_bounds(self):
        from src.inbox.humanize import estimate_typing_lead
        assert estimate_typing_lead("", per_char_sec=0.08) == 1.2
        assert estimate_typing_lead("字" * 500, per_char_sec=0.08) == 10.0

    def test_latin_weighted_lighter_than_cjk(self):
        # 同字符数：英文打字比 CJK 快（加权 0.25），lead 更短
        from src.inbox.humanize import estimate_typing_lead
        cjk = estimate_typing_lead("字" * 30, per_char_sec=0.1)
        latin = estimate_typing_lead("a" * 30, per_char_sec=0.1)
        assert latin < cjk

    def test_resolve_typing_lead_uses_persona_override_speed(self):
        # persona_overrides.slow 的 per_char_sec 更大 → lead 更长（同一手速源）
        from src.inbox.humanize import resolve_typing_lead
        blk = {"min_sec": 0, "max_sec": 60, "per_char_sec": 0.05,
               "persona_overrides": {"slow": {"per_char_sec": 0.2}}}
        base = resolve_typing_lead(blk, text="测试文本一二三四五六七八")
        slow = resolve_typing_lead(blk, text="测试文本一二三四五六七八",
                                   persona_id="slow")
        assert slow > base

    def test_resolve_typing_lead_bad_block_defaults(self):
        from src.inbox.humanize import resolve_typing_lead
        v = resolve_typing_lead({"per_char_sec": "junk"}, text="你好呀")
        assert 1.2 <= v <= 10.0

    def test_latin_rate_extends_english_lead(self):
        # 2026-08-09：latin_per_char_sec 按真实英文手速计（加权刻度虚高 ~4 倍）；
        # 缺省=旧加权行为锚
        from src.inbox.humanize import estimate_typing_lead
        legacy = estimate_typing_lead("a" * 40, per_char_sec=0.1)
        real = estimate_typing_lead("a" * 40, per_char_sec=0.1,
                                    latin_per_char_sec=0.06)
        assert legacy == pytest.approx(max(1.2, 0.6 + 40 * 0.25 * 0.1))
        assert real == pytest.approx(0.6 + 40 * 0.06)
        assert real > legacy

    def test_resolve_typing_lead_reads_latin_key(self):
        from src.inbox.humanize import resolve_typing_lead
        blk = {"per_char_sec": 0.1, "latin_per_char_sec": 0.06}
        assert resolve_typing_lead(blk, text="a" * 40) == pytest.approx(
            0.6 + 40 * 0.06)
        # 非法 latin 值回落加权刻度，不抛
        bad = resolve_typing_lead(
            {"per_char_sec": 0.1, "latin_per_char_sec": "junk"},
            text="a" * 40)
        assert bad == pytest.approx(max(1.2, 0.6 + 40 * 0.25 * 0.1))


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


class TestPlatformOverridesPacing:
    """平台节奏覆写（P1 2026-08-03）：人设 > 平台 > 全局，键级合并。"""

    def test_platform_layer_applies(self):
        blk = {"min_sec": 1, "max_sec": 5, "adaptive": False,
               "platform_overrides": {
                   "messenger": {"min_sec": 10, "max_sec": 30}}}
        r = resolve_pacing(blk, platform="messenger", rng=_norng)
        assert r.delay == 20.0   # uniform(10,30) 中点
        # 未覆写平台 / 不带平台 → 顶层默认
        assert resolve_pacing(blk, platform="line", rng=_norng).delay == 3.0
        assert resolve_pacing(blk, rng=_norng).delay == 3.0

    def test_persona_wins_over_platform(self):
        blk = {"min_sec": 1, "max_sec": 1, "adaptive": False,
               "platform_overrides": {"messenger": {"min_sec": 10, "max_sec": 10}},
               "persona_overrides": {"fast": {"min_sec": 2, "max_sec": 2}}}
        r = resolve_pacing(blk, platform="messenger", persona_id="fast",
                           rng=_norng)
        assert r.delay == 2.0   # 人设覆写压过平台覆写

    def test_key_level_merge_across_layers(self):
        # 人设只给 min、平台只给 max → min 取人设、max 取平台（键级不连坐）
        blk = {"min_sec": 1, "max_sec": 60, "adaptive": False,
               "platform_overrides": {"messenger": {"max_sec": 20}},
               "persona_overrides": {"p": {"min_sec": 10}}}
        r = resolve_pacing(blk, platform="messenger", persona_id="p",
                           rng=_norng)
        assert r.delay == 15.0   # uniform(10,20) 中点

    def test_platform_key_case_insensitive_on_lookup(self):
        blk = {"min_sec": 1, "max_sec": 1, "adaptive": False,
               "platform_overrides": {"messenger": {"min_sec": 4, "max_sec": 4}}}
        assert resolve_pacing(blk, platform="MESSENGER", rng=_norng).delay == 4.0

    def test_compute_pacing_delay_passthrough(self):
        blk = {"min_sec": 1, "max_sec": 1, "adaptive": False,
               "platform_overrides": {"line": {"min_sec": 6, "max_sec": 6}}}
        assert compute_pacing_delay(blk, platform="line", rng=_norng) == 6.0


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
