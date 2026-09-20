# -*- coding: utf-8 -*-
"""情绪补判兜底层：只在词表读不出时花一次模型调用，且永不劣化。

背景：2026-09-20 英文人设的情绪五级瀑布全部落空，每句都用同一基线语气念出来。
词表补齐英文后仍有长尾（情绪全在语境里、一个情绪词都没有），这层用小模型兜。
"""
import pytest

from src.ai.voice_emotion import EmotionSpec
from src.ai.voice_emotion_llm import (
    parse_label,
    refine_emotion,
    reset_state,
    should_consult,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """补判缓存是进程级的，不清会让用例互相喂结果。"""
    reset_state()
    yield
    reset_state()

ON = {"emotion": {"llm": {"enabled": True}}}
OFF = {"emotion": {"llm": {"enabled": False}}}
LONG = "I keep thinking about what you said in the car on the way back."


# ── 该不该调（纯函数：每条在线回复都多等半秒是听得出来的）──────────────
def test_disabled_by_default_and_when_off():
    assert should_consult(None, LONG, {}) is False
    assert should_consult(None, LONG, OFF) is False


def test_consulted_only_when_cues_stay_silent():
    assert should_consult(None, LONG, ON) is True
    # 词表已经读出情绪 → 确定性结果更可信，不花这次调用
    assert should_consult(None, "oh my god I'm so happy right now!", ON) is False


def test_short_text_skipped():
    """短句没语境，模型只会瞎猜。"""
    assert should_consult(None, "ok", ON) is False


# ── 输出解析（越界一律丢弃，别把脏标签喂给下游）──────────────────────
def test_label_must_be_a_known_emotion():
    assert parse_label("excited") == "excited"
    assert parse_label(" Angry.\n") == "angry"
    assert parse_label("bittersweet") is None
    assert parse_label("") is None


def test_abstain_and_neutral_are_not_labels():
    """判不出就弃权；neutral 比基线更平，这层不许往下压。"""
    assert parse_label("unclear") is None
    assert parse_label("neutral") is None


def test_multi_word_output_is_rejected():
    """多词＝模型在解释而不是标注，从里面挑词等于替它猜。"""
    assert parse_label("the tone is excited") is None


# ── 端到端：任何失败都退回确定性结果 ──────────────────────────────────
@pytest.mark.asyncio
async def test_model_label_replaces_emotion_but_keeps_intensity(monkeypatch):
    base = EmotionSpec("playful", intensity=0.8, pace="slow")
    import src.ai.voice_colloquial_llm as vcl

    async def _fake(ep, system, user, **kw):
        return "sad"

    monkeypatch.setattr(vcl, "_rewrite_via_endpoint", _fake)
    cfg = {"llm_endpoints": [{"base_url": "http://x/v1", "model": "m"}]}
    out = await refine_emotion(base, LONG, voice_cfg=ON, colloquial_cfg=cfg)
    assert out.emotion == "sad"
    assert out.intensity == 0.8 and out.pace == "slow"   # 只换情绪，不动强度/语速


@pytest.mark.asyncio
async def test_endpoint_failure_returns_original_spec(monkeypatch):
    base = EmotionSpec("playful", intensity=0.6)
    import src.ai.voice_colloquial_llm as vcl

    async def _boom(ep, system, user, **kw):
        raise RuntimeError("LAN down")

    monkeypatch.setattr(vcl, "_rewrite_via_endpoint", _boom)
    cfg = {"llm_endpoints": [{"base_url": "http://x/v1", "model": "m"}]}
    out = await refine_emotion(base, LONG, voice_cfg=ON, colloquial_cfg=cfg)
    assert out is base          # 下限＝和没有这层时完全一样


@pytest.mark.asyncio
async def test_no_endpoints_configured_is_a_noop():
    base = EmotionSpec("warm")
    out = await refine_emotion(base, LONG, voice_cfg=ON, colloquial_cfg={})
    assert out is base
