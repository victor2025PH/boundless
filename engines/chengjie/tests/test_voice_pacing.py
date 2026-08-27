# -*- coding: utf-8 -*-
"""voice_pacing（慢速拟人编排层，2026-08-19 v3 投产）门禁。

覆盖：分段/合并/思考词注入、停顿策略、人设外放判定、分段情绪、淡边、底床、
QC、端到端装配（假合成回调，零网络零 ffmpeg 依赖）、以及「不合格必须回落」
的 PacingSkip 语义。numpy 缺席整文件跳过（软依赖契约本身也在测）。
"""
from __future__ import annotations

import shutil

import pytest

np = pytest.importorskip("numpy")

from src.ai.voice_pacing import (  # noqa: E402
    PacingSkip,
    atempo_wav,
    build_bed,
    chunk_emotion,
    ensure_think,
    extract_breath,
    fade_edges,
    float_to_wav,
    gap_ms_after,
    is_expressive,
    merge_to_max,
    paced_synthesize,
    qc_frames,
    split_chunks,
    wav_to_float,
)

SR = 22050


def _tone(sec: float, f: float = 220.0, amp: float = 0.3):
    t = np.arange(int(sec * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def _tone_wav(sec: float = 0.8, pad: float = 0.10) -> bytes:
    x = np.concatenate([
        np.zeros(int(pad * SR), np.float32), _tone(sec),
        np.zeros(int(pad * SR), np.float32)])
    return float_to_wav(x, SR)


# ── 文本层 ───────────────────────────────────────────────────────────────────
def test_split_chunks_sentences_and_flags():
    cs = split_chunks("今天挺忙的。嗯……我想了想。你吃饭了吗？")
    assert [c["text"] for c in cs] == ["今天挺忙的。", "嗯……我想了想。", "你吃饭了吗？"]
    assert [c["think"] for c in cs] == [False, True, False]
    assert cs[2]["question"] is True


def test_split_chunks_long_sentence_comma_cut_and_tiny_merge():
    long = "这个方案我看过了整体思路没有问题，但是有三个地方要改预算拆得太粗，时间线也太乐观了要留缓冲。"
    cs = split_chunks(long + "嗯。")
    assert len(cs) >= 2
    assert cs[0]["comma_tail"] is True          # 长句从逗号切开
    assert all(len(c["text"]) >= 5 for c in cs)  # <5 字碎尾（嗯。）并入前段


def test_merge_to_max_keeps_text():
    cs = split_chunks("第一句话在这里说完。第二句话跟着到位。第三句话继续讲。最后一句是问句吗？")
    assert len(cs) == 4
    merged = merge_to_max(cs, 2)
    assert len(merged) == 2
    assert "".join(c["text"] for c in merged) == "".join(c["text"] for c in cs)
    assert merged[-1]["question"] is True        # 尾段语义（问句）跟合并走


def test_ensure_think_injects_once_deterministically():
    cs = split_chunks("第一句话说完了。第二句话在这里。第三句话收尾。")
    assert not any(c["think"] for c in cs)
    out1 = ensure_think([dict(c) for c in cs], seed=12345)
    out2 = ensure_think([dict(c) for c in cs], seed=12345)
    assert sum(c["think"] for c in out1) == 1
    assert [c["text"] for c in out1] == [c["text"] for c in out2]  # 确定性
    already = ensure_think([dict(c) for c in split_chunks("嗯……好。可以的呀。")], 7)
    assert sum(c["think"] for c in already) == 1  # 已有思考词不再注入


def test_gap_policy_ordering_and_think_bonus():
    comma = {"comma_tail": True, "question": False}
    sent = {"comma_tail": False, "question": False}
    q = {"comma_tail": False, "question": True}
    nxt = {"think": False}
    nxt_think = {"think": True}
    g_comma = gap_ms_after(comma, nxt, seed=0)
    g_sent = gap_ms_after(sent, nxt, seed=0)
    g_q = gap_ms_after(q, nxt, seed=0)
    assert g_comma < g_sent < g_q
    assert gap_ms_after(sent, nxt_think, 0) > g_sent   # 思考词前额外停顿
    assert gap_ms_after(sent, None, 0) == 0            # 末段无停顿
    for seed in range(40):                             # 抖动有界 ±15%
        assert 560 * 0.84 <= gap_ms_after(sent, nxt, seed) <= 560 * 1.16


def test_is_expressive_matches_eval_calibration():
    assert is_expressive("沉稳", "serious") is False      # marcus/zhang/chen_mo
    assert is_expressive("沉稳", "warm") is True          # zhao_laoshi
    assert is_expressive("俏皮", "playful") is True       # lin_xiaoyu
    assert is_expressive("温柔", "serious") is True       # chen_meiling
    assert is_expressive("", "serious") is False


def test_chunk_emotion_accents():
    assert chunk_emotion("今天好开心哈哈", "gentle", expressive=True,
                         think=False) == "happy"
    assert chunk_emotion("今天好开心哈哈", "serious", expressive=False,
                         think=False) == "serious"      # 克制型钉基调
    assert chunk_emotion("嗯……我想想", "gentle", expressive=True,
                         think=True) == "gentle"        # 思考段不跳
    assert chunk_emotion("唉，好累啊", "happy", expressive=True,
                         think=False) == "gentle"       # 疲惫内容收着


# ── 音频层 ───────────────────────────────────────────────────────────────────
def test_fade_edges_zeroes_endpoints():
    x = np.ones(SR, np.float32)
    y = fade_edges(x, SR, ms=15.0)
    assert abs(float(y[0])) < 1e-3 and abs(float(y[-1])) < 1e-3
    assert float(y[SR // 2]) == pytest.approx(1.0)


def test_build_bed_residue_vs_noise_fallback():
    rng = np.random.default_rng(1)
    residues = [rng.standard_normal(2000).astype(np.float32) * 0.003
                for _ in range(4)]
    bed, src = build_bed(residues, SR, SR, seed=9)
    assert src == "residue" and bed.size == SR
    db = 20 * np.log10(float(np.sqrt(np.mean(bed ** 2))) + 1e-12)
    assert -60.0 < db < -48.0                          # 响度夹在带内
    bed2, src2 = build_bed([], SR, SR, seed=9)
    assert src2 == "noise"
    db2 = 20 * np.log10(float(np.sqrt(np.mean(bed2 ** 2))) + 1e-12)
    assert -58.0 < db2 < -54.0                          # -56dB 暖褐噪


def test_build_bed_exact_length_with_many_tiny_residues():
    """回归钉（2026-08-19 方言 demo broadcast 崩溃）：交叉淡接吃重叠样本，
    残料碎（段多）时床必须仍精确等长，否则 track+bed 广播崩。"""
    rng = np.random.default_rng(2)
    tiny = [rng.standard_normal(int(0.04 * SR)).astype(np.float32) * 0.003
            for _ in range(60)]                        # 40ms × 60 段全碎料
    total = 5 * SR
    bed, src = build_bed(tiny, total, SR, seed=11)
    assert src == "residue" and bed.size == total


def test_qc_detects_dead_air_and_bed_fixes_it():
    track = np.concatenate([_tone(0.5), np.zeros(SR, np.float32), _tone(0.5)])
    min_db, ok = qc_frames(track, SR)
    assert not ok and min_db <= -70.0                   # 纯数字零静音=死寂
    bed, _ = build_bed([], track.size, SR, seed=3)
    min_db2, ok2 = qc_frames(track + bed, SR)
    assert ok2 and min_db2 > -70.0


def test_extract_breath_gates():
    rng = np.random.default_rng(5)
    floor = rng.standard_normal(int(0.6 * SR)).astype(np.float32) * 1e-4
    breath = rng.standard_normal(int(0.30 * SR)).astype(np.float32)
    # 带通到呼吸带（500-2200Hz）
    B = np.fft.rfft(breath)
    fq = np.fft.rfftfreq(breath.size, 1 / SR)
    B[(fq < 500) | (fq > 2200)] = 0
    breath = np.fft.irfft(B, breath.size).astype(np.float32)
    breath *= 0.02 / (np.sqrt(np.mean(breath ** 2)) + 1e-12)
    voiced = _tone(1.5, f=220, amp=0.5)
    ref = np.concatenate([floor, breath, floor[: int(0.3 * SR)], voiced, floor])
    got = extract_breath(ref, SR)
    assert got is not None and 0.10 * SR <= got.size <= 0.60 * SR
    # 全程说话（无安静前置）→ 宁缺毋滥返 None
    assert extract_breath(_tone(3.0, amp=0.4), SR) is None


# ── 端到端装配（假合成回调）──────────────────────────────────────────────────
def _fake_synth_factory(calls: list, sr_second: int = SR):
    def synth(text: str, emotion: str) -> bytes:
        calls.append((text, emotion))
        if len(calls) == 2 and sr_second != SR:
            x, _ = wav_to_float(_tone_wav())
            return float_to_wav(x, sr_second)
        return _tone_wav()
    return synth


def test_paced_synthesize_end_to_end():
    calls: list = []
    text = "今天店里特别忙，忙得脚不沾地。刚才对完账小赚了一点哈哈。你那边忙完了没有？"
    wav, meta = paced_synthesize(
        text, synth_chunk=_fake_synth_factory(calls), base_emotion="gentle",
        expressive=True, polish=lambda s: s.rstrip("。"),
        tempo=1.0, think_tempo=1.0,           # 免 ffmpeg 依赖，确定性
        breath_loader=None, bed=True, seed_key="t")
    x, sr = wav_to_float(wav)
    assert sr == SR
    assert len(calls) == meta["chunks"] >= 3   # 逐段合成（含注入的思考段）
    speech_sec = len(calls) * 1.0              # 每段 1.0s（0.8 音 + 0.2 pad，pad 被修边）
    assert x.size / SR > speech_sec * 0.8 + 0.6  # 停顿真实存在（时长>语音本体）
    assert meta["bed"] in ("residue", "noise")
    assert meta["min_frame_db"] > -70.0        # QC 无死寂帧
    assert any(e == "happy" for _, e in calls)  # 「哈哈」段跳了 happy
    assert all("。" not in t[-1:] for t, _ in calls)  # polish 每段生效（尾句号被剥）


def test_paced_synthesize_skips_short_and_single_chunk():
    with pytest.raises(PacingSkip):
        paced_synthesize("太短了。", synth_chunk=lambda t, e: _tone_wav(),
                         min_chars=24)
    with pytest.raises(PacingSkip):
        paced_synthesize("这一句凑够了二十四个字但是没有第二句可以切分呀",
                         synth_chunk=lambda t, e: _tone_wav(), min_chars=10)


def test_paced_synthesize_rejects_sr_mismatch():
    calls: list = []
    with pytest.raises(PacingSkip):
        paced_synthesize(
            "第一句话在这里说完。第二句话跟着到位。第三句话继续讲。",
            synth_chunk=_fake_synth_factory(calls, sr_second=16000),
            tempo=1.0, think_tempo=1.0, inject_think=False,
            breath_loader=None, bed=True, seed_key="m")


def test_breath_loader_none_and_failure_tolerated():
    text = "第一句话在这里说完。第二句话跟着到位。第三句话继续讲。"

    def boom(sr: int):
        raise RuntimeError("ref unavailable")
    wav, meta = paced_synthesize(
        text, synth_chunk=lambda t, e: _tone_wav(), tempo=1.0, think_tempo=1.0,
        breath_loader=boom, bed=True, inject_think=False, seed_key="b")
    assert meta["breaths"] == 0                # 提不到呼吸=不放，绝不合成凑数


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 缺席")
def test_atempo_slows_down():
    src = _tone_wav(1.0)
    out = atempo_wav(src, 0.90, SR)
    x0, _ = wav_to_float(src)
    x1, _ = wav_to_float(out)
    assert x1.size > x0.size * 1.05            # 0.90 变速 → 时长 ~+11%


def test_atempo_noop_when_factor_one():
    src = _tone_wav(0.5)
    assert atempo_wav(src, 1.0, SR) == src
