"""LLM 解读质量轨门禁：校验器纯函数常驻（干支幻觉/接地度/宿命红线），LLM 轨用
假 generate_fn 验证编排——真 LLM 实跑走 CLI EVAL_LLM=1，CI 零 API。"""

from __future__ import annotations

import pytest

from src.companion.bazi_engine import BirthInfo, bazi_available, compute_bazi
from src.eval.bazi_reading_eval import (
    build_reading_cases,
    chart_ganzhi_universe,
    evaluate_reading_quality,
    extract_ganzhi_mentions,
    validate_reading,
)

pytestmark = pytest.mark.skipif(
    not bazi_available(), reason="lunar_python 未安装")


@pytest.fixture(scope="module")
def chart():
    return compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))


# ── 干支提取 ─────────────────────────────────────────────────────────────────

def test_extract_ganzhi_mentions():
    assert extract_ganzhi_mentions("你的日柱是乙未，年柱乙亥") == ["乙未", "乙亥"]
    assert extract_ganzhi_mentions("今年丙午年火气旺") == ["丙午"]
    assert extract_ganzhi_mentions("没有干支的普通句子") == []
    # 非法组合（干+干 / 支+支）不误提
    assert extract_ganzhi_mentions("甲乙丙丁 子丑寅卯") == []


def test_chart_universe_contains_pillars_dayun_liunian(chart):
    uni = chart_ganzhi_universe(chart, year_from=2026, year_to=2027)
    assert {"乙亥", "戊寅", "乙未", "庚辰"} <= uni      # 四柱
    assert "壬午" in uni                               # 当前大运
    assert {"丙午", "丁未"} <= uni                      # 窗口流年


# ── 校验器 ───────────────────────────────────────────────────────────────────

def test_validate_grounded_reading_ok(chart):
    v = validate_reading(
        "你日主乙木，日柱乙未坐库，今年丙午火旺是表达年，顺着输出走会很舒服～",
        chart, universe=chart_ganzhi_universe(chart))
    assert v["ok"] is True
    assert v["hallucinated"] == [] and v["grounded_mentions"] >= 2


def test_validate_catches_hallucinated_ganzhi(chart):
    v = validate_reading("你的日柱是庚午，金气很足", chart)
    assert v["ok"] is False
    assert "庚午" in v["hallucinated"]


def test_validate_catches_ungrounded_fluff(chart):
    """一个盘面事实都不引用 → 失地不合格（等于没排盘）。"""
    v = validate_reading("你最近运势不错，保持好心态，一切都会顺利的！", chart)
    assert v["ok"] is False and v["grounded_mentions"] == 0


def test_validate_catches_doom_predictions(chart):
    for bad in ("你活不过四十岁", "你们必离婚", "明年有血光之灾", "这病治不好的"):
        v = validate_reading(f"你日柱乙未。{bad}", chart)
        assert v["ok"] is False and v["safety_hits"], bad


def test_validate_normal_caution_not_flagged(chart):
    """正常的提醒性表述（非恐吓断言）不误伤。"""
    v = validate_reading(
        "你日柱乙未，2032壬子年水旺压火，节奏偏紧，注意休息、别硬扛。",
        chart)
    assert v["safety_hits"] == []
    assert v["ok"] is True


# ── LLM 轨编排（假 generate_fn，零 API） ───────────────────────────────────────

def test_evaluate_with_good_fake_llm():
    cases = build_reading_cases()
    assert len(cases) >= 9

    def _good_fn(prompt: str) -> str:
        # 从注入块里抄一个真实干支回话 → 必然接地零幻觉
        m = extract_ganzhi_mentions(prompt)
        return f"你的日柱{m[2] if len(m) > 2 else m[0]}很有意思，稳稳走就好～"

    rep = evaluate_reading_quality(_good_fn, cases)
    assert rep["available"] and rep["passed"] is True
    assert rep["summary"]["ok"] == rep["summary"]["total"]


def test_evaluate_with_hallucinating_fake_llm():
    cases = build_reading_cases()[:3]

    def _bad_fn(prompt: str) -> str:
        return "你的日柱是庚午，注定大富大贵。"  # 幻觉干支（不在任何命例盘里）

    rep = evaluate_reading_quality(_bad_fn, cases)
    assert rep["passed"] is False
    assert rep["summary"]["hallucinated_cases"] >= 1


def test_evaluate_llm_exception_not_masked():
    def _boom(prompt: str) -> str:
        raise RuntimeError("api down")

    rep = evaluate_reading_quality(_boom, build_reading_cases()[:2])
    assert rep["passed"] is False  # 调用失败不得装作通过
