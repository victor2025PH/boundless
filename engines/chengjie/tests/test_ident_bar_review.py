# -*- coding: utf-8 -*-
"""身份条「跳转→换绑」裁决 CLI 门禁（tools/ident_bar_review.py）。

钉三类不变量：
1. **纪元契约**：纪元日 2026-08-17 与模板转化窗 30s 是同一套度量语义——
   谁改模板窗口/纪元日而不同步这里，先红。
2. **判词纯函数**：样本闸门（min_days/min_jumps）、链路自证（整库零≠分桶零）、
   三档转化率分支各自给对方向；证据字段齐全。
3. **读侧口径**：LIKE 前缀只取 ident_bar_*，db_total 数整库（经真 UiEventTrendStore
   round-trip 验证，防转义写错静默漏读）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.ident_bar_review import (  # noqa: E402
    ACTION_BIND,
    ACTION_JUMP,
    CONV_HIGH,
    CONV_LOW,
    IDENT_EPOCH_DAY,
    aggregate,
    build_verdict,
    observed_days,
    read_window,
)


def test_epoch_and_window_pinned():
    """纪元日 + 模板 30s 窗 + 判词工具三者互指；改一处必须全改。"""
    assert IDENT_EPOCH_DAY == "2026-08-17"
    tpl = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "_IDENT_JUMP_BIND_MS=30000" in tpl
    assert "ident_bar_review" in tpl  # 纪元注释指路读数单源


def test_observed_days():
    assert observed_days("2026-08-17") == 1
    assert observed_days("2026-08-31") == 15
    assert observed_days("bad-date") == 0


def test_insufficient_before_min_days():
    v = build_verdict(5, 2, 3, 100, min_days=14)
    assert v["status"] == "insufficient"
    assert v["eta_days"] == 11
    assert v["evidence"]["jumps"] == 5


def test_chain_break_when_whole_db_empty():
    """整库零流量＝链路问题，不是「没人点」。"""
    v = build_verdict(0, 0, 20, 0, min_days=14)
    assert v["status"] == "insufficient"
    assert "链路" in v["note"]


def test_zero_jumps_is_a_verdict_when_db_alive():
    """整库有流量而分桶为零 → 零就是答案（高置信，不做气泡）。"""
    v = build_verdict(0, 0, 20, 500, min_days=14)
    assert v["status"] == "ready"
    assert v["confidence"] == "high"
    assert "不做气泡" in v["recommendation"]


def test_high_conversion_recommends_popover():
    v = build_verdict(40, 20, 20, 900, min_days=14, min_jumps=20)
    assert v["status"] == "ready"
    assert v["confidence"] == "high"
    assert "气泡换绑" in v["recommendation"]
    assert v["evidence"]["conv_pct"] == 50.0


def test_low_conversion_recommends_card_fix():
    v = build_verdict(40, 2, 20, 900, min_days=14, min_jumps=20)
    assert v["status"] == "ready"
    assert "人设卡" in v["recommendation"]
    assert "不做气泡" in v["recommendation"]


def test_mid_conversion_keeps_jump():
    v = build_verdict(40, 10, 20, 900, min_days=14, min_jumps=20)
    assert v["status"] == "ready"
    assert "维持跳转" in v["recommendation"]


def test_low_sample_ratio_degrades_confidence():
    v = build_verdict(3, 2, 20, 900, min_days=14, min_jumps=20)
    assert v["status"] == "ready"
    assert v["confidence"] == "low"


def test_low_absolute_demand_noted_even_on_high_conversion():
    """转化率高但周均跳转极低 → 判词必须标注绝对需求低。"""
    v = build_verdict(4, 3, 30, 900, min_days=14, min_jumps=20)
    assert "绝对需求低" in v["recommendation"]


def test_binds_never_exceed_jumps_in_ratio():
    v = build_verdict(2, 5, 20, 900, min_days=14, min_jumps=20)
    assert v["evidence"]["conv_pct"] == 100.0


def test_thresholds_sane():
    assert 0 < CONV_LOW < CONV_HIGH < 1


def test_aggregate_counts_only_ident_actions():
    rows = [
        {"day": "2026-08-17", "action": ACTION_JUMP, "n": 3},
        {"day": "2026-08-17", "action": ACTION_BIND, "n": 1},
        {"day": "2026-08-18", "action": ACTION_JUMP, "n": 2},
        {"day": "2026-08-18", "action": "ident_bar_future_bucket", "n": 9},
    ]
    agg = aggregate(rows)
    assert agg["jumps"] == 5
    assert agg["binds"] == 1
    assert agg["by_day"]["2026-08-18"][ACTION_JUMP] == 2


def test_read_window_roundtrip(tmp_path):
    """经真 UiEventTrendStore 落库再读：前缀过滤 + 整库计数两个口径都对。"""
    from src.web.ui_event_trend import UiEventTrendStore

    db = tmp_path / "config" / "ui_event_trend.db"
    store = UiEventTrendStore(db)
    store.add(ACTION_JUMP, n=4)
    store.add(ACTION_BIND, n=1)
    store.add("iflt_claimed", n=7)          # 别的命名空间：不进 rows、进 db_total
    store.add("identity_other", n=2)        # 前缀相近但非 ident_bar_*：同上

    win = read_window(db, days=7)
    acts = {r["action"] for r in win["rows"]}
    assert acts == {ACTION_JUMP, ACTION_BIND}
    agg = aggregate(win["rows"])
    assert agg["jumps"] == 4 and agg["binds"] == 1
    assert win["db_total"] == 14


def test_read_window_missing_db(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        read_window(tmp_path / "config" / "ui_event_trend.db")
