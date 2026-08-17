# -*- coding: utf-8 -*-
"""多窗口治理验收 CLI（tools/multiwin_review.py）门禁。

判词是给运营看的验收结论，口径漂了比没有更糟——钉四件事：
修复日当天不入两段 / 修复后归零才报喜 / 残量必须点名数字 / 只读绝不建库。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.multiwin_review import collect_mw, summarize_mw  # noqa: E402

FIX = "2026-08-11"


def test_fix_day_rows_excluded_from_both_segments():
    rows = [
        ("2026-08-10", "mw.takeover_auto", 5),
        ("2026-08-11", "mw.takeover_reclaim", 8),   # 修复日：混杂，不定罪不报喜
        ("2026-08-12", "mw.takeover_auto", 0),
    ]
    s = summarize_mw(rows, FIX)
    assert s["dup_pre"] == 5
    assert s["dup_post"] == 0
    # 修复日行仍进日表（可见），只是不入两段
    assert any(r["day"] == "2026-08-11" and r["mw.takeover_reclaim"] == 8 for r in s["days"])


def test_clean_post_gives_ok_verdict():
    rows = [
        ("2026-08-10", "mw.takeover_auto", 5),
        ("2026-08-12", "mw.promoted", 1),           # 自愈接管不算重复窗口
        ("2026-08-13", "mw.takeover_auto", 0),
    ]
    s = summarize_mw(rows, FIX)
    assert s["dup_post"] == 0 and s["post_days"] == 2
    assert any(v.startswith("[OK]") for v in s["verdicts"])
    assert not any(v.startswith("[WARN]") for v in s["verdicts"])


def test_residual_post_gives_warn_with_numbers():
    rows = [
        ("2026-08-12", "mw.takeover_auto", 2),
        ("2026-08-13", "mw.takeover_reclaim", 3),
    ]
    s = summarize_mw(rows, FIX)
    assert s["dup_post"] == 5
    warn = [v for v in s["verdicts"] if v.startswith("[WARN]")]
    assert warn and "takeover_auto=2" in warn[0] and "reclaim=3" in warn[0]


def test_no_post_days_says_come_back_tomorrow():
    rows = [("2026-08-11", "mw.takeover_reclaim", 8)]
    s = summarize_mw(rows, FIX)
    assert s["post_days"] == 0
    assert any("明天" in v for v in s["verdicts"])


def test_boot_standby_reported_as_info_not_warn():
    rows = [("2026-08-12", "mw.boot_standby", 2)]
    s = summarize_mw(rows, FIX)
    assert s["dup_post"] == 0
    assert any(v.startswith("[INFO]") and "boot_standby=2" in v for v in s["verdicts"])
    assert any(v.startswith("[OK]") for v in s["verdicts"])


def test_unknown_actions_ignored():
    rows = [("2026-08-12", "mw.someday_new_metric", 9), ("2026-08-12", "dpick.open", 3)]
    s = summarize_mw(rows, FIX)
    assert s["dup_post"] == 0 and not s["days"] or all(
        set(r) <= {"day", *("mw.takeover_auto", "mw.takeover_reclaim",
                            "mw.boot_standby", "mw.promoted")} for r in s["days"])


def test_collect_missing_db_returns_none_and_never_creates(tmp_path):
    p = tmp_path / "config" / "ui_event_trend.db"
    assert collect_mw(p, 14) is None
    assert not p.exists()          # 只读契约：绝不建库


def test_collect_reads_real_schema(tmp_path):
    p = tmp_path / "ui_event_trend.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE ui_event_trend_daily (day TEXT NOT NULL, action TEXT NOT NULL,"
                " n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, action))")
    con.execute("INSERT INTO ui_event_trend_daily VALUES ('2026-08-12', 'mw.takeover_auto', 4)")
    con.execute("INSERT INTO ui_event_trend_daily VALUES ('2026-08-12', 'iflt_more_open', 7)")
    con.commit()
    con.close()
    rows = collect_mw(p, 3650)
    assert rows == [("2026-08-12", "mw.takeover_auto", 4)]
