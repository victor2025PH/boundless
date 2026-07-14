# -*- coding: utf-8 -*-
"""photo_directive：LLM 发图指令协议（解析/剥离/模式解析/协议文本）。

安全重点：**剥离必须比解析宽松**——解析不出的畸形/被出站翻译污染的标记
也必须剥净（泄漏给客户=穿帮）；正文无标记时零改动（零副作用回归）。
"""
import pytest

from src.ai.photo_directive import (
    build_photo_deny_line,
    build_photo_protocol_prompt,
    extract_photo_directive,
    parse_photo_directive,
    resolve_intent_mode,
    strip_photo_directives,
)


# ── parse：标准形态 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,kind,scene_part", [
    ("好呀，刚拍的！\n[PHOTO selfie cozy dorm room, warm lamp light]",
     "selfie", "cozy dorm room"),
    ("看看我做的蛋糕～\n[PHOTO object matcha cake on a plate]",
     "object", "matcha cake"),
    ("[photo Selfie beach at sunset]", "selfie", "beach at sunset"),  # 大小写
    ("[PHOTO: selfie library, evening]", "selfie", "library"),        # 冒号
    ("【PHOTO selfie night street】", "selfie", "night street"),      # 全角括号
    ("[PHOTO selfie]", "selfie", ""),                                 # 无场景也可执行
])
def test_parse_standard(text, kind, scene_part):
    d = parse_photo_directive(text)
    assert d is not None and d["kind"] == kind
    if scene_part:
        assert scene_part in d["scene"]


@pytest.mark.parametrize("text", [
    "",
    "今天天气真好",
    "[PHOTO 自拍 卧室]",          # kind 非英文枚举 → 不可执行（但必须可剥离）
    "[PHOTO video dancing]",      # 不支持的 kind
    "photo selfie no brackets",   # 无括号
])
def test_parse_rejects(text):
    assert parse_photo_directive(text) is None


def test_parse_sanitizes_scene():
    d = parse_photo_directive("[PHOTO selfie <script>{alert}</script> café!!]")
    assert d is not None
    assert "<" not in d["scene"] and "{" not in d["scene"]


def test_parse_scene_length_capped():
    d = parse_photo_directive("[PHOTO selfie " + "a" * 500 + "]")
    assert d is not None and len(d["scene"]) <= 300


# ── strip：宽松剥离（比 parse 覆盖面大）──────────────────────────────────────
def test_strip_standard_directive():
    out = strip_photo_directives("刚拍的！\n[PHOTO selfie dorm room]")
    assert "[PHOTO" not in out and out == "刚拍的！"


def test_strip_malformed_and_translated_variants():
    # kind 非法/被出站翻译污染 → parse 不认，但 strip 必须剥净
    for t in ("好呀 [PHOTO 自拍 卧室] 嘿嘿",
              "here [照片 selfie room] ok",
              "看 【图片: 自拍 咖啡店】 呀",
              "[PHOTO video dancing]"):
        out = strip_photo_directives(t)
        assert "[" not in out or "PHOTO" not in out
        assert "照片" not in out or "[" not in out


def test_strip_multiple_markers():
    out = strip_photo_directives(
        "[PHOTO selfie a]中间文字[PHOTO object b]\n[PHOTO selfie c]")
    assert "PHOTO" not in out and "中间文字" in out


def test_strip_no_marker_zero_touch():
    for t in ("普通回复，没有标记", "带[方括号]但不是指令", "photo 这个词也无妨", ""):
        assert strip_photo_directives(t) == t


def test_strip_collapses_leftover_blank_lines():
    out = strip_photo_directives("第一行\n[PHOTO selfie x]\n第二行")
    assert "\n\n\n" not in out
    assert "第一行" in out and "第二行" in out


# ── extract：组合入口 ─────────────────────────────────────────────────────────
def test_extract_returns_clean_and_directive():
    clean, d = extract_photo_directive("嘿嘿～\n[PHOTO selfie cafe, afternoon]")
    assert d and d["kind"] == "selfie" and "cafe" in d["scene"]
    assert "PHOTO" not in clean and clean == "嘿嘿～"


def test_extract_no_marker():
    clean, d = extract_photo_directive("纯文字")
    assert clean == "纯文字" and d is None


def test_extract_malformed_strips_but_no_directive():
    clean, d = extract_photo_directive("好 [PHOTO 自拍 卧室] 呀")
    assert d is None and "PHOTO" not in clean and "好" in clean


# ── 模式与协议文本 ────────────────────────────────────────────────────────────
def test_resolve_intent_mode_default_and_values():
    assert resolve_intent_mode(None) == "hybrid"
    assert resolve_intent_mode({}) == "hybrid"
    assert resolve_intent_mode({"intent": {"mode": "keyword"}}) == "keyword"
    assert resolve_intent_mode({"intent": {"mode": "LLM"}}) == "llm"
    assert resolve_intent_mode({"intent": {"mode": "whatever"}}) == "hybrid"


def test_protocol_prompt_mentions_marker_and_rules():
    p = build_photo_protocol_prompt()
    assert "[PHOTO selfie" in p and "[PHOTO object" in p
    assert "英文" in p          # 场景要求英文（直通 FLUX）
    assert "不要输出" in p      # 无意图不打标记
    assert "等我去拍" in p      # 文案与图同到的措辞约束


def test_deny_line_forbids_marker():
    assert "[PHOTO" in build_photo_deny_line()
