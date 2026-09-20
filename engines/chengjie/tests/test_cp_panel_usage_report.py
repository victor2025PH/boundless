# -*- coding: utf-8 -*-
"""业务助手用量裁决 CLI 的纯函数门禁（tools/cp_panel_usage_report.py）。

守的是**裁决口径**而不是数字本身：纪元剔除（08-17 门禁污染日绝不进分子分母）、
样本闸门（天数/总量未到只出 wait）、零点击是证据（撕出满窗 0 → 常驻图标建议）、
链路自证（满窗全 0 → 先查链）。CLI 与真库交互走 read_rows 的 ro-URI，另测。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from tools.cp_panel_usage_report import (  # noqa: E402
    CPPANEL_EPOCH_DAY,
    aggregate,
    build_verdicts,
    observed_days,
    read_rows,
    split_epoch,
)


def _v(verdicts, topic):
    for it in verdicts:
        if it["topic"] == topic:
            return it
    return None


def test_epoch_day_is_after_gate_polluted_launch_day():
    # 上线日 2026-08-17 门禁真点了 5+ 轮（cppanel_* 假用量）；纪元必须晚于它
    assert CPPANEL_EPOCH_DAY > "2026-08-17"


def test_split_epoch_excludes_polluted_days():
    rows = [
        {"day": "2026-08-17", "action": "cppanel_rail", "n": 30},   # 门禁污染日
        {"day": "2026-08-18", "action": "cppanel_rail", "n": 2},
        {"day": "2026-08-19", "action": "cppanel_pin", "n": 1},
    ]
    parts = split_epoch(rows)
    assert sum(r["n"] for r in parts["polluted"]) == 30
    agg = aggregate(parts["clean"])
    assert agg["totals"] == {"cppanel_rail": 2, "cppanel_pin": 1}
    assert agg["grand_total"] == 3


def test_observed_days_semantics():
    assert observed_days("2026-08-18") == 1          # 纪元当天=1
    assert observed_days("2026-08-24") == 7
    assert observed_days("2026-08-10") == 0          # 纪元在未来 → 0（保守）
    assert observed_days("garbage") == 0


def test_wait_gate_before_min_days():
    v = build_verdicts({"cppanel_rail": 5}, 3, min_days=7, min_total=12)
    assert len(v) == 1 and v[0]["topic"] == "sample" and v[0]["status"] == "wait"


def test_chain_check_when_zero_after_window():
    v = build_verdicts({}, 14, min_days=7, min_total=12)
    assert len(v) == 1 and v[0]["topic"] == "chain" and v[0]["status"] == "check"


def test_tear_zero_is_evidence_not_wait():
    # 面板有量、撕出为 0 → 入口隐蔽判词（action），绝不是「再等等」
    totals = {"cppanel_rail": 40, "cppanel_flyout": 20, "cppanel_pin": 12}
    it = _v(build_verdicts(totals, 10, min_days=7, min_total=12), "tear")
    assert it and it["status"] == "action" and "常驻" in it["text"]


def test_tear_small_sample_waits():
    totals = {"cppanel_rail": 40, "cppanel_tear": 3}
    it = _v(build_verdicts(totals, 10, min_days=7, min_total=12), "tear")
    assert it and it["status"] == "wait"


def test_tear_adopted_suggests_expansion():
    totals = {"cppanel_tear": 20, "cppanel_tear_dock": 6, "cppanel_rail": 5}
    it = _v(build_verdicts(totals, 10, min_days=7, min_total=12), "tear")
    assert it and it["status"] == "action" and "撕出集合" in it["text"]


def test_tear_high_dock_rate_flags_size_check():
    totals = {"cppanel_tear": 20, "cppanel_tear_dock": 18, "cppanel_rail": 5}
    it = _v(build_verdicts(totals, 10, min_days=7, min_total=12), "tear")
    assert it and it["status"] == "check"


def test_flyout_pin_rate_suggests_dock_default():
    totals = {"cppanel_flyout": 20, "cppanel_pin": 12, "cppanel_flyout_dismiss": 2,
              "cppanel_tear": 20, "cppanel_tear_dock": 2}
    it = _v(build_verdicts(totals, 10, min_days=7, min_total=12), "flyout")
    assert it and it["status"] == "action" and "expanded" in it["text"]


def test_flyout_dismiss_rate_keeps_click_preview():
    totals = {"cppanel_flyout": 20, "cppanel_pin": 2, "cppanel_flyout_dismiss": 15,
              "cppanel_tear": 20, "cppanel_tear_dock": 2}
    it = _v(build_verdicts(totals, 10, min_days=7, min_total=12), "flyout")
    assert it and it["status"] == "ok" and "悬停" in it["text"]


def test_modes_line_always_present_when_data():
    totals = {"cppanel_expanded": 4, "cppanel_rail": 2, "cppanel_hidden": 1,
              "cppanel_flyout": 20, "cppanel_pin": 12, "cppanel_tear": 20}
    v = build_verdicts(totals, 10, min_days=7, min_total=12)
    it = _v(v, "modes")
    assert it and "expanded 4" in it["text"] and "rail 2" in it["text"]


def test_read_rows_filters_prefixes_and_never_creates_db(tmp_path):
    db = tmp_path / "ui_event_trend.db"
    # 库不存在 → FileNotFoundError（绝不创建空库）
    with pytest.raises(FileNotFoundError):
        read_rows(db, days=30)
    assert not db.exists()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ui_event_trend_daily("
                 "day TEXT NOT NULL, action TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,"
                 "PRIMARY KEY (day, action))")
    conn.executemany(
        "INSERT INTO ui_event_trend_daily (day, action, n) VALUES (?, ?, ?)",
        [
            ("2026-08-18", "cppanel_rail", 3),
            ("2026-08-18", "cpapp_on", 1),
            ("2026-08-18", "iflt_mine", 9),        # 别的命名空间：不得混入
            ("2026-08-18", "cppanelXfake", 7),     # 下划线通配符必须被转义
        ],
    )
    conn.commit()
    conn.close()
    rows = read_rows(db, days=3650)
    acts = sorted(r["action"] for r in rows)
    assert acts == ["cpapp_on", "cppanel_rail"]
