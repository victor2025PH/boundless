"""参考音质量审计门禁 — src/ai/reference_audio_audit.py（Phase E）。

守住的不变量：
  - 韵律平淡（能量+音高双低）的参考音必被点名——Phase E「换带情绪起伏的参考音」
    的决策依据，漏报=运营继续用念稿素材，克隆声天然像播报；
  - 削波/过短/首尾静音/缺逐字稿等硬伤如实报告；
  - 健康素材不误伤（ok 级零 issues）；
  - 脏输入（非 WAV/缺文件）→ bad 级，绝不抛。

附带守卫探针 Phase F 增量：A/B off 行不得污染自然度校准地板 +
tts() per-call prosody 覆盖真的落进请求体。
"""
from __future__ import annotations

import base64
import io
import json
import wave

import numpy as np
import pytest

from src.ai.reference_audio_audit import (
    analyze_wav_bytes,
    audit_reference_file,
    classify_reference,
)


def _make_wav(samples: np.ndarray, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(
            (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _tone(sr: int, dur: float, freq: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(sr * dur)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float64)


def _expressive(sr: int = 16000, dur: float = 6.0) -> np.ndarray:
    """频率 120→300Hz 扫 + 幅度 0.05..0.9 调制 → 韵律「丰富」的合成素材。"""
    t = np.arange(int(sr * dur)) / sr
    freq = 120 + (300 - 120) * (t / dur)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    amp = 0.05 + 0.85 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t)) ** 2
    return amp * np.sin(phase)


# ── analyze_wav_bytes ────────────────────────────────────────────────────────
def test_analyze_flat_tone_reports_low_dynamics():
    m = analyze_wav_bytes(_make_wav(_tone(16000, 5.0, 150.0)))
    assert m["ok"] is True
    assert m["duration_sec"] == pytest.approx(5.0, abs=0.1)
    assert m["f0_semi_std"] < 1.0          # 恒定音高 → 音高动态≈0
    assert m["energy_db_std"] < 3.0        # 恒定幅度 → 能量动态≈0
    assert m["voiced_ratio"] > 0.8


def test_analyze_expressive_reports_rich_dynamics():
    m = analyze_wav_bytes(_make_wav(_expressive()))
    assert m["ok"] is True
    assert m["f0_semi_std"] > 2.5          # 扫频 → 音高动态足
    assert m["energy_db_std"] > 6.0        # 幅度调制 → 能量动态足


def test_analyze_detects_clipping():
    m = analyze_wav_bytes(_make_wav(1.6 * _tone(16000, 4.0, 200.0, amp=1.0)))
    assert m["ok"] is True
    assert m["clip_ratio"] > 0.01


def test_analyze_detects_edge_silence():
    sr = 16000
    audio = np.concatenate([np.zeros(int(sr * 2.2)), _tone(sr, 4.0, 180.0)])
    m = analyze_wav_bytes(_make_wav(audio, sr))
    assert m["ok"] is True
    assert m["lead_silence_sec"] > 1.5


def test_analyze_rejects_garbage_and_too_short():
    assert analyze_wav_bytes(b"not a wav at all")["ok"] is False
    assert analyze_wav_bytes(b"")["ok"] is False
    m = analyze_wav_bytes(_make_wav(_tone(16000, 0.1, 150.0)))
    assert m["ok"] is False


# ── classify_reference ───────────────────────────────────────────────────────
def _healthy_metrics() -> dict:
    return {
        "ok": True, "duration_sec": 7.0, "sample_rate": 24000, "channels": 1,
        "peak": 0.8, "clip_ratio": 0.0, "lead_silence_sec": 0.2,
        "trail_silence_sec": 0.3, "energy_db_std": 10.5, "f0_semi_std": 5.2,
        "voiced_ratio": 0.62,
    }


def test_classify_healthy_reference_is_ok():
    v = classify_reference(_healthy_metrics(), has_sidecar=True)
    assert v["level"] == "ok"
    assert v["issues"] == []


def test_classify_flat_prosody_flagged_with_actionable_tip():
    m = _healthy_metrics()
    m["energy_db_std"] = 4.0
    m["f0_semi_std"] = 1.8
    v = classify_reference(m, has_sidecar=True)
    assert v["level"] == "warn"
    assert any("韵律平淡" in s for s in v["issues"])
    assert any("情绪起伏" in s for s in v["tips"])   # 动作句，不是指标复读


def test_classify_missing_sidecar_flagged():
    v = classify_reference(_healthy_metrics(), has_sidecar=False)
    assert v["level"] == "warn"
    assert any("逐字稿" in s for s in v["issues"])


def test_classify_duration_and_clip_and_silence():
    m = _healthy_metrics()
    m.update({"duration_sec": 1.5, "clip_ratio": 0.02, "lead_silence_sec": 2.0})
    v = classify_reference(m, has_sidecar=True)
    joined = "；".join(v["issues"])
    assert "过短" in joined and "削波" in joined and "静音" in joined


def test_classify_bad_metrics():
    v = classify_reference({"ok": False, "detail": "decode failed"},
                           has_sidecar=False)
    assert v["level"] == "bad"


# ── audit_reference_file ─────────────────────────────────────────────────────
def test_audit_missing_file_is_bad():
    r = audit_reference_file("Z:/definitely/absent/ref.wav")
    assert r["level"] == "bad"


def test_audit_end_to_end_with_sidecar(tmp_path):
    wav = tmp_path / "ref.wav"
    wav.write_bytes(_make_wav(_expressive()))
    (tmp_path / "ref.txt").write_text("参考音逐字稿内容", encoding="utf-8")
    r = audit_reference_file(str(wav))
    assert r["has_sidecar"] is True
    assert r["metrics"].get("ok") is True
    # 合成素材韵律丰富 → 不该报「韵律平淡」
    assert not any("韵律平淡" in s for s in r["issues"])


# ── pick_best_segment（裁剪选段）─────────────────────────────────────────────
def test_pick_best_segment_prefers_expressive_half():
    """前半平淡（恒定音）+ 后半表现力（扫频调幅）→ 选段落在后半。"""
    from src.ai.reference_audio_audit import pick_best_segment
    sr = 16000
    flat = _tone(sr, 10.0, 150.0)
    rich = _expressive(sr, 10.0)
    a = np.concatenate([flat, rich])
    s0, s1 = pick_best_segment(a, sr, target_sec=6.0)
    assert s0 >= 9.0           # 窗口主体在表现力段（允许吸附些许提前）
    assert 5.0 <= (s1 - s0) <= 7.5


def test_pick_best_segment_strips_lead_silence():
    from src.ai.reference_audio_audit import pick_best_segment
    sr = 16000
    a = np.concatenate([np.zeros(int(sr * 2.0)), _expressive(sr, 5.0)])
    s0, s1 = pick_best_segment(a, sr, target_sec=8.0)
    assert s0 >= 1.8           # 头部静音被剥掉
    assert s1 <= 7.2


def test_pick_best_segment_short_audio_returns_whole():
    from src.ai.reference_audio_audit import pick_best_segment
    sr = 16000
    a = _expressive(sr, 4.0)
    s0, s1 = pick_best_segment(a, sr, target_sec=8.0)
    assert s0 <= 0.2 and s1 >= 3.6


def test_write_wav_mono_roundtrip(tmp_path):
    from src.ai.reference_audio_audit import analyze_wav_bytes, write_wav_mono
    sr = 16000
    out = tmp_path / "trim.wav"
    write_wav_mono(_expressive(sr, 5.0), sr, str(out))
    m = analyze_wav_bytes(out.read_bytes())
    assert m["ok"] is True and m["channels"] == 1
    assert m["duration_sec"] == pytest.approx(5.0, abs=0.1)


# ── Phase F：探针 A/B 不污染校准 + per-call prosody 覆盖 ─────────────────────
def test_calibrate_floor_ignores_ab_off_rows():
    from scripts.voice_similarity_probe import calibrate_naturalness_floor
    rows = ([{"naturalness": 0.9, "prosody": "on"}] * 15
            + [{"naturalness": 0.2, "prosody": "off"}] * 15)
    floor = calibrate_naturalness_floor(rows, min_n=15, margin=0.05)
    assert floor > 0.8      # off 组 0.2 未把 p10 拉塌


def test_tts_per_call_prosody_override(monkeypatch):
    from src.ai.avatar_voice import AvatarVoiceClient
    client = AvatarVoiceClient({
        "enabled": True,
        "prosody": {"enabled": True, "flow_temperature": 1.08, "llm_top_k": 32},
    })
    captured: list = []

    def fake_post(url, payload, *, timeout):
        captured.append(json.loads(payload.decode("utf-8")))
        return json.dumps(
            {"audio_base64": base64.b64encode(b"RIFFfake").decode()}
        ).encode("utf-8")

    monkeypatch.setattr(client, "_post_with_retry", fake_post)
    client.tts("测试一句话", reference_audio_b64="QQ==",
               prosody_variation=False)
    assert captured[0]["prosody_variation"] is False    # per-call 覆盖生效
    captured.clear()
    client.tts("测试一句话", reference_audio_b64="QQ==")
    assert captured[0]["prosody_variation"] is True     # 默认走实例配置
    assert captured[0]["flow_temperature"] == pytest.approx(1.08)
    assert captured[0]["llm_top_k"] == 32
