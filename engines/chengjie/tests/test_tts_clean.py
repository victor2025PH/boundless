"""TTS 合成前文本清洗门禁：剔 emoji + 换行折停顿（防克隆 TTS 念一半截断）。"""
from src.ai.tts_pipeline import clean_text_for_tts


def test_strips_emoji():
    out = clean_text_for_tts("刚做完饭😋好辣🍜")
    assert "😋" not in out and "🍜" not in out
    assert "刚做完饭" in out and "好辣" in out


def test_newline_becomes_pause_not_cut():
    # 多行英文（真机 cut 案例）：换行折成停顿，整段保留（不被截断）
    out = clean_text_for_tts("cooking noodles 🍜\n\nWhat about you?")
    assert "cooking noodles" in out and "What about you?" in out
    assert "\n" not in out


def test_cjk_multiline_joined():
    out = clean_text_for_tts("哈哈当然可以呀，等我一下～\n\n不过卖相可能一般般😂")
    assert "等我一下" in out and "不过卖相可能一般般" in out
    assert "\n" not in out and "😂" not in out
    # 换行折空格（不用逗号——防 CosyVoice 在人工逗号处早停）
    assert "，不过" not in out
    assert "等我一下～ 不过" in out or "等我一下～不过" in out.replace("  ", " ")


def test_multiline_prod_case_no_comma_break():
    """2026-07-14 真机：多行回复第二句被截在首句。"""
    raw = "哈哈，这不已经发给你了嘛～  \n你看看，是不是还挺有感觉的 😂"
    out = clean_text_for_tts(raw)
    assert "你看看" in out and "是不是还挺有感觉的" in out
    assert "\n" not in out
    # 换行处不应插入中文逗号
    assert "嘛～，你看看" not in out


def test_flatten_tts_clauses():
    from src.ai.tts_pipeline import flatten_tts_clauses
    raw = "哈哈，这不已经发给你了嘛～  \n你看看，是不是还挺有感觉的"
    flat = flatten_tts_clauses(raw)
    assert "，" not in flat and "你看看" in flat


def test_suspect_tts_truncation_multiline_short_audio():
    from src.ai.tts_pipeline import suspect_tts_truncation
    raw = "哈哈，这不已经发给你了嘛～  \n你看看，是不是还挺有感觉的 😂"
    assert suspect_tts_truncation(raw, 2846, provider="avatar_clone") is True
    assert suspect_tts_truncation(raw, 5200, provider="avatar_clone") is False
    assert suspect_tts_truncation("单行短句", 2000, provider="avatar_clone") is False


def test_empty_and_dirty_safe():
    assert clean_text_for_tts("") == ""
    assert clean_text_for_tts(None) == ""
    assert clean_text_for_tts("😂😂😂") == ""  # 全 emoji → 空（调用方回落原文）


def test_no_trailing_comma():
    out = clean_text_for_tts("你好呀\n")
    assert not out.endswith("，") and out.startswith("你好")
