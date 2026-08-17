# -*- coding: utf-8 -*-
"""翻译 A/B 裁判台门禁（纯函数，无网络无 GPU，常驻 CI）。

重点覆盖那些**天真实现会静默做错**的地方——错了不会报错，只会给出一个看起来很正常
的错误结论，而这个结论会被拿去做换模型的决策：

- 按下标对齐 → 一侧少一个样本就整体错位，delta 全是垃圾但报告完全正常；
- 只比均分不做区间估计 → 把噪声当成质量提升；
- 把「压根没翻出来」的 0 分混进质量分 → 端点抖动被误读成模型变差；
- 对同一 engine 名下的两个 model 给 per_lang_order 建议 → 那条配置根本区分不了它们。
"""

from __future__ import annotations

import pytest

from src.eval.translation_ab import (
    bootstrap_ci,
    compare,
    format_ab_report,
    is_hard_failure,
    join_results,
    judge_is_contaminated,
    sign_counts,
    suggest_per_lang_order,
)


def _row(text, tgt, score, *, src="zh", semantic=None, reason=None, ok=None):
    r = {"text": text, "source": src, "target": tgt, "score": score,
         "ok": bool(score >= 0.5) if ok is None else ok}
    if semantic is not None:
        r["semantic"] = semantic
    if reason is not None:
        r["reason"] = reason
    return r


def _report(rows):
    return {"results": list(rows), "summary": {}, "passed": True}


# ---------------------------------------------------------------- 对齐

def test_join_aligns_by_content_not_position():
    """一侧漏一个样本时，按下标对齐会把后续全部错位——错位后的 delta 是纯噪声，
    但报告长度、均分、CI 一切正常，是这类工具最危险的静默失败。"""
    a = [_row("一", "en", 0.9), _row("二", "ja", 0.8), _row("三", "ko", 0.7)]
    b = [_row("一", "en", 0.9), _row("三", "ko", 0.7)]  # B 少了「二」
    paired = join_results(a, b)
    assert [p["key"][0] for p in paired] == ["一", "三"]
    # 若按下标对齐，「二」会被拿去和 B 的「三」比 → 出现不该存在的配对
    assert all(p["a"]["text"] == p["b"]["text"] for p in paired)


def test_join_reports_unpaired_instead_of_silently_padding():
    a = [_row("一", "en", 0.9), _row("二", "ja", 0.8)]
    b = [_row("一", "en", 0.6)]
    c = compare(_report(a), _report(b))
    assert c["paired_n"] == 1
    assert c["unpaired_a"] == 1


def test_same_text_different_target_is_not_confused():
    """同一原文翻成不同目标语是两个独立样本，不能因为文本相同就并成一个。"""
    a = [_row("你好", "en", 0.9), _row("你好", "ja", 0.4)]
    b = [_row("你好", "ja", 0.5), _row("你好", "en", 0.8)]
    paired = join_results(a, b)
    got = {(p["key"][2], p["a"]["score"], p["b"]["score"]) for p in paired}
    assert got == {("en", 0.9, 0.8), ("ja", 0.4, 0.5)}


# ---------------------------------------------------------------- 统计

def test_bootstrap_is_deterministic_so_gates_and_trends_are_comparable():
    deltas = [0.1, -0.05, 0.2, 0.0, 0.15, -0.1, 0.05, 0.3]
    assert bootstrap_ci(deltas) == bootstrap_ci(deltas)


def test_ci_crosses_zero_for_noise_so_verdict_is_do_not_switch():
    """对称抖动的两个引擎必须判「无差异」——这正是拦住「拿噪声换模型」的那道闸。"""
    a = [_row(f"s{i}", "en", 0.70) for i in range(20)]
    b = [_row(f"s{i}", "en", 0.70 + (0.12 if i % 2 else -0.12)) for i in range(20)]
    c = compare(_report(a), _report(b))
    lo, hi = c["ci95"]
    assert lo < 0 < hi
    assert c["verdict"] == "no_difference"


def test_clear_and_large_gap_is_called_a_win():
    a = [_row(f"s{i}", "en", 0.60) for i in range(20)]
    b = [_row(f"s{i}", "en", 0.80) for i in range(20)]
    c = compare(_report(a), _report(b))
    assert c["verdict"] == "b_better"
    assert c["ci95"][0] > 0
    assert c["b_wins"] == 20 and c["a_wins"] == 0


def test_significant_but_tiny_gap_is_marginal_not_a_migration_order():
    """统计显著 ≠ 值得迁移。恒定 +0.01 的差 CI 不跨零，但换模型的代价远大于 0.01。"""
    a = [_row(f"s{i}", "en", 0.60) for i in range(20)]
    b = [_row(f"s{i}", "en", 0.61) for i in range(20)]
    c = compare(_report(a), _report(b), min_effect=0.02)
    assert c["ci95"][0] > 0
    assert c["verdict"] == "b_better_marginal"


def test_a_side_win_is_symmetric():
    a = [_row(f"s{i}", "en", 0.85) for i in range(15)]
    b = [_row(f"s{i}", "en", 0.60) for i in range(15)]
    c = compare(_report(a), _report(b))
    assert c["verdict"] == "a_better"


def test_tie_epsilon_absorbs_rounding_noise():
    assert sign_counts([0.001, -0.002, 0.0], eps=0.005) == {
        "a_wins": 0, "b_wins": 0, "ties": 3}
    assert sign_counts([0.02, -0.03], eps=0.005) == {
        "a_wins": 1, "b_wins": 1, "ties": 0}


def test_samples_missing_the_metric_are_surfaced_not_silently_dropped():
    """配对成功但语义轨无分（嵌入失败）的样本会被排除在判据外。3/50 无所谓，
    30/50 就是「结论其实只用了 20 个样本」——两种情况报告不能长得一模一样。"""
    a = ([_row(f"s{i}", "en", 0.6, semantic=0.90) for i in range(8)]
         + [_row(f"s{i}", "en", 0.6) for i in range(8, 12)])
    b = ([_row(f"s{i}", "en", 0.6, semantic=0.92) for i in range(8)]
         + [_row(f"s{i}", "en", 0.6) for i in range(8, 12)])
    c = compare(_report(a), _report(b))
    assert c["metric"] == "semantic"
    assert c["paired_total"] == 12 and c["paired_n"] == 8
    assert c["unscored_on_metric"] == 4
    assert "未参与判据" in format_ab_report(c)


def test_fully_scored_run_does_not_nag():
    a = [_row(f"s{i}", "en", 0.6, semantic=0.90) for i in range(10)]
    b = [_row(f"s{i}", "en", 0.6, semantic=0.92) for i in range(10)]
    c = compare(_report(a), _report(b))
    assert c["unscored_on_metric"] == 0
    assert "未参与判据" not in format_ab_report(c)


def test_too_few_pairs_refuses_to_conclude():
    c = compare(_report([_row("一", "en", 0.9)]), _report([_row("一", "en", 0.2)]))
    assert c["verdict"] == "insufficient_data"


# ---------------------------------------------------------------- 硬失败

def test_hard_failures_are_reported_separately_from_quality():
    """翻不出来是可用性问题（查端点），翻得差是质量问题（选型）——两者处置完全不同，
    混在一个均分里会把一次端点抖动误读成「模型变差了」。"""
    a = [_row(f"s{i}", "en", 0.8) for i in range(10)]
    b = ([_row(f"s{i}", "en", 0.8) for i in range(7)]
         + [_row(f"s{i}", "en", 0.0, reason="forward_failed") for i in range(7, 10)])
    c = compare(_report(a), _report(b))
    assert c["b_hard_failures"] == 3
    assert c["a_hard_failures"] == 0
    assert "硬失败" in format_ab_report(c)


def test_back_translation_failure_is_not_blamed_on_a_candidate():
    """回译失败是**裁判**没出活，对两个候选是共同噪声，不该记到某一方头上。"""
    assert is_hard_failure({"reason": "forward_failed"}) is True
    assert is_hard_failure({"reason": "back_failed"}) is False


# ---------------------------------------------------------------- 裁判污染

def test_self_judging_candidate_is_flagged():
    assert judge_is_contaminated("ollama_mt:hy-mt2", "ollama_mt:hy-mt2", "ai(LLM)") == "a"
    assert judge_is_contaminated("ai(LLM)", "ollama_mt:hy-mt2", "ai(LLM)") == "b"
    assert judge_is_contaminated("same", "x", "y") == "both"
    assert judge_is_contaminated("deepl/google", "ollama_mt:a", "ollama_mt:b") is None


def test_contamination_warning_reaches_the_reader():
    a = [_row(f"s{i}", "en", 0.6) for i in range(10)]
    b = [_row(f"s{i}", "en", 0.9) for i in range(10)]
    c = compare(_report(a), _report(b), a_label="X", b_label="Y", judge_label="Y")
    assert c["judge_contaminated"] == "b"
    assert "裁判污染" in format_ab_report(c)


# ---------------------------------------------------------------- 轨道选择

def test_semantic_track_wins_when_both_sides_have_it():
    """字符轨会把正确的意译压成低分（rescue 机制就是为此存在），选型不该被措辞偏好带偏。"""
    a = [_row(f"s{i}", "en", 0.30, semantic=0.88) for i in range(12)]
    b = [_row(f"s{i}", "en", 0.90, semantic=0.86) for i in range(12)]
    c = compare(_report(a), _report(b))
    assert c["metric"] == "semantic"
    assert c["verdict"] in ("a_better", "a_better_marginal", "no_difference")


def test_falls_back_to_char_when_embedding_unavailable():
    a = [_row(f"s{i}", "en", 0.30) for i in range(12)]
    b = [_row(f"s{i}", "en", 0.90) for i in range(12)]
    c = compare(_report(a), _report(b))
    assert c["metric"] == "char"
    assert c["verdict"] == "b_better"


def test_explicit_metric_overrides_auto():
    a = [_row(f"s{i}", "en", 0.30, semantic=0.88) for i in range(12)]
    b = [_row(f"s{i}", "en", 0.90, semantic=0.86) for i in range(12)]
    c = compare(_report(a), _report(b), metric="char")
    assert c["metric"] == "char" and c["verdict"] == "b_better"


# ---------------------------------------------------------------- 语对 & 配置建议

def test_per_pair_verdict_needs_enough_samples():
    a = [_row("一", "hi", 0.4), _row("二", "hi", 0.4), _row("三", "hi", 0.4),
         _row("四", "ja", 0.8)]
    b = [_row("一", "hi", 0.9), _row("二", "hi", 0.9), _row("三", "hi", 0.9),
         _row("四", "ja", 0.2)]
    c = compare(_report(a), _report(b), min_pair_n=3)
    assert c["by_pair"]["zh->hi"]["verdict"] == "b_better"
    assert c["by_pair"]["zh->ja"]["verdict"] == "insufficient"


def test_same_engine_name_refuses_a_misleading_per_lang_order_suggestion():
    """per_lang_order 按 **引擎名** 路由。两个 ollama_mt 模型对它是同一个东西——
    照抄建议只会写出一条无效配置，还让人以为已经按语对分流了。"""
    a = [_row(f"s{i}", "hi", 0.4) for i in range(5)]
    b = [_row(f"s{i}", "hi", 0.9) for i in range(5)]
    c = compare(_report(a), _report(b))
    s = suggest_per_lang_order(c, a_engine="ollama_mt", b_engine="ollama_mt")
    assert s["applicable"] is False
    assert s["order"] == {}
    assert "model" in s["note"]
    assert "不适用" in format_ab_report(c, s)


def test_distinct_engines_get_an_actionable_order():
    a = [_row(f"s{i}", "hi", 0.4) for i in range(5)]
    b = [_row(f"s{i}", "hi", 0.9) for i in range(5)]
    c = compare(_report(a), _report(b))
    s = suggest_per_lang_order(c, a_engine="ollama_mt", b_engine="ai")
    assert s["applicable"] is True
    assert s["order"] == {"hi": ["ai", "ollama_mt"]}


def test_tied_pairs_produce_no_config_churn():
    a = [_row(f"s{i}", "en", 0.70) for i in range(10)]
    b = [_row(f"s{i}", "en", 0.70) for i in range(10)]
    c = compare(_report(a), _report(b))
    s = suggest_per_lang_order(c, a_engine="ollama_mt", b_engine="ai")
    assert s["order"] == {}


# ---------------------------------------------------------------- 健壮性

@pytest.mark.parametrize("bad", [{}, {"results": []}, {"results": None}])
def test_degenerate_reports_do_not_crash(bad):
    c = compare(dict(bad), dict(bad))
    assert c["verdict"] == "insufficient_data"
    assert isinstance(format_ab_report(c), str)
