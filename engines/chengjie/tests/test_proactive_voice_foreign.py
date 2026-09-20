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


# ── P0（2026-08-31）：capable_langs 按克隆主路实况收窄意愿名单 ────────────────
# clone_languages 是 fish 时代实证的「意愿」（en/ja/es）；主链切 IndexTTS-2
# （仅中英）后名单不会自动跟上——收窄后名单内但念不了的语种回 edge 多语声，
# 保住语音触达而不是「克隆试败 → 纯文本」。

def test_capable_langs_narrows_stale_wishlist():
    cfg = {"clone_languages": ["en", "ja", "es"]}
    capable = ("zh", "en")            # SSOT：hub index_tts 仅中英
    assert use_clone_for_language(cfg, "en", capable_langs=capable) is True
    assert use_clone_for_language(cfg, "ja", capable_langs=capable) is False
    assert use_clone_for_language(cfg, "es-ES", capable_langs=capable) is False


def test_capable_langs_unknown_keeps_wishlist_semantics():
    """能力未知（None/空元组）→ 维持纯名单语义（宁可漏收窄不误伤）。"""
    cfg = {"clone_languages": ["en", "ja"]}
    assert use_clone_for_language(cfg, "ja") is True
    assert use_clone_for_language(cfg, "ja", capable_langs=None) is True
    assert use_clone_for_language(cfg, "ja", capable_langs=()) is True


def test_capable_langs_never_widens_wishlist():
    """capable 再宽也不越权：名单外语种恒 False，中文恒 False。"""
    cfg = {"clone_languages": ["en"]}
    assert use_clone_for_language(
        cfg, "th", capable_langs=("zh", "en", "th")) is False
    assert use_clone_for_language(
        cfg, "zh", capable_langs=("zh",)) is False
    # BCP47 变体归一：capable 给 zh-TW 形态也认前缀
    cfg2 = {"clone_languages": ["ja"]}
    assert use_clone_for_language(
        cfg2, "ja-JP", capable_langs=("ZH", "JA-jp")) is True
