# -*- coding: utf-8 -*-
"""「音频异常（疑似无声）」误报除根（#121 三进宫，2026-09-02 KKXSTU 诊断包）。

事故：1.0.70 上 ``tmp_tts_preview/tts-20260902-202949-c0592a3d.wav`` 被 Whisper
全文转录成功（"what are you up to right now you haven't eaten yet have you…"），
前端红条照亮。前两轮修的是浏览器端解码启发式的阈值——本轮把终审交给服务端：
**ASR 能从产物转出与送稿相关的文字即有声**（终审白名单）+ 落盘字节能量。

金标夹具：``tests/fixtures/gold_clone_indextts_22k.wav``——真实 IndexTTS-2 克隆产物
（22.05k/16bit/C2PA 尾块，与 KKXSTU 那份 wav 同引擎同容器）。用它钉：
  · 服务端能量探测必须判「有能量」；
  · 前端旧双信号阈值（峰值<0.002 且 RMS<0.0008）对真实样本本就不该触发——
    真样本峰值 ~0.7；误报来自解码/容器层而非阈值，故裁决改由服务端下发；
  · KKXSTU 形态（转写命中、CER≈0）无论能量探测怎么说都判 voiced。
"""
from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from src.ai.speech_verdict import (
    TRANSCRIPT_MAX_CER,
    TRANSCRIPT_MIN_CHARS,
    judge_preview_speech,
    speech_verdict,
)

GOLD = Path(__file__).parent / "fixtures" / "gold_clone_indextts_22k.wav"

# KKXSTU 20:30:18 那次回验的形状：英文外语轨、转写全文命中
_KKXSTU_SV = {"cer": 0.03, "retried": 0, "lang": "en", "hyp_chars": 88}


def _silent_wav(path: Path, sec: float = 1.2, sr: int = 22050) -> Path:
    n = int(sec * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)
    return path


def _pcm_stats(path: Path):
    """与 cp-voice 旧客户端探测同口径：float 归一峰值/RMS（步进抽样 ≤48k 点）。"""
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
    cnt = len(frames) // 2
    vals = struct.unpack(f"<{cnt}h", frames[:cnt * 2])
    step = max(1, cnt // 48000)
    peak, ss, k = 0.0, 0.0, 0
    for i in range(0, cnt, step):
        a = abs(vals[i]) / 32768.0
        peak = max(peak, a)
        ss += a * a
        k += 1
    return peak, (ss / k) ** 0.5 if k else 0.0


# ══ 1. 纯裁决矩阵 ═══════════════════════════════════════════════════════════

def test_kkxstu_transcript_is_final_whitelist():
    """转写命中（Whisper 出了与送稿相关的文字）→ 有声，能量探测不管怎么说。"""
    assert speech_verdict(_KKXSTU_SV, None)["speech"] == "voiced"
    v = speech_verdict(_KKXSTU_SV, False)
    assert v["speech"] == "voiced" and v["basis"] == "transcript+energy"
    v2 = speech_verdict(_KKXSTU_SV, True)      # 探测器与转写矛盾 → 转写为准
    assert v2["speech"] == "voiced" and "conflict" in v2["basis"]
    assert v2["transcript_chars"] == 88 and v2["energy"] == "silent"


def test_energy_alone_decides_when_no_transcript():
    assert speech_verdict(None, True) == {
        "speech": "silent", "basis": "energy", "transcript_chars": 0, "energy": "silent"}
    assert speech_verdict({}, False)["speech"] == "voiced"
    assert speech_verdict({}, False)["basis"] == "energy"
    assert speech_verdict(None, None)["speech"] == "unknown"


def test_hallucination_or_unrelated_transcript_not_evidence():
    """ASR 对静音幻觉出一两个字 / 转出与送稿无关的字 → 不算有声证据。"""
    weak = {"cer": 1.0, "retried": 0, "hyp_chars": TRANSCRIPT_MIN_CHARS - 1}
    assert speech_verdict(weak, None)["speech"] == "unknown"
    assert speech_verdict(weak, True)["speech"] == "silent"
    unrelated = {"cer": TRANSCRIPT_MAX_CER + 0.1, "retried": 1, "hyp_chars": 60}
    assert speech_verdict(unrelated, None)["speech"] == "unknown"
    assert speech_verdict(unrelated, False)["speech"] == "voiced"   # 能量兜住


def test_verdict_tolerates_garbage_shapes():
    assert speech_verdict({"cer": "x", "hyp_chars": "y"}, None)["speech"] == "unknown"
    assert speech_verdict("not-a-dict", False)["speech"] == "voiced"


# ══ 2. 金标真实样本 ═══════════════════════════════════════════════════════════

def test_gold_fixture_present_and_real_speech():
    assert GOLD.is_file(), "金标夹具缺失（.gitignore 须放行 tests/fixtures/gold_*.wav）"
    peak, rms = _pcm_stats(GOLD)
    # 真实克隆产物的量级：旧前端双信号阈值本就不该触发（0.002/0.0008）
    assert peak > 0.3 and rms > 0.02, (peak, rms)
    assert not (peak < 0.002 and rms < 0.0008)


def test_gold_fixture_energy_only_is_voiced():
    v = judge_preview_speech(GOLD, "wav", None)
    assert v["speech"] == "voiced" and v["basis"] == "energy" and v["energy"] == "ok"


def test_gold_fixture_with_kkxstu_transcript_is_voiced():
    v = judge_preview_speech(GOLD, "wav", _KKXSTU_SV)
    assert v["speech"] == "voiced" and v["basis"] == "transcript+energy"
    assert v["transcript_chars"] == 88


def test_gold_fixture_mislabeled_format_still_voiced():
    """预览文件此前按配置后缀命名（.mp3 装 WAV 字节）：能量探测按 RIFF 魔数走，不受标签误导。"""
    v = judge_preview_speech(GOLD, "mp3", None)
    assert v["speech"] == "voiced"


# ══ 3. 真静音空壳照拦 ════════════════════════════════════════════════════════

def test_digital_silence_is_silent(tmp_path):
    p = _silent_wav(tmp_path / "hollow.wav")
    v = judge_preview_speech(p, "wav", None)
    assert v["speech"] == "silent" and v["energy"] == "silent"


def test_digital_silence_with_short_hallucination_still_silent(tmp_path):
    p = _silent_wav(tmp_path / "hollow2.wav")
    v = judge_preview_speech(p, "wav", {"cer": 1.0, "retried": 0, "hyp_chars": 2})
    assert v["speech"] == "silent"


# ══ 4. 判不了 → unknown（前端才用自己的启发式兜底） ═══════════════════════════

def test_undecodable_blob_is_unknown(tmp_path):
    p = tmp_path / "blob.mp3"
    p.write_bytes(b"AAAA" * 64)
    v = judge_preview_speech(p, "mp3", None)
    assert v["speech"] == "unknown" and v["energy"] == "unknown"
    assert judge_preview_speech(tmp_path / "missing.wav", "wav", None)["speech"] == "unknown"


def test_undecodable_but_transcribed_is_voiced(tmp_path):
    """容器判不了但 ASR 转出了字（KKXSTU 的另一半形态）→ 仍是有声。"""
    p = tmp_path / "blob2.ogg"
    p.write_bytes(b"OggS" + b"\x00" * 200)
    v = judge_preview_speech(p, "ogg", _KKXSTU_SV)
    assert v["speech"] == "voiced" and v["basis"] == "transcript"


# ══ 5. synth_verify 证据链：回验结果必须带 hyp_chars ═══════════════════════════

def test_synth_verify_reports_transcript_chars(tmp_path):
    import asyncio

    from src.ai.tts_pipeline import verify_and_retry_synth

    class _T:
        async def transcribe_voice_message(self, path, language="zh"):
            return "what are you up to right now you haven't eaten yet have you"

    av = tmp_path / "o.wav"
    av.write_bytes(b"v1")
    text = "What are you up to right now? You haven't eaten yet, have you?"
    info = asyncio.run(verify_and_retry_synth(
        av, text, {"enabled": True, "cer_threshold": 0.30, "max_retries": 1},
        _T(), lambda: None))
    assert info["hyp_chars"] >= TRANSCRIPT_MIN_CHARS
    assert info["lang"] == "en" and info["cer"] <= 0.05
    assert speech_verdict(info, None)["speech"] == "voiced"


@pytest.mark.parametrize("fmt", ["wav", "mp3", "ogg"])
def test_route_shape_voice_meta_keys(fmt):
    """tts-test 响应契约：voice_meta.speech ∈ {voiced,silent,unknown}（前端特性探测）。"""
    v = speech_verdict(None, None)
    assert set(v) == {"speech", "basis", "transcript_chars", "energy"}
    assert v["speech"] in ("voiced", "silent", "unknown")
