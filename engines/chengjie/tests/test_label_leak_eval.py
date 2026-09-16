# -*- coding: utf-8 -*-
"""系统标签泄漏评测门禁：确定性轨常驻全绿 + 诱导轨探测器自证（假 LLM）。"""
from __future__ import annotations

from src.eval.label_leak_eval import (
    evaluate_label_imitation,
    evaluate_label_leak,
    evaluate_label_leak_deterministic,
    format_label_leak_report,
)


def test_deterministic_track_passes():
    rep = evaluate_label_leak_deterministic()
    assert rep["passed"], rep["errors"]
    assert rep["summary"]["correct"] == rep["summary"]["total"] >= 10


def test_imitation_track_skips_without_generate_fn():
    rep = evaluate_label_imitation(None)
    assert rep["available"] is False and rep["passed"] is True
    full = evaluate_label_leak(None)
    assert full["passed"] is True
    txt = format_label_leak_report(full)
    assert "未跑" in txt and "[PASS]" in txt


def _imitating_llm(user_message, history, system_hint):
    """模拟真事故行为：历史里 assistant 以标签开头就照抄；否则正常说话。"""
    asst = [r for r in history if r.get("role") == "assistant"]
    if asst and all(str(r.get("content", "")).startswith("[") for r in asst):
        return "[我方语音消息] 好呀，那我用中文说。"
    return "好呀，那我用中文说。"


def _always_leaking_llm(user_message, history, system_hint):
    return "[Voice message from our side] okay"


def test_imitation_track_detects_leak_and_control_shows_sensitivity():
    rep = evaluate_label_imitation(_imitating_llm, rounds=3)
    assert rep["available"] is True
    cur, ctrl = rep["summary"]["current"], rep["summary"]["control"]
    assert cur["leaks"] == 0 and rep["passed"] is True          # 现口径：干净历史不诱导
    assert ctrl["leaks"] == 3 and ctrl["rate"] == 1.0            # 事故口径对照：全照抄 → 轨能抓
    bad = evaluate_label_imitation(_always_leaking_llm, rounds=2)
    assert bad["passed"] is False and bad["errors"][0]["layer"] == "imitation"
    full = evaluate_label_leak(_always_leaking_llm, rounds=2)
    assert full["passed"] is False
    assert "[FAIL]" in format_label_leak_report(full)


def test_generate_fn_exception_counts_as_non_leak_but_is_visible():
    def _boom(*a, **k):
        raise RuntimeError("endpoint down")
    rep = evaluate_label_imitation(_boom, rounds=2, with_control=False)
    assert rep["passed"] is True and rep["summary"]["control"] is None
