# -*- coding: utf-8 -*-
"""proactive 外语语音决策单测（Phase15）。"""
from src.companion.proactive_voice_foreign import (
    foreign_voice_allowed,
    is_chinese_peer_language,
    pick_edge_voice,
    peer_lang_prefix,
)


def test_chinese_detection():
    assert is_chinese_peer_language("zh") is True
    assert is_chinese_peer_language("zh-cn") is True
    assert is_chinese_peer_language("") is True
    assert is_chinese_peer_language("en") is False


def test_foreign_allowed_whitelist():
    cfg = {"enabled": True, "languages": ["en", "ja"]}
    assert foreign_voice_allowed(cfg, "en") is True
    assert foreign_voice_allowed(cfg, "en-US") is True
    assert foreign_voice_allowed(cfg, "fr") is False
    assert foreign_voice_allowed(cfg, "zh") is False


def test_foreign_disabled():
    assert foreign_voice_allowed({"enabled": False}, "en") is False


def test_pick_edge_voice():
    assert "Jenny" in pick_edge_voice({}, "en")
    assert pick_edge_voice({"edge_voices": {"en": "en-US-GuyNeural"}}, "en") == "en-US-GuyNeural"
    assert peer_lang_prefix("ja-JP") == "ja"
