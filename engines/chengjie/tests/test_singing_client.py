# -*- coding: utf-8 -*-
"""singing_client 纯函数门禁：magic 嗅探/RIFF 解析/产物三道闸/multipart 构造（零网络）。"""
from __future__ import annotations

import struct

import pytest

from src.ai.singing_client import (
    MIN_AUDIO_BYTES, SingingArtifactError, build_cover_multipart, sniff_audio,
    validate_audio_bytes, wav_info,
)


def _wav_bytes(sr: int = 24000, sec: float = 1.0, ch: int = 1,
               bits: int = 16) -> bytes:
    n = int(sr * sec) * ch * (bits // 8)
    fmt = struct.pack("<HHIIHH", 1, ch, sr, sr * ch * (bits // 8),
                      ch * (bits // 8), bits)
    body = (b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", n) + b"\x00" * n)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_sniff_audio():
    assert sniff_audio(_wav_bytes()) == "wav"
    assert sniff_audio(b"OggS" + b"\x00" * 100) == "ogg"
    assert sniff_audio(b"ID3" + b"\x00" * 100) == "mp3"
    assert sniff_audio(b"\xff\xfb" + b"\x00" * 100) == "mp3"
    assert sniff_audio(b"fLaC" + b"\x00" * 100) == "flac"
    assert sniff_audio(b'{"ok": true, "audio_base64": "..."}') == ""
    assert sniff_audio(b"") == ""


def test_wav_info_parses_rate_and_duration():
    sr, dur = wav_info(_wav_bytes(sr=22050, sec=2.0))
    assert sr == 22050
    assert abs(dur - 2.0) < 0.05
    assert wav_info(b"OggS" + b"\x00" * 50) == (0, 0.0)
    assert wav_info(b"RIFFxxxx") == (0, 0.0)   # 坏头不抛


def test_validate_audio_bytes_gates():
    good = _wav_bytes(sec=2.0)
    assert len(good) >= MIN_AUDIO_BYTES
    assert validate_audio_bytes(good, "audio/wav") == "wav"
    # JSON 信封（2026-08-13 事故语义）：content-type 即拒，不看内容
    with pytest.raises(SingingArtifactError):
        validate_audio_bytes(good, "application/json; charset=utf-8")
    # 未知 magic
    with pytest.raises(SingingArtifactError):
        validate_audio_bytes(b"\x00" * MIN_AUDIO_BYTES, "audio/wav")
    # 空壳
    with pytest.raises(SingingArtifactError):
        validate_audio_bytes(_wav_bytes(sec=0.01), "audio/wav")


def test_build_cover_multipart_shape():
    body, ctype = build_cover_multipart(
        {"profile": "林小雨-智聊", "pitch": "auto", "dry_vocal": "true"},
        "song", "tpl.wav", b"RIFFxxxxWAVEdata", boundary="BND")
    assert ctype == "multipart/form-data; boundary=BND"
    text = body.decode("utf-8", "replace")
    assert 'name="profile"' in text and "林小雨-智聊" in text
    assert 'name="dry_vocal"' in text and "true" in text
    assert 'name="song"; filename="tpl.wav"' in text
    assert b"RIFFxxxxWAVEdata" in body
    assert body.endswith(b"--BND--\r\n")
