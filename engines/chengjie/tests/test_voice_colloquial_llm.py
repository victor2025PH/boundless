"""LLM 口语化改写门禁 — src/ai/voice_colloquial_llm.py。

守住 A 档（本地 LLM 深度口语化）的关键不变量：
  - 缓存命中不重复调 LLM（对 TTS 缓存友好 + 省调用）
  - 失败/超时/校验不过 → None（调用方回落规则档，绝不阻塞语音）
  - 端点熔断：连续失败进冷却期，冷却期直接回落（不让挂掉的端点每条拖满超时）
  - 输出消毒：剥前缀/引号/元话语段；长度/语言校验
  - 短句 / 非中文 no-op（与规则档同口径）
"""
from __future__ import annotations

import pytest

from src.ai.voice_colloquial_llm import (
    build_colloquial_prompt,
    llm_colloquialize,
    reset_state,
    sanitize_llm_output,
)


class _FakeAI:
    """假 AIClient：按 responses（定值/列表/callable）返回 rewrite_local 结果。"""

    def __init__(self, responses):
        self.calls = 0
        self._responses = responses

    async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
        self.calls += 1
        r = self._responses
        if callable(r):
            return r(user)
        if isinstance(r, list):
            return r[min(self.calls - 1, len(r) - 1)]
        return r


# ── 纯函数：prompt ───────────────────────────────────────────────────────────
def test_build_prompt_lead_toggle_and_tone():
    p_lead = build_colloquial_prompt("warm", lead=True)
    p_nolead = build_colloquial_prompt("warm", lead=False)
    assert "开头" in p_lead                       # 允许句首口语连接
    assert "不要用语气词或开场白开头" in p_nolead   # 非首条：直接说
    assert "温暖亲切" in p_lead                    # 情绪语气注入
    assert "保持原意" in p_lead                    # 信息保真硬要求


def test_build_prompt_style():
    p = build_colloquial_prompt("playful", lead=True, style="撒娇黏人")
    assert "撒娇黏人" in p


def test_build_prompt_persona_catchphrase_in_style():
    p = build_colloquial_prompt(
        "warm", lead=True,
        style="声线底色：撒娇；标志性口头禅（可自然用于句首）：哇、啊对对对")
    assert "哇" in p and "啊对对对" in p


# ── 纯函数：sanitize ─────────────────────────────────────────────────────────
def test_sanitize_strips_prefix_and_quotes():
    assert sanitize_llm_output("口语版：其实我今天挺好的啦",
                               "我今天状态不错还行呢") == "其实我今天挺好的啦"
    assert sanitize_llm_output("「其实我今天挺好的」",
                               "我今天状态不错还行呢") == "其实我今天挺好的"


def test_sanitize_meta_truncation():
    out = sanitize_llm_output("其实我今天挺好的\n\n解释：把书面语改成了口语",
                              "我今天状态不错啊啊")
    assert out == "其实我今天挺好的"


def test_sanitize_length_guard():
    orig = "我今天状态不错啊"  # 8 字
    assert sanitize_llm_output("嗯", orig) is None          # 过短
    assert sanitize_llm_output("其实" * 20, orig) is None    # 过长（发挥过度/夹带）


def test_sanitize_rejects_content_eating_shrink():
    """2026-07-27 实锤回归钉：讲故事回复被口语化腰斩（67→33，比率 0.49）竟能过
    旧 0.3 下限 → 客户听到半截故事（「怎么说话说一半呀」）。改写砍 >40% 必拒。"""
    story = ("从前有只小猫特别爱喝抹茶拿铁，每天下午都蹲在咖啡馆窗边，"
             "盯着拉花师傅做小熊图案，有一天它终于忍不住伸爪子偷喝了一口")  # 60+ 字
    half = "从前有只小猫爱喝抹茶，有一天它偷喝了一口"  # ~原文一半
    assert sanitize_llm_output(half, story) is None
    # 正常口语化（轻度压缩 ~0.8）仍应放行
    ok = ("从前有只小猫超爱抹茶拿铁，天天下午蹲咖啡馆窗边看拉花师傅画小熊，"
          "有天忍不住伸爪子偷喝了一口")
    assert sanitize_llm_output(ok, story) == ok


def test_sanitize_rejects_non_chinese_output():
    # 原文 12 字、输出 17 字（长度守卫放行）→ 只可能被语言守卫拒，精确测语言分支
    assert sanitize_llm_output("i feel pretty good today",
                               "我今天状态还不错真的挺好") is None


def test_sanitize_empty():
    assert sanitize_llm_output("", "我今天状态还不错") is None
    assert sanitize_llm_output(None, "我今天状态还不错") is None


# ── async：llm_colloquialize ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_success_and_cache():
    reset_state()
    fake = _FakeAI("其实我今天过得还不错啦")
    src = "我今天其实过得挺不错的但是有点累"
    out1 = await llm_colloquialize(src, ai_client=fake, emotion="warm")
    assert out1 == "其实我今天过得还不错啦"
    assert fake.calls == 1
    out2 = await llm_colloquialize(src, ai_client=fake, emotion="warm")
    assert out2 == out1
    assert fake.calls == 1          # 缓存命中：没再调 LLM
    reset_state()


class _DualAI:
    """假 AIClient：分别记录 rewrite_local / rewrite_cloud 的调用与返回。"""

    def __init__(self, local=None, cloud=None):
        self.local_calls = 0
        self.cloud_calls = 0
        self._local = local
        self._cloud = cloud

    async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
        self.local_calls += 1
        return self._local

    async def rewrite_cloud(self, system, user, *, timeout_sec=12.0, **kw):
        self.cloud_calls += 1
        return self._cloud


_SRC = "我今天其实过得挺不错的但是有点累"


@pytest.mark.asyncio
async def test_provider_cloud_uses_cloud_only():
    """provider=cloud：只打云端，本地节点缺模型时也不再白等一次本地往返。"""
    reset_state()
    fake = _DualAI(local="本地不该被调用的改写内容", cloud="其实我今天过得还不错啦")
    out = await llm_colloquialize(_SRC, ai_client=fake, provider="cloud")
    assert out == "其实我今天过得还不错啦"
    assert (fake.cloud_calls, fake.local_calls) == (1, 0)
    reset_state()


@pytest.mark.asyncio
async def test_provider_auto_falls_back_to_local():
    """provider=auto：云端失败（超时/未配）→ 回落本地，不直接掉规则档。"""
    reset_state()
    fake = _DualAI(local="其实我今天过得还不错啦", cloud=None)
    out = await llm_colloquialize(_SRC, ai_client=fake, provider="auto")
    assert out == "其实我今天过得还不错啦"
    assert (fake.cloud_calls, fake.local_calls) == (1, 1)
    reset_state()


@pytest.mark.asyncio
async def test_provider_default_is_local():
    """缺省仍是 local（向后兼容：未配 provider 的部署行为不变）。"""
    reset_state()
    fake = _DualAI(local="其实我今天过得还不错啦", cloud="云端不该被调用")
    out = await llm_colloquialize(_SRC, ai_client=fake)
    assert out == "其实我今天过得还不错啦"
    assert (fake.cloud_calls, fake.local_calls) == (0, 1)
    reset_state()


@pytest.mark.asyncio
async def test_provider_cloud_missing_method_degrades():
    """老客户端没有 rewrite_cloud → 返回 None 回落规则档，绝不抛。"""
    reset_state()
    fake = _FakeAI("本地改写内容不该被用到")
    assert await llm_colloquialize(_SRC, ai_client=fake, provider="cloud") is None
    assert fake.calls == 0
    reset_state()


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_none():
    reset_state()
    fake = _FakeAI(None)
    out = await llm_colloquialize("我今天其实过得挺不错的但是有点累", ai_client=fake)
    assert out is None
    reset_state()


@pytest.mark.asyncio
async def test_llm_bad_output_rejected():
    reset_state()
    fake = _FakeAI("this is a totally english rewrite that is wrong")
    out = await llm_colloquialize("我今天其实过得挺不错的但是有点累", ai_client=fake)
    assert out is None              # 串语言 → 校验不过 → 回落
    reset_state()


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures():
    reset_state()
    fake = _FakeAI(None)            # 一直失败
    for i in range(3):
        await llm_colloquialize("这是一句足够长的测试句子内容" + str(i) * 2,
                                ai_client=fake)
    assert fake.calls == 3
    # 冷却期：新文本也直接回落，不再调 LLM
    out = await llm_colloquialize("完全不同的另一句够长的内容啊啊啊", ai_client=fake)
    assert out is None
    assert fake.calls == 3          # 熔断生效，没调 LLM
    reset_state()


@pytest.mark.asyncio
async def test_short_and_non_chinese_noop():
    reset_state()
    fake = _FakeAI("不该被调用")
    assert await llm_colloquialize("好的呀", ai_client=fake) is None      # 短句
    assert await llm_colloquialize(
        "this is a fairly long english sentence here now", ai_client=fake) is None
    assert fake.calls == 0          # 短句/非中文根本不调 LLM
    reset_state()


@pytest.mark.asyncio
async def test_lead_false_reflected_in_prompt(monkeypatch):
    """colloquial_lead=False 通过 prompt 传给 LLM（缓存键也区分 lead）。"""
    reset_state()
    seen = {}

    class _Spy:
        calls = 0

        async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
            _Spy.calls += 1
            seen["system"] = system
            return "其实这句被改写得挺自然的呢"

    src = "我今天其实过得挺不错的但是有点累啊"
    await llm_colloquialize(src, ai_client=_Spy(), emotion="warm", lead=False)
    assert "不要用语气词或开场白开头" in seen["system"]
    reset_state()
