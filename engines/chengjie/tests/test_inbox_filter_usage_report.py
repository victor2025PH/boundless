# -*- coding: utf-8 -*-
"""收件箱筛选区用量裁决 CLI 门禁：聚合/观测天数/判词纯函数 + 只读契约。

覆盖：埋点纪元观测口径（数据首现日不可靠——零点击分不清「没人用」和「没装」）、
零点击=合法证据（满窗后照样裁决）、小样本比值降置信、埋点链自证（全 0 → 先查链）、
四问各主要分支、DB 不存在绝不创建空库、ro 连接读真库。只读工具，绝不改行为。
"""
from __future__ import annotations

import calendar
import sqlite3
import time
from pathlib import Path

from tools.inbox_filter_usage_report import (
    IFLT_EPOCH_DAY,
    IFLT_GFOCUS_EPOCH_DAY,
    _report_for_root,
    aggregate,
    build_verdicts,
    observed_days,
    read_rows,
)

EPOCH = IFLT_EPOCH_DAY  # "2026-08-11"
READY_DAY = "2026-08-25"  # 纪元 + 14 天（观测=15 ≥ 14）
GFOCUS_EPOCH = IFLT_GFOCUS_EPOCH_DAY  # "2026-08-20"
GFOCUS_READY_DAY = "2026-09-02"  # gfocus 纪元 + 13 天（观测=14 ≥ 14）


def _rows(**totals):
    """按 {action: n} 造单日 rows（默认落在纪元日）。"""
    return [{"day": EPOCH, "action": a, "n": n} for a, n in totals.items()]


def _verdict(vs, q_sub):
    hits = [v for v in vs if q_sub in v["question"]]
    assert hits, f"未找到判词: {q_sub}"
    return hits[0]


def test_aggregate_totals_and_first_seen():
    rows = [
        {"day": "2026-08-11", "action": "iflt_attn", "n": 2},
        {"day": "2026-08-12", "action": "iflt_attn", "n": 3},
        {"day": "2026-08-12", "action": "iflt_more_open", "n": 1},
    ]
    agg = aggregate(rows)
    assert agg["totals"]["iflt_attn"] == 5
    assert agg["grand_total"] == 6
    assert agg["first_seen"]["iflt_attn"] == "2026-08-11"
    assert agg["first_seen"]["iflt_more_open"] == "2026-08-12"


def test_observed_days_epoch_inclusive():
    assert observed_days(EPOCH) == 1          # 纪元当天=1
    assert observed_days("2026-08-24") == 14  # 满两周
    assert observed_days("2026-08-01") == 0   # 纪元前（时钟错乱）→ 保守 0
    assert observed_days("bad-date") == 0


def test_insufficient_before_min_days_with_eta():
    agg = aggregate(_rows(iflt_claimed=2, iflt_claimed_m=1, iflt_more_open=4))
    vs = build_verdicts(agg, now_day="2026-08-13", min_days=14, min_total=20)
    q1 = _verdict(vs, "我的")
    assert q1["status"] == "insufficient"
    assert q1["eta_days"] and q1["eta_days"] >= 11  # 至少还差 11 天
    assert "观测 3/14" in q1["note"]


def test_broken_chain_guard_blocks_all_verdicts():
    # 满窗但 iflt_* 全 0 → 全部「样本不足」并点名先查埋点链（零流量更像链断）
    vs = build_verdicts(aggregate([]), now_day=READY_DAY, min_days=14, min_total=20)
    assert all(v["status"] == "insufficient" for v in vs)
    assert any("埋点链" in (v.get("note") or "") for v in vs)


def test_q1_retire_row_when_panel_carries():
    agg = aggregate(_rows(iflt_claimed=0, iflt_claimed_m=25, iflt_more_open=30))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q1 = _verdict(vs, "我的")
    assert q1["status"] == "ready" and q1["confidence"] == "high"
    assert "撤主行" in q1["recommendation"]


def test_q1_keep_row_when_row_dominates():
    agg = aggregate(_rows(iflt_claimed=30, iflt_claimed_m=5, iflt_more_open=9))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q1 = _verdict(vs, "我的")
    assert q1["status"] == "ready"
    assert "保留主行" in q1["recommendation"]


def test_q1_low_confidence_on_tiny_ratio_sample():
    # 1 vs 2 的比值不是真理：ready 但降置信
    agg = aggregate(_rows(iflt_claimed=1, iflt_claimed_m=2, iflt_more_open=3))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q1 = _verdict(vs, "我的")
    assert q1["status"] == "ready" and q1["confidence"] == "low"


def test_q2_retire_strip_zero_clicks_is_high_confidence():
    # 满窗零点击=答案本身（不因 total<min_total 降置信）
    agg = aggregate(_rows(iflt_striptag=0, iflt_ptag=6, iflt_tags_open=2, iflt_more_open=8))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q2 = _verdict(vs, "tag-strip")
    assert q2["status"] == "ready" and q2["confidence"] == "high"
    assert "退役 tag-strip" in q2["recommendation"]


def test_q2_keep_strip_with_real_clicks():
    agg = aggregate(_rows(iflt_striptag=12, iflt_ptag=3, iflt_more_open=30))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    q2 = _verdict(vs, "tag-strip")
    assert q2["status"] == "ready"
    assert "保留 tag-strip" in q2["recommendation"]


def test_q3_presets_threshold():
    agg = aggregate(_rows(iflt_view_save=4, iflt_more_open=30))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    assert "值得建内置预设" in _verdict(vs, "视图预设")["recommendation"]
    agg2 = aggregate(_rows(iflt_view_save=0, iflt_more_open=30))
    vs2 = build_verdicts(agg2, now_day=READY_DAY, min_days=14, min_total=20)
    assert "优先级低" in _verdict(vs2, "视图预设")["recommendation"]


def test_read_rows_missing_db_never_creates(tmp_path: Path):
    db = tmp_path / "config" / "ui_event_trend.db"
    rep = _report_for_root(tmp_path, days=14, min_days=14, min_total=20)
    assert rep.get("missing") is True
    assert not db.exists()  # 只读契约：绝不创建空库


def test_read_rows_ro_and_prefix_filter(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    db = cfg / "ui_event_trend.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ui_event_trend_daily ("
                 "day TEXT NOT NULL, action TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, "
                 "PRIMARY KEY (day, action))")
    conn.executemany(
        "INSERT INTO ui_event_trend_daily (day, action, n) VALUES (?, ?, ?)",
        [(EPOCH, "iflt_attn", 7),
         (EPOCH, "ifltXattn", 3),      # LIKE 通配陷阱：_ 必须按字面匹配（ESCAPE）
         (EPOCH, "goal_form_open", 9)])  # 其他前缀不得混入
    conn.commit()
    conn.close()
    # now 锚定纪元日次日（勿用真实时钟：行落在固定纪元日，滚动窗口会让本测试三周后自然变红）
    now = calendar.timegm(time.strptime(EPOCH, "%Y-%m-%d")) + 86400.0
    rows = read_rows(db, days=14, now=now)
    acts = {r["action"] for r in rows}
    assert acts == {"iflt_attn"}
    rep = _report_for_root(tmp_path, days=14, min_days=14, min_total=20, now=now)
    assert rep["grand_total"] == 7 and rep["totals"]["iflt_attn"] == 7
    assert len(rep["verdicts"]) == 5


# ── Q5 群焦点过滤：独立纪元（晚上线分桶不得吃全局窗口）──────────────────────


def test_gfocus_uses_own_epoch_not_global():
    # 全局裁决日（全局纪元+14）：Q1 已可裁决，gfocus 上线才 6 天 → 必须 insufficient
    agg = aggregate(_rows(iflt_claimed=30))
    vs = build_verdicts(agg, now_day=READY_DAY, min_days=14, min_total=20)
    assert _verdict(vs, "我的")["status"] == "ready"
    v5 = _verdict(vs, "群焦点过滤")
    assert v5["status"] == "insufficient"
    assert GFOCUS_EPOCH in v5["note"]   # note 必须报自己的纪元，不是全局纪元


def test_gfocus_zero_clicks_verdict_checks_scope_usage():
    # gfocus 满窗零点击：scope 也零进入 → 归因 scope 而非过滤；scope 有量 → 收主行 chip
    vs = build_verdicts(aggregate(_rows(iflt_claimed=30)),
                        now_day=GFOCUS_READY_DAY, min_days=14, min_total=20)
    v5 = _verdict(vs, "群焦点过滤")
    assert v5["status"] == "ready" and v5["confidence"] == "high"
    assert "scope 本身也零进入" in v5["recommendation"]
    vs2 = build_verdicts(aggregate(_rows(iflt_claimed=30, iflt_scope_group=40)),
                         now_day=GFOCUS_READY_DAY, min_days=14, min_total=20)
    v52 = _verdict(vs2, "群焦点过滤")
    assert v52["status"] == "ready"
    assert "收掉主行 chip" in v52["recommendation"]


def test_gfocus_row_dominant_keeps_chip():
    rows = [{"day": GFOCUS_EPOCH, "action": a, "n": n} for a, n in
            {"iflt_gfocus": 22, "iflt_gfocus_m": 3, "iflt_scope_group": 40}.items()]
    vs = build_verdicts(aggregate(rows),
                        now_day=GFOCUS_READY_DAY, min_days=14, min_total=20)
    v5 = _verdict(vs, "群焦点过滤")
    assert v5["status"] == "ready"
    assert "保留主行 chip" in v5["recommendation"]
