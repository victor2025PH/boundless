# -*- coding: utf-8 -*-
"""Q-6 E UI（#266）：al- 前缀 / 三段计数 / 一键采纳 / 场景下拉 / 多选批量。"""
from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "personas.html"


def _src() -> str:
    return _TPL.read_text(encoding="utf-8")


def test_al_prefix_controls_present():
    s = _src()
    for token in (
        'id="al-counts"', 'id="al-adopt-all"', 'class="al-ai"',
        'class="al-kind"', 'class="al-chk"', 'id="al-batch"',
        'id="al-batch-trg"', "_alAdoptAll", "_alBatchAdd", "_alSetKind",
    ):
        assert token in s, token


def test_three_count_keys_and_adopt():
    from src.web.i18n_packs.persona_apply_modal import EN, ZH
    for k in (
        "al_count_confirmed", "al_count_ai", "al_count_none",
        "al_adopt_all", "al_ai_badge", "al_kind_outdoor", "al_batch_add",
    ):
        assert k in ZH and k in EN, k
    assert "{n}" in ZH["al_adopt_all"] and "{n}" in EN["al_adopt_all"]
    assert "待确认" in ZH["pma_f_notrg"]
    assert "Pending confirm" in EN["pma_f_notrg"]
