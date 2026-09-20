# -*- coding: utf-8 -*-
"""驾驶舱用量裁决 CLI 门禁：聚合/观测天数/判词纯函数 + 只读契约。

覆盖：埋点纪元观测口径（零点击分不清「没人用」和「没装」，起点必须钉纪元日）、
零点击=合法证据（满窗后照样裁决）、埋点链自证（全 0 → 先查链）、五问各主要
分支、DB 不存在绝不创建空库、ro 连接读真库、埋点名与 cockpit.html 逐字一致
（防模板改埋点名后 CLI 静默读空）。只读工具，绝不改行为。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.cockpit_usage_report import (
    CK_EPOCH_DAY,
    _report_for_root,
    aggregate,
    build_verdicts,
    observed_days,
    read_rows,
)

EPOCH = CK_EPOCH_DAY  # "2026-08-14"
READY_DAY = "2026-08-28"  # 纪元 + 14 天（观测=15 ≥ 14）

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
_COCKPIT_HTML = _ENGINE_ROOT / "src" / "web" / "templates" / "cockpit.html"


def _rows(**totals):
    """按 {action: n} 造单日 rows（默认落在纪元日）。"""
    return [{"day": EPOCH, "action": a, "n": n} for a, n in totals.items()]


def _verdict(vs, q_sub):
    hits = [v for v in vs if q_sub in v["question"]]
    assert hits, f"未找到判词: {q_sub}"
    return hits[0]


def test_aggregate_totals_and_first_seen():
    rows = [
        {"day": "2026-08-14", "action": "ck_open", "n": 2},
        {"day": "2026-08-15", "action": "ck_open", "n": 3},
        {"day": "2026-08-15", "action": "ck_resolve", "n": 1},
    ]
    agg = aggregate(rows)
    assert agg["totals"]["ck_open"] == 5
    assert agg["grand_total"] == 6
    assert agg["first_seen"]["ck_open"] == "2026-08-14"
    assert agg["first_seen"]["ck_resolve"] == "2026-08-15"


def test_observed_days_epoch_inclusive():
    assert observed_days(EPOCH) == 1          # 纪元当天=1
    assert observed_days("2026-08-27") == 14  # 满两周
    assert observed_days("2026-08-01") == 0   # 纪元前（时钟错乱）→ 保守 0
    assert observed_days("bad-date") == 0


def test_insufficient_before_min_days_with_eta():
    agg = aggregate(_rows(ck_open=4, ck_resolve=1))
    vs = build_verdicts(agg, now_day="2026-08-16", min_days=14, min_total=20)
    q1 = _verdict(vs, "页面采用度")
    assert q1["status"] == "insufficient"
    assert q1["eta_days"] and q1["eta_days"] >= 11
    assert "观测 3/14" in q1["note"]


def test_broken_chain_guard_blocks_all_verdicts():
    # 满窗但 ck_* 全 0（含 ck_open 页面打开）→ 全部「样本不足」并点名先查埋点链
    vs = build_verdicts(aggregate([]), now_day=READY_DAY, min_days=14, min_total=20)
    assert all(v["status"] == "insufficient" for v in vs)
    assert any("埋点链" in (v.get("note") or "") for v in vs)


def test_q1_zero_opens_recommends_entry_rethink():
    # 有其他埋点（链没断）但 ck_open=0（理论形态：直链操作）→ 零访问裁决
    agg = aggregate(_rows(ck_take=3))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q1 = _verdict(vs, "页面采用度")
    assert q1["status"] == "ready"
    assert "零访问" in q1["recommendation"]


def test_q2_resolve_unused_with_traffic_flags_visibility():
    agg = aggregate(_rows(ck_open=30, ck_resolve=0))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q2 = _verdict(vs, "已处理")
    assert q2["status"] == "ready" and q2["confidence"] == "high"
    assert "零使用" in q2["recommendation"]


def test_q2_resolve_used_confirms_outlet():
    agg = aggregate(_rows(ck_open=30, ck_resolve=8))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q2 = _verdict(vs, "已处理")
    assert q2["status"] == "ready"
    assert "成立" in q2["recommendation"]


def test_q3_filter_zero_clicks_high_confidence():
    # 零点击不降置信——零就是答案
    agg = aggregate(_rows(ck_open=25))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q3 = _verdict(vs, "筛选")
    assert q3["status"] == "ready" and q3["confidence"] == "high"
    assert "收掉" in q3["recommendation"]


def test_q3_filter_prefix_bucket_counts_all_kinds():
    agg = aggregate(_rows(ck_open=25, ck_filter_waiting=5,
                          ck_filter_all=3, ck_filter_needs_human=2))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q3 = _verdict(vs, "筛选")
    assert q3["evidence"]["ck_filter_*"] == 10
    assert "保留" in q3["recommendation"]


def test_q5_take_without_handback_flags_leak():
    agg = aggregate(_rows(ck_open=40, ck_take=20, ck_handback=3))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q5 = _verdict(vs, "接管")
    assert q5["status"] == "ready"
    assert "接完不还" in q5["recommendation"]


def test_read_rows_missing_db_never_creates(tmp_path):
    db = tmp_path / "config" / "ui_event_trend.db"
    try:
        read_rows(db)
        assert False, "应抛 FileNotFoundError"
    except FileNotFoundError:
        pass
    assert not db.exists()  # 绝不创建空库
    rep = _report_for_root(tmp_path, days=30, min_days=14, min_total=20)
    assert rep.get("missing") is True


def test_read_rows_ro_and_prefix_filter(tmp_path):
    db = tmp_path / "ui_event_trend.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ui_event_trend_daily "
                 "(day TEXT, action TEXT, n INTEGER, PRIMARY KEY (day, action))")
    conn.executemany(
        "INSERT INTO ui_event_trend_daily VALUES (?, ?, ?)",
        [("2026-08-14", "ck_open", 5),
         ("2026-08-14", "ck_resolve", 2),
         ("2026-08-14", "iflt_attn", 9),      # 他域埋点：必须被前缀滤掉
         ("2026-08-14", "ckx_bogus", 1)],     # ck 前缀边界：ck_ 才算
    )
    conn.commit()
    conn.close()
    rows = read_rows(db, days=3650, now=1786700000)  # now 不重要，窗口够宽即可
    acts = {r["action"] for r in rows}
    assert acts == {"ck_open", "ck_resolve"}


def test_beacon_names_pinned_to_template():
    """CLI 消费的埋点名必须在 cockpit.html 里逐字存在——改埋点名两边要一起改。"""
    src = _COCKPIT_HTML.read_text(encoding="utf-8")
    for name in ("ck_open", "ck_take", "ck_handback", "ck_resolve",
                 "ck_hint_dismiss"):
        assert f"'{name}'" in src, f"cockpit.html 缺埋点 {name}"
    # 前缀型（拼接产生）：钉拼接源
    assert "ck_filter_" in src and "ck_stale_" in src
