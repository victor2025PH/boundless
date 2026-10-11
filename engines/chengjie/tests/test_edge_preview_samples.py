"""预置声试听稿：每个目录语种都有稿，且稿子本身能通过语种一致性闸。"""
from __future__ import annotations

import pytest

from src.ai.edge_voice_catalog import (
    PREVIEW_SAMPLES, _V, catalog_payload, pick_edge_voice, preview_sample)
from src.ai.tts_pipeline import TTSPipeline


def test_every_catalog_lang_has_sample():
    langs = {lang for _vid, lang, _g, _n in _V}
    assert langs <= set(PREVIEW_SAMPLES)
    assert catalog_payload()["preview_samples"] == PREVIEW_SAMPLES


def test_preview_sample_normalizes_lang():
    assert preview_sample("ja-JP") == PREVIEW_SAMPLES["ja"]
    assert preview_sample("xx") == ""


# detect_language 目前认不出这些语种（fil→tl、ms→id、uk→ru、nl/pl/sv→en），
# 同语种音色 + 同语种文本也会被语种一致性闸误拦——检测器缺口，非试听稿问题。
_DETECTOR_GAPS = {"fil", "ms", "nl", "pl", "sv", "uk"}


@pytest.mark.parametrize("lang", [
    pytest.param(lg, marks=pytest.mark.xfail(reason="detect_language gap", strict=True))
    if lg in _DETECTOR_GAPS else lg
    for lg in sorted({lang for _vid, lang, _g, _n in _V})])
def test_sample_passes_lang_gate_for_its_voice(lang):
    voice = pick_edge_voice(lang)
    tts = TTSPipeline({"enabled": True, "backend": "edge_tts", "voice": voice})
    lc = tts._lang_consistency(PREVIEW_SAMPLES[lang], voice)
    assert not lc["mismatch"], (lang, lc)


def test_japanese_voice_rejects_chinese_default_text():
    voice = pick_edge_voice("ja")
    tts = TTSPipeline({"enabled": True, "backend": "edge_tts", "voice": voice})
    assert tts._lang_consistency("你好呀，我是陈美玲，很高兴认识你！", voice)["mismatch"]
