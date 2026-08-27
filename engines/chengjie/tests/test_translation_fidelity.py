# -*- coding: utf-8 -*-
"""B56 翻译专名/数字保真对账（实施64 P1-3，2026-08-23，`_297` 实录）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.ai.translation_fidelity import (
    FIDELITY_PROMPT_RULE,
    annotate_missing_anchors,
    extract_anchors,
    fidelity_issues,
    swapped_geo,
)

REPO = Path(__file__).resolve().parents[1]
ACCIDENT_SRC = "Take care of yourself, and see you in Cebu when the time comes."
ACCIDENT_BAD = "照顾好自己，到时候我很想在马尼拉见到你。"
ACCIDENT_GOOD = "照顾好自己，到时候宿务见。"


def test_extract_anchors_finds_mid_sentence_proper_noun_and_numbers():
    assert "Cebu" in extract_anchors(ACCIDENT_SRC)
    assert "38" in extract_anchors("I told you I'm 38 already")
    # 句首大写普通词/客套词绝不当锚点
    assert extract_anchors("Hello there. Thanks for today. See you!") == []
    assert extract_anchors("Good morning! How are you") == []


def test_accident_case_cebu_swapped_to_manila_is_caught():
    issues = fidelity_issues(ACCIDENT_SRC, ACCIDENT_BAD)
    assert issues == ["Cebu"]
    assert swapped_geo(ACCIDENT_SRC, ACCIDENT_BAD) == "manila"
    out, missing = annotate_missing_anchors(ACCIDENT_SRC, ACCIDENT_BAD, "zh")
    assert missing == ["Cebu"]
    assert out.endswith("（原词：Cebu）")


def test_correct_transliteration_passes_clean():
    assert fidelity_issues(ACCIDENT_SRC, ACCIDENT_GOOD) == []
    assert swapped_geo(ACCIDENT_SRC, ACCIDENT_GOOD) == ""
    out, missing = annotate_missing_anchors(ACCIDENT_SRC, ACCIDENT_GOOD, "zh")
    assert out == ACCIDENT_GOOD and missing == []


def test_verbatim_latin_in_translation_passes():
    out, missing = annotate_missing_anchors(
        ACCIDENT_SRC, "照顾好自己，到时候 Cebu 见！", "zh")
    assert missing == []


def test_number_preserved_and_missing():
    src = "转账 1500 明天到"
    assert fidelity_issues(src, "The 1500 transfer arrives tomorrow") == []
    bad = fidelity_issues(src, "The transfer arrives tomorrow")
    assert bad == ["1500"]
    out, _ = annotate_missing_anchors(src, "The transfer arrives tomorrow", "en")
    assert out.endswith("(original: 1500)")


def test_cjk_source_latin_token_anchor():
    src = "我们在 Cebu 的办公室等你"
    assert fidelity_issues(src, "We'll wait for you at the Manila office") == ["Cebu"]
    assert fidelity_issues(src, "We'll wait for you at the Cebu office") == []


def test_annotation_never_fires_on_empty_or_failed_output():
    out, missing = annotate_missing_anchors(ACCIDENT_SRC, "", "zh")
    assert out == "" and missing  # 缺失如实报，但不往空串上贴括注


def test_prompt_rule_wired_into_ai_engine_and_system():
    src = (REPO / "src" / "ai" / "translation_engines.py").read_text(encoding="utf-8")
    assert "FIDELITY_PROMPT_RULE" in src
    assert "preserved exactly" in src  # system 铁律
    assert "annotate_missing_anchors" in src  # 路由译后对账接线
    assert FIDELITY_PROMPT_RULE  # 常量非空


def test_router_annotates_end_to_end():
    """路由端到端：引擎返回偷换译文 → translate() 出口自动括注原词。"""
    from src.ai.translation_engines import EngineResult, EngineRouter

    class _SwapEngine:
        name = "fake"
        label = "Fake"
        available = True

        def supports_target(self, t):
            return True

        async def translate(self, text, *, source_lang, target_lang,
                            style="chat", glossary_hint=""):
            return EngineResult(ACCIDENT_BAD, "fake", True)

    router = EngineRouter([_SwapEngine()])
    res = asyncio.run(router.translate(
        ACCIDENT_SRC, source_lang="en", target_lang="zh"))
    assert res.ok
    assert res.text.endswith("（原词：Cebu）")
