"""PTT 格式硬闸：魔数 / ensure / B 线禁 passthrough（2026-08-04 .198 事故回归）。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from src.client.voice_ptt_gate import (
    ensure_ptt_ogg,
    is_ptt_ready,
    looks_like_ogg_opus,
    looks_like_ogg_opus_file,
    platform_requires_ptt,
    ptt_ready_reason,
)


def _minimal_ogg_opus() -> bytes:
    return b"OggS" + b"\x00" * 60 + b"OpusHead" + b"\x00" * 32


def test_looks_like_ogg_opus_accepts_magic():
    assert looks_like_ogg_opus(_minimal_ogg_opus()) is True


def test_looks_like_ogg_opus_rejects_wav_mp3_fake_ogg():
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 80
    assert looks_like_ogg_opus(wav) is False
    assert looks_like_ogg_opus(b"\xff\xfb" + b"\x00" * 80) is False
    assert looks_like_ogg_opus(b"OggS" + b"A" * 80) is False  # 无 OpusHead
    assert looks_like_ogg_opus(b"") is False


def test_ptt_ready_reason_on_disk(tmp_path):
    good = tmp_path / "ok.ogg"
    good.write_bytes(_minimal_ogg_opus())
    assert ptt_ready_reason(str(good)) == ""
    assert is_ptt_ready(str(good))

    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF" + b"\x00" * 100)
    assert ptt_ready_reason(str(bad)) == "ptt_not_ogg_opus"

    fake = tmp_path / "fake.ogg"
    fake.write_bytes(b"OggS" + b"x" * 80)
    assert ptt_ready_reason(str(fake)) == "ptt_ogg_magic_mismatch"


def test_platform_requires_ptt_covers_whatsapp():
    assert platform_requires_ptt("whatsapp")
    assert platform_requires_ptt("WhatsApp")
    assert platform_requires_ptt("telegram")


def test_ensure_ptt_ogg_passthrough_ready(tmp_path):
    p = tmp_path / "a.ogg"
    p.write_bytes(_minimal_ogg_opus())
    out, why = ensure_ptt_ogg(str(p))
    assert why == ""
    assert out == str(p)


def test_ensure_ptt_ogg_never_passthrough_wav(tmp_path, monkeypatch):
    """事故回归：convert 失败时绝不能回落原 WAV。"""
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 100)

    monkeypatch.setattr(
        "src.client.voice_sender.convert_to_ogg_opus",
        lambda *a, **k: None,
    )
    out, why = ensure_ptt_ogg(str(wav), delete_src=False)
    assert out is None
    assert why  # ptt_not_ogg_opus 或 ptt_convert_failed
    assert wav.exists()  # delete_src=False 保留源


def test_ensure_ptt_ogg_rejects_convert_that_returns_non_opus(tmp_path, monkeypatch):
    src = tmp_path / "x.wav"
    src.write_bytes(b"RIFF" + b"\x00" * 100)
    # 模拟「转码」仍吐出非 opus（旧 passthrough 语义）
    monkeypatch.setattr(
        "src.client.voice_sender.convert_to_ogg_opus",
        lambda p, delete_src=False, application="voip": p,
    )
    out, why = ensure_ptt_ogg(str(src))
    assert out is None
    assert "ptt_" in why


def test_convert_to_ogg_opus_does_not_trust_ogg_suffix(tmp_path, monkeypatch):
    """假 .ogg 不得被 convert 原样放行。"""
    from src.client import voice_sender as vs

    fake = tmp_path / "lie.ogg"
    fake.write_bytes(b"OggS" + b"Z" * 80)  # 无 OpusHead

    called = {"n": 0}

    def _fake_run(*a, **k):
        called["n"] += 1
        # 写出真魔数到 dst（argv 末位）
        dst = Path(a[0][-1])
        dst.write_bytes(_minimal_ogg_opus())

        class _R:
            returncode = 0
            stderr = ""
        return _R()

    monkeypatch.setattr(vs, "_ffmpeg_available", lambda: True)
    monkeypatch.setattr(vs.subprocess, "run", _fake_run)
    out = vs.convert_to_ogg_opus(str(fake))
    assert called["n"] == 1
    assert out is not None
    assert looks_like_ogg_opus_file(out)


@pytest.mark.asyncio
async def test_synth_ogg_wav_falls_back_to_none(monkeypatch, tmp_path):
    """B 线：TTS 吐 WAV + convert 失败 → None（禁按原格式）。"""
    import src.inbox.voice_autosend as va
    import src.ai.persona_voice as pv

    wav = tmp_path / "t.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 100)

    class _R:
        ok = True
        audio_path = str(wav)
        provider = "avatar_clone"
        voice = ""
        latency_ms = 1
        duration_sec = 1.0
        error = ""
        extra = {}

    class _TTS:
        def __init__(self, cfg):
            pass

        async def synthesize(self, text, timeout_sec=45.0, emotion=None, **kw):
            return _R()

    monkeypatch.setattr(
        pv, "resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "fake"}, "emotion": None})
    monkeypatch.setattr(va, "preflight_voice_synth", lambda *a, **k: None)
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _TTS)
    monkeypatch.setattr(
        "src.client.voice_sender.convert_to_ogg_opus",
        lambda *a, **k: None)

    path, meta = await va._synth_ogg(
        {}, "p1", "hello there", out_dir=str(tmp_path), platform="whatsapp")
    assert path is None
    assert str(meta.get("ptt_reject") or "").startswith("ptt_")
    assert va.pop_synth_failure_reason().startswith("ptt_")


@pytest.mark.asyncio
async def test_stage_voice_file_preserves_ptt_failure_reason(monkeypatch, tmp_path):
    import src.inbox.voice_autosend as va

    async def _fail(*a, **k):
        va._set_synth_failure("ptt_convert_failed")
        return None, {"ptt_reject": "ptt_convert_failed"}

    monkeypatch.setattr(va, "_synth_ogg", _fail)
    out = await va.stage_voice_file({}, "whatsapp", "acct", "p1", "hi")
    assert out is None
    assert va.pop_synth_failure_reason() == "ptt_convert_failed"
