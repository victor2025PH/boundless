# -*- coding: utf-8 -*-
"""B3 前置：composer 按钮用量裁决 CLI 门禁（纯函数 + 埋点接线钉）。

与 iflt 主线同法：纪元日口径（零点击是合法证据）/ 样本闸门 / 埋点链自证。
另含模板接线钉——9 个 cbtn_* 埋点点位被移除时先红（埋点断链=该按钮永远
无法参与瘦身裁决，比误判更糟）。
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools"))

from composer_button_report import (  # noqa: E402
    BUTTONS, CBTN_EPOCH_DAY, DENOM, analyze_rows, observed_days,
)


def _now_at(days_after_epoch: int) -> float:
    d = date.fromisoformat(CBTN_EPOCH_DAY) + timedelta(days=days_after_epoch)
    return time.mktime(d.timetuple()) + 12 * 3600


def _rows(**totals):
    return [{"day": CBTN_EPOCH_DAY, "action": k, "n": v} for k, v in totals.items()]


def test_insufficient_before_min_days():
    rep = analyze_rows(_rows(cbtn_send=50, cbtn_ai=10), now=_now_at(2), min_days=7)
    assert rep["observed_days"] == 3
    assert all(b["verdict"] == "insufficient" for b in rep["buttons"].values())


def test_zero_chain_flags_wiring_not_usage():
    rep = analyze_rows([], now=_now_at(10), min_days=7)
    assert rep["chain_alive"] is False
    assert all(b["verdict"] == "insufficient" for b in rep["buttons"].values())
    assert "埋点链" in rep["buttons"]["cbtn_ai"]["why"]


def test_keep_vs_fold_verdicts():
    rep = analyze_rows(
        _rows(cbtn_send=1000, cbtn_ai=200, cbtn_kbdhelp=2, cbtn_voice=1),
        now=_now_at(9), min_days=7, keep_share=0.05, keep_daily=3.0)
    assert rep["buttons"]["cbtn_ai"]["verdict"] == "keep"          # 份额 20%
    assert rep["buttons"]["cbtn_kbdhelp"]["verdict"] == "fold_candidate"
    assert rep["buttons"]["cbtn_voice"]["verdict"] == "fold_candidate"
    # 零点击满观察期同样出裁决（零就是答案）
    assert rep["buttons"]["cbtn_emoji"]["verdict"] == "fold_candidate"


def test_daily_avg_line_keeps_low_share_but_frequent():
    # 份额低于 5% 但日均 ≥3 → 仍 keep（高频小按钮不该被份额线误杀）
    rep = analyze_rows(
        _rows(cbtn_send=10000, cbtn_media=40),
        now=_now_at(9), min_days=7, keep_share=0.05, keep_daily=3.0)
    assert rep["buttons"]["cbtn_media"]["daily_avg"] >= 3
    assert rep["buttons"]["cbtn_media"]["verdict"] == "keep"


def test_observed_days_epoch_semantics():
    assert observed_days(_now_at(0)) == 1
    assert observed_days(_now_at(13)) == 14


def test_template_beacon_wiring_pins():
    """接线钉：全部顶行入口 + 发送分母的埋点必须在模板里（断链先红）。"""
    tpl = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    for key in list(BUTTONS) + [DENOM]:
        assert f"_uiBeacon('{key}')" in tpl, f"模板丢失埋点 {key}"
    # 发送分母两个动作分支（媒体发送 / 文本+编辑）都在
    assert tpl.count(f"_uiBeacon('{DENOM}')") >= 2
