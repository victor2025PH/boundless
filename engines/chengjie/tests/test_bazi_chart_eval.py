"""命盘质量评测门禁：四条确定性轨常驻全绿 + 探测器有效性（坏数据必报警）。"""

from __future__ import annotations

import pytest

from src.companion.bazi_engine import bazi_available
from src.eval.bazi_chart_eval import (
    GOLDEN_PILLARS,
    evaluate_bazi_chart,
    format_bazi_chart_report,
)

pytestmark = pytest.mark.skipif(
    not bazi_available(), reason="lunar_python 未安装")


@pytest.fixture(scope="module")
def report():
    # 门禁用轻量扫描（~90 盘，秒级）；CLI 默认 50 年更宽
    return evaluate_bazi_chart(sweep_start=1980, sweep_years=25, sweep_step_days=97)


def test_all_tracks_pass(report):
    assert report["available"] is True
    assert report["passed"] is True, format_bazi_chart_report(report)


def test_pillars_track_full_score(report):
    t = report["tracks"]["pillars"]
    assert t["ok"] == t["total"] == len(GOLDEN_PILLARS)
    assert t["errors"] == []


def test_shishen_cross_zero_mismatch(report):
    t = report["tracks"]["shishen_cross"]
    assert t["charts"] > 50  # 扫描量足够才有说服力
    assert t["mismatches"] == 0


def test_strength_invariants_zero_violation(report):
    assert report["tracks"]["strength_invariants"]["violations"] == 0


def test_kline_sanity(report):
    assert report["tracks"]["kline_sanity"]["errors"] == []


def test_detector_catches_pillar_drift(monkeypatch):
    """有效性自证：篡改一条金标 → pillars 轨必 FAIL（评测不是摆设）。"""
    import src.eval.bazi_chart_eval as mod
    bad = list(GOLDEN_PILLARS)
    y, m, d, gy, gm, gd, note = bad[0]
    bad[0] = (y, m, d, gy, gm, "癸丑", note)  # 故意写错日柱（甲子→癸丑）
    monkeypatch.setattr(mod, "GOLDEN_PILLARS", bad)
    rep = mod.evaluate_bazi_chart(
        sweep_start=2000, sweep_years=2, sweep_step_days=200)
    assert rep["tracks"]["pillars"]["passed"] is False
    assert rep["passed"] is False


def test_report_format_lines(report):
    txt = format_bazi_chart_report(report)
    assert "四柱金标" in txt and "十神交叉验证" in txt
    assert "[PASS]" in txt and "总判" in txt


def test_skip_report_when_unavailable(monkeypatch):
    import src.eval.bazi_chart_eval as mod
    monkeypatch.setattr(
        "src.companion.bazi_engine.bazi_available", lambda: False)
    rep = mod.evaluate_bazi_chart()
    assert rep["available"] is False and rep["passed"] is None
    assert "SKIP" in format_bazi_chart_report(rep)
