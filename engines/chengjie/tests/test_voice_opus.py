"""Opus 编码档门禁 — src/client/voice_sender.py Phase D。"""
from __future__ import annotations

from src.client.voice_sender import resolve_opus_application


def test_resolve_opus_application_defaults_voip():
    assert resolve_opus_application({}) == "voip"
    assert resolve_opus_application({"telegram": {"voice_reply": {}}}) == "voip"


def test_resolve_opus_application_audio():
    cfg = {"telegram": {"voice_reply": {"opus": {"application": "audio"}}}}
    assert resolve_opus_application(cfg) == "audio"


def test_resolve_opus_application_invalid_falls_back():
    cfg = {"telegram": {"voice_reply": {"opus": {"application": "music"}}}}
    assert resolve_opus_application(cfg) == "voip"
