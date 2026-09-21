"""P0-2b（#339 / #340）：相册触发词整词匹配 + 人设开场白账本。"""
from __future__ import annotations

from src.ai.reply_variety import (
    build_variety_hint,
    collect_overused,
    extract_persona_openers,
    message_opener,
)
from src.companion.persona_media import _partition, trigger_term_hit
from src.inbox.image_send_gate import TRIGGER_KEYWORD, compute_image_intent, keyword_hit


# ── #339：触发词子串误触 ──────────────────────────────────────────────────────

def test_latin_trigger_requires_word_boundary():
    assert trigger_term_hit("car", "i love my car")
    assert trigger_term_hit("car", "two cars in the lot")
    assert not trigger_term_hit("car", "i don't care")
    assert not trigger_term_hit("car", "a scarf")
    assert not trigger_term_hit("cat", "which category")
    assert trigger_term_hit("my dog", "look at my dog!")
    assert not trigger_term_hit("dog", "hotdog stand")


def test_cjk_trigger_keeps_substring_semantics():
    assert trigger_term_hit("海边", "周末去海边玩")
    assert trigger_term_hit("跳舞", "你还跳舞吗")


def test_keyword_hit_gate_and_partition_share_the_rule():
    assert not keyword_hit("take care of yourself", ["car"])
    assert keyword_hit("send me your car", ["car"])
    g = compute_image_intent("I don't care about it", trigger_terms=["car"])
    assert not g.intent
    g2 = compute_image_intent("show me the car", trigger_terms=["car"])
    assert g2.intent and g2.trigger in (TRIGGER_KEYWORD, "ask")
    rows = [{"id": 1, "enabled": True, "media_type": "photo", "triggers": ["car"]}]
    kw, _sug, _gen = _partition(rows, "i don't care")
    assert kw == []
    kw2, _, _ = _partition(rows, "your car looks nice")
    assert [r["id"] for r in kw2] == [1]


# ── #340：英文 opener 有账本 ─────────────────────────────────────────────────

def test_extract_persona_openers():
    p = {"speaking": {"openers": ["Hey you!", "嘿嘿", "Hey you!", "", None]}}
    assert extract_persona_openers(p) == ["Hey you!", "嘿嘿"]
    assert extract_persona_openers({}) == []


def test_message_opener_prefix_case_and_punct_insensitive():
    ops = ["Hey you!", "Okay so", "说真的"]
    assert message_opener("hey you 😘 how was your day", ops) == "Hey you!"
    assert message_opener("Okay, so… I was thinking", ops) == "Okay so"
    assert message_opener("说真的，我今天好累", ops) == "说真的"
    assert message_opener("Nothing much today", ops) == ""


def test_english_openers_are_ledgered_and_blocked():
    outs = [
        "Hey you! Just got home.",
        "hey you 😘 missed you",
        "Hey you, what are you up to?",
        "Okay so today was long",
    ]
    ov = collect_overused(outs, opener_words=["Hey you!", "Okay so"], head_limit=99)
    assert ov["openers"][0] == {"word": "Hey you!", "count": 3}
    hint = build_variety_hint(ov, lang="en")
    assert '"Hey you!"' in hint and "do not open with" in hint
    hint_zh = build_variety_hint(ov, lang="zh")
    assert "「Hey you!」" in hint_zh and "开场" in hint_zh


def test_english_repeated_first_word_now_visible_in_heads():
    outs = ["Hey there, love", "Hey babe, how are you", "Honestly no idea"]
    ov = collect_overused(outs, head_limit=2)
    assert ov["heads"][0] == {"head": "hey", "count": 2}


def test_opener_ledger_dedups_heads_for_same_fact():
    outs = ["Hey you! a", "Hey you! b", "Hey you! c"]
    ov = collect_overused(outs, opener_words=["Hey you!"], head_limit=2)
    assert "openers" in ov and "heads" not in ov
