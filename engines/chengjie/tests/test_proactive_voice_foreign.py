# -*- coding: utf-8 -*-
"""proactive 外语语音决策单测（Phase15）。"""
from src.companion.proactive_voice_foreign import (
    clone_capable_languages,
    foreign_voice_allowed,
    is_chinese_peer_language,
    pick_edge_voice,
    peer_lang_prefix,
    use_clone_for_language,
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


# ── P2（2026-08-02）：clone_languages 名单 → 外语改走克隆链 ────────────────────
def test_clone_languages_default_empty_is_old_behavior():
    """基线不配 clone_languages＝旧行为：任何外语都不走克隆链（全 edge）。"""
    assert clone_capable_languages({}) == frozenset()
    assert use_clone_for_language({}, "en") is False
    assert use_clone_for_language({"enabled": True}, "ja") is False


def test_clone_languages_opt_in_and_prefixing():
    cfg = {"clone_languages": ["en", "JA-jp", " es "]}
    assert clone_capable_languages(cfg) == frozenset({"en", "ja", "es"})
    assert use_clone_for_language(cfg, "en") is True
    assert use_clone_for_language(cfg, "en-US") is True      # BCP47 前缀归一
    assert use_clone_for_language(cfg, "ja") is True
    assert use_clone_for_language(cfg, "ko") is False        # 名单外仍走 edge
    assert use_clone_for_language(cfg, "th") is False


def test_clone_languages_never_hijacks_chinese():
    """zh 恒 False——中文本就走克隆链，这个开关只管外语分流。"""
    cfg = {"clone_languages": ["zh", "en"]}
    assert use_clone_for_language(cfg, "zh") is False
    assert use_clone_for_language(cfg, "zh-cn") is False
    assert use_clone_for_language(cfg, "") is False


def test_clone_languages_tolerates_garbage():
    assert clone_capable_languages({"clone_languages": "en"}) == frozenset({"en"})
    assert clone_capable_languages({"clone_languages": 123}) == frozenset()
    assert clone_capable_languages({"clone_languages": [None, "", "  "]}) == frozenset()
