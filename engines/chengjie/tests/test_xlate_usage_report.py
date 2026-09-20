# -*- coding: utf-8 -*-
"""对话翻译弹层用量裁决 CLI 门禁（tools/xlate_usage_report.py，纯函数层）。

与 test_inbox_filter_usage_report 同族：喂合成 rows 验证判词状态机——
埋点链自证 / 纪元天数闸门 / 五个问题在关键证据组合下的推荐方向。
判词文案可改，**状态与方向**（ready/insufficient、保留/调整）是契约。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from xlate_usage_report import (  # noqa: E402
    XLT_EPOCH_DAY, aggregate, build_verdicts, observed_days, quarantine_pre_epoch,
)


def _rows(day: str, **actions) -> list:
    return [{"day": day, "action": a, "n": n} for a, n in actions.items()]


def _verdict(vs, question_kw):
    for v in vs:
        if question_kw in v["question"]:
            return v
    raise AssertionError(f"缺判词: {question_kw}")


def test_observed_days_epoch_math():
    assert observed_days(XLT_EPOCH_DAY) == 1          # 纪元日当天=1
    assert observed_days("2026-08-30") == 14          # 纪元 08-17 → 第 14 天
    assert observed_days("bogus") == 0                # 解析失败保守 0


def test_quarantine_drops_launch_day_gate_noise():
    """纪元（08-17）前的行整体隔离——上线当天（08-16）的门禁点击不进裁决样本。"""
    rows = _rows("2026-08-16", xlt_quick_on=22) + _rows("2026-08-17", xlt_quick_on=3)
    kept = quarantine_pre_epoch(rows)
    assert {r["day"] for r in kept} == {"2026-08-17"}
    assert aggregate(kept)["grand_total"] == 3


def test_broken_chain_all_insufficient():
    """窗口内 xlt_* 全 0 → 全部判词点名先查埋点链。"""
    agg = aggregate([])
    vs = build_verdicts(agg, now_day="2026-09-01")
    assert len(vs) == 5
    assert all(v["status"] == "insufficient" for v in vs)
    assert all("埋点链" in (v.get("note") or "") for v in vs)


def test_before_min_days_insufficient_with_eta():
    agg = aggregate(_rows("2026-08-17", xlt_quick_on=5, xlt_suggest_shown=3))
    vs = build_verdicts(agg, now_day="2026-08-18", min_days=14)  # 观测第 2 天
    q1 = _verdict(vs, "建议条")
    assert q1["status"] == "insufficient"
    assert q1["eta_days"] and q1["eta_days"] >= 12


def test_suggest_zero_shows_is_good_news():
    """建议条零曝光=smart default 已点亮外语会话，判词方向=不动。"""
    agg = aggregate(_rows("2026-08-20", xlt_quick_on=30))
    vs = build_verdicts(agg, now_day="2026-09-01", min_days=14)
    q1 = _verdict(vs, "建议条")
    assert q1["status"] == "ready" and q1["confidence"] == "high"
    assert "不动" in q1["recommendation"]


def test_suggest_high_and_low_conversion_directions():
    high = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_suggest_shown=40, xlt_suggest_accept=20)),
        now_day="2026-09-01")
    assert "保留" in _verdict(high, "建议条")["recommendation"]
    low = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_suggest_shown=40, xlt_suggest_accept=2)),
        now_day="2026-09-01")
    assert "低转化" in _verdict(low, "建议条")["recommendation"]


def test_quick_on_heavy_flags_smart_default():
    """开远多于关且过样本量 → 点名查 smart default 抑制条件。"""
    vs = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_quick_on=30, xlt_quick_off=3)),
        now_day="2026-09-01")
    assert "smart default" in _verdict(vs, "一键开关")["recommendation"]


def test_preview_adoption_directions():
    none = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_quick_on=25)),
        now_day="2026-09-01")
    assert "维持「一击直发」" in _verdict(none, "预览")["recommendation"]
    hot = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_preview_on=24, xlt_preview_off=4)),
        now_day="2026-09-01")
    assert "新坐席默认" in _verdict(hot, "预览")["recommendation"]


def test_mgr_weekly_rate_threshold():
    # 观测 17 天（09-01 vs 纪元 08-16），24 次 ≈ 9.9 次/周 → 升级设置中心
    hot = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_mgr_open=24)),
        now_day="2026-09-01")
    assert "设置中心" in _verdict(hot, "管理弹窗")["recommendation"]
    cold = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_mgr_open=1, xlt_quick_on=5)),
        now_day="2026-09-01")
    assert "维持弹窗" in _verdict(cold, "管理弹窗")["recommendation"]


def test_agent_lang_adoption_directions():
    used = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_agent_lang_set=3, xlt_quick_on=5)),
        now_day="2026-09-01")
    assert "保留" in _verdict(used, "我的语言")["recommendation"]
    unused = build_verdicts(
        aggregate(_rows("2026-08-20", xlt_quick_on=5)),
        now_day="2026-09-01")
    assert "维持现状" in _verdict(unused, "我的语言")["recommendation"]
