# -*- coding: utf-8 -*-
"""B130（2026-08-28）：自动回复句数在档位内真随机。

实录：档位「适中 2-4 句」恒定输出 3 句（skuio 09:44），机械感强。根因＝prompt 给
的是区间，LLM 每次都取中间值。本文件钉住「摇出来的数真的会变、且不越档」。
"""
from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

from src.ai.reply_length_variety import (
    SENTENCE_RANGES,
    SHORT_BURST_PROB,
    pick_reply_sentence_target,
    sentence_target_hint,
)

REPO = Path(__file__).resolve().parents[1]


def _sample(tier: str, n: int = 3000, seed: int = 20260828) -> Counter:
    rng = random.Random(seed)
    return Counter(pick_reply_sentence_target(tier, rng=rng) for _ in range(n))


# ── 事故本体：不再恒定 ──────────────────────────────────────────────────────

def test_moderate_is_not_constant():
    """事故回归钉：适中档必须真的在 2/3/4 之间摇，而不是恒 3。"""
    got = _sample("moderate")
    assert {2, 3, 4} <= set(got), f"档位内取值不全：{sorted(got)}"
    top = got.most_common(1)[0][1]
    assert top < sum(got.values()) * 0.6, "某个句数占比过半＝退回恒定输出"


def test_every_tier_spans_its_range():
    for tier, (lo, hi) in SENTENCE_RANGES.items():
        got = _sample(tier, n=2000)
        assert set(range(lo, hi + 1)) <= set(got), f"{tier} 未覆盖 {lo}-{hi}：{sorted(got)}"


# ── 越界护栏：拟人不能变成不听话 ────────────────────────────────────────────

def test_never_exceeds_tier_ceiling():
    """上限是运营的明确意愿，任何情况不得突破。"""
    for tier, (_lo, hi) in SENTENCE_RANGES.items():
        assert max(_sample(tier, n=2000)) <= hi, tier


def test_short_burst_drops_at_most_one_below_floor():
    """偶发短打只低一句、且不低于 1——不做整档坍塌。"""
    for tier, (lo, _hi) in SENTENCE_RANGES.items():
        floor = max(1, lo - 1)
        assert min(_sample(tier, n=2000)) >= floor, tier


def test_detailed_never_collapses_to_single_sentence():
    """运营选了「稍详细」却经常只收一句话＝违背设置，不是拟人。"""
    assert 1 not in _sample("detailed", n=3000)


def test_short_burst_is_occasional_not_dominant():
    got = _sample("moderate", n=4000)
    ratio = got[1] / sum(got.values())
    assert 0 < ratio < SHORT_BURST_PROB * 2, f"短打占比异常：{ratio:.3f}"


# ── 未知档位：不注入 ────────────────────────────────────────────────────────

def test_unknown_tier_is_noop():
    """运营没选档位（空串）→ 不摇不注入，字节级旧行为。"""
    for tier in ("", "   ", "unknown", None):
        assert pick_reply_sentence_target(tier) == 0
        assert sentence_target_hint(tier) == ""


def test_alias_tiers_share_range():
    """历史别名与主名同区间（concise=short / balanced=moderate / long=detailed）。"""
    assert SENTENCE_RANGES["concise"] == SENTENCE_RANGES["short"]
    assert SENTENCE_RANGES["balanced"] == SENTENCE_RANGES["moderate"]
    assert SENTENCE_RANGES["long"] == SENTENCE_RANGES["detailed"]


# ── 文案 ────────────────────────────────────────────────────────────────────

def test_hint_wording_by_count():
    class _Fixed(random.Random):
        def __init__(self, n):
            super().__init__(0)
            self._n = n

        def random(self):
            return 1.0          # 不触发短打

        def randint(self, a, b):
            return self._n

    assert "只回一句" in sentence_target_hint("short", rng=_Fixed(1))
    assert "写 3 句" in sentence_target_hint("moderate", rng=_Fixed(3))


# ── 接线锚点 ────────────────────────────────────────────────────────────────

def test_persona_prompt_wires_variety():
    """写了随机器没接进 prompt＝白修（本次事故的形状：切分链无辜，prompt 才是源头）。"""
    src = (REPO / "src" / "utils" / "persona_manager.py").read_text(encoding="utf-8")
    assert "sentence_target_hint" in src, "人设 prompt 未接句数随机"
    assert 'length_variety' in src, "缺少退回旧行为的开关"
    # 档位区间文案必须留着——设置页与人设文档都按它解释「适中 2-4 句」
    assert "回复均衡：2-4 句话" in src
