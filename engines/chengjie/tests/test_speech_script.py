# -*- coding: utf-8 -*-
"""语音剧本 Speech Script v1 门禁（实施65）。

把「停哪/想哪/错哪」的决策权从 crc32/几何切分移交给 LLM 的执行层契约：
- parse_speech_script：剧本→chunks（段数出界/无标记→None 回落旧路）
- gap_ms_for_class：档位→毫秒（未知档→None 回落 gap_ms_after）
- sanitize_speech_script：预算/口误选址/事实锚点（复用 sanitize_llm_output）
- strip_speech_script：标记剥离（语义比对与非剧本路径兜底用）
- paced_synthesize：剧本自动识别（不再 ensure_think 乱插）、[breath] 决定呼吸插槽
- build_speech_script_prompt：协议关键规则钉死（防提示词被顺手删规则）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.ai.voice_colloquial_llm import (
    build_speech_script_prompt,
    sanitize_speech_script,
    strip_speech_script,
)
from src.ai import voice_pacing as vp


SCRIPT = "[breath]哎今天店里人可多了，卖得特别好‖长 嗯……你吃饭了没呀？‖短 记得歇会儿啊"


# ── parse_speech_script ──────────────────────────────────────────────────────

def test_parse_basic_segments_and_classes():
    chunks = vp.parse_speech_script(SCRIPT)
    assert chunks is not None and len(chunks) == 3
    assert chunks[0]["breath"] is True
    assert chunks[0]["gap_class"] == "长"
    assert chunks[1]["gap_class"] == "短"
    assert chunks[1]["think"] is True
    assert chunks[1]["question"] is True
    assert chunks[2]["gap_class"] == ""
    assert chunks[2]["breath"] is False


def test_parse_no_marks_returns_none():
    assert vp.parse_speech_script("没有任何标记的普通文本。") is None
    assert vp.parse_speech_script("") is None


def test_parse_segment_count_bounds():
    assert vp.parse_speech_script("只有一段‖短") is None          # 1 段（尾段空）
    many = "‖中 ".join(f"第{i}段内容够长" for i in range(11))
    assert vp.parse_speech_script(many) is None                    # 11 段出界


def test_parse_tiny_fragment_merges_into_prev():
    chunks = vp.parse_speech_script("今天真的很开心呀‖短 嗯‖中 明天见啦好不好")
    assert chunks is not None
    assert any("嗯" in c["text"] for c in chunks)
    assert len(chunks) == 2  # 「嗯」<2 字并入前段，不成独立段


# ── gap_ms_for_class ─────────────────────────────────────────────────────────

def test_gap_class_ranges_and_unknown():
    for cls, base in (("短", 300), ("中", 560), ("长", 850)):
        for seed in (0, 7, 123456):
            g = vp.gap_ms_for_class(cls, seed)
            assert base * 0.89 <= g <= base * 1.11, (cls, g)
    assert vp.gap_ms_for_class("", 1) is None
    assert vp.gap_ms_for_class("狂", 1) is None


def test_merge_to_max_carries_gap_class():
    chunks = vp.parse_speech_script(
        "第一段说个事‖短 第二段接着说‖中 第三段继续讲‖长 第四段快说完了‖短 最后一段收尾啦")
    merged = vp.merge_to_max(chunks, 3)
    assert len(merged) == 3
    # 尾段被折进倒数第二段：gap_class 随尾段走（最后可听段的停顿语义不丢）
    assert merged[-1]["gap_class"] == ""


# ── strip / sanitize ─────────────────────────────────────────────────────────

def test_strip_removes_marks_keeps_laughter():
    plain = strip_speech_script(SCRIPT)
    assert "‖" not in plain and "[breath]" not in plain
    assert "哎今天店里人可多了" in plain
    t = strip_speech_script("哈哈[laughter]真逗‖短 是呀")
    assert "[laughter]" in t  # laughter 是合成标记，保留给 CosyVoice 系消费


def test_sanitize_accepts_valid_script():
    original = "今天店里客人很多，卖得很好。你吃饭了吗？记得休息。"
    out = sanitize_speech_script(SCRIPT, original)
    assert out is not None and "‖" in out


def test_sanitize_rejects_budget_violations():
    original = "今天店里客人很多，卖得很好。你吃饭了吗？记得休息。"
    too_many_breath = ("[breath]今天店里人很多‖短 [breath]卖得特别好‖中 "
                       "[breath]你吃饭了没呀？‖短 记得歇会儿")
    assert sanitize_speech_script(too_many_breath, original) is None


def test_sanitize_strips_laughter_tags_v4_policy():
    # v4 政策（老板耳测定案）：笑声标记引擎念出来怪异 → 消毒器一律静默剥除，
    # 不废整条剧本；笑意由文字表达。
    original = "今天店里客人很多，卖得很好。你吃饭了吗？记得休息。"
    with_laughs = ("今天店里人可多了哈哈[laughter]‖短 卖得特别好‖中 "
                   "你吃饭了没呀？‖短 记得歇会儿啊")
    out = sanitize_speech_script(with_laughs, original)
    assert out is not None and "[laughter]" not in out


def test_sanitize_rejects_fix_near_anchor_and_in_last_segment():
    original = "订单是 3 月 15 号发的，你吃饭了吗？记得休息，先这样啦。"
    near_anchor = ("订单是 3 月…啊不对，15 号发的‖中 你吃饭了没呀？‖短 "
                   "记得歇会儿，先这样啦")
    assert sanitize_speech_script(near_anchor, original) is None
    original2 = "今天店里客人很多。你吃饭了吗？我先去忙了。"
    fix_last = ("今天店里客人可多了‖中 你吃饭了没呀？‖短 "
                "我先去忙了…啊不对，先歇会儿")
    assert sanitize_speech_script(fix_last, original2) is None


def test_sanitize_rejects_anchor_loss_via_existing_guard():
    original = "订单号 A1234 明天发货。你吃饭了吗？记得休息。"
    lost = "订单明天就发货啦‖中 你吃饭了没呀？‖短 记得歇会儿"   # 丢了 A1234
    assert sanitize_speech_script(lost, original) is None


def test_sanitize_flattens_multiline_and_requires_marks():
    original = "今天店里客人很多，卖得很好。你吃饭了吗？记得休息。"
    multiline = SCRIPT.replace("‖长 ", "‖长\n")
    out = sanitize_speech_script(multiline, original)
    assert out is not None and "\n" not in out
    assert sanitize_speech_script("没有标记的普通改写", original) is None


# ── paced_synthesize 剧本执行 ────────────────────────────────────────────────

def _fake_synth_factory(calls):
    import numpy as np

    def _synth(text, emo):
        calls.append((text, emo))
        sr = 24000
        t = np.linspace(0, 0.6, int(sr * 0.6), dtype=np.float32)
        x = (0.2 * np.sin(2 * 3.14159 * 220 * t)).astype(np.float32)
        return vp.float_to_wav(x, sr)

    return _synth


@pytest.mark.skipif(not vp.numpy_available(), reason="numpy 缺席")
def test_paced_synthesize_script_mode_contract():
    calls = []
    audio, meta = vp.paced_synthesize(
        SCRIPT, synth_chunk=_fake_synth_factory(calls), base_emotion="playful",
        expressive=False, tempo=1.0, think_tempo=1.0, min_chars=10,
        max_chunks=8, breath_loader=None, bed=False, inject_think=True,
        seed_key="t")
    assert meta["script"] is True
    assert meta["chunks"] == 3 and len(calls) == 3
    # 剧本模式绝不 ensure_think 乱插：送合成的文本只含 LLM 剧本原文
    assert all("‖" not in t for t, _ in calls)
    joined = "".join(t for t, _ in calls)
    assert "[breath]" not in joined            # breath 已转执行层插槽，不进合成文本
    assert joined.count("嗯") == SCRIPT.count("嗯")
    assert audio[:4] == b"RIFF"


@pytest.mark.skipif(not vp.numpy_available(), reason="numpy 缺席")
def test_paced_synthesize_plain_text_falls_back_to_old_path():
    calls = []
    plain = "今天可把我忙坏了，客人特别多。你那边一切都顺利吧？记得按时吃饭，别老熬夜啊。"
    audio, meta = vp.paced_synthesize(
        plain, synth_chunk=_fake_synth_factory(calls), base_emotion="warm",
        expressive=False, tempo=1.0, think_tempo=1.0, min_chars=10,
        max_chunks=8, breath_loader=None, bed=False, inject_think=True,
        seed_key="t")
    assert meta["script"] is False
    assert meta["chunks"] >= 2


@pytest.mark.skipif(not vp.numpy_available(), reason="numpy 缺席")
def test_paced_synthesize_script_breath_slot_follows_marks():
    import numpy as np
    calls = []
    script = ("先说说今天的事情‖中 [breath]然后我想跟你商量一件挺重要的事情呢‖短 "
              "就是那个周末的安排啦")

    def _breath_loader(sr):
        return (0.05 * np.ones(int(sr * 0.2))).astype(np.float32)

    audio, meta = vp.paced_synthesize(
        script, synth_chunk=_fake_synth_factory(calls), base_emotion="warm",
        expressive=False, tempo=1.0, think_tempo=1.0, min_chars=10,
        max_chunks=8, breath_loader=_breath_loader, bed=False,
        inject_think=True, seed_key="t")
    assert meta["script"] is True
    assert meta["breaths"] == 1   # 呼吸插槽=剧本标记位，不再按 520ms 阈值猜


# ── 生产接线钉桩 ─────────────────────────────────────────────────────────────

def test_hub_paced_respects_persona_pacing_tempo():
    """实施65 定稿契约：hub 分段路语速必须支持人设级覆写
    （voice_profile.pacing_tempo 优先于全局 pacing.tempo；陈默 1.06 靠它生效）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "ai"
           / "tts_pipeline.py").read_text(encoding="utf-8")
    assert 'vp.get("pacing_tempo")' in src, "人设级语速覆写被移除（定稿回归）"


# ── 提示词协议钉桩 ───────────────────────────────────────────────────────────

def test_prompt_pins_core_rules():
    p = build_speech_script_prompt("playful", "东北老板娘", disfluency=True)
    for needle in ("‖短", "‖中", "‖长", "意思说完", "[breath]", "[laughter]",
                   "最后一段", "数字", "人名", "东北老板娘"):
        assert needle in p, needle
    # 口误规则只在 disfluency 开启时进入
    p2 = build_speech_script_prompt("warm", "", disfluency=False)
    assert "啊不对" not in p2
