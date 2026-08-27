# -*- coding: utf-8 -*-
"""快捷回复用量裁决 CLI 门禁（P2 2026-08-18，机制先上线、样本门槛当等待期）。

钉住的口径（与 tools/quickreply_usage_report.py 同源）：
① 聚合：cpkb_use_<key> 剥前缀进 per_key、聚合动作各归各桶、脏行/负数不计；
② 判词四态：样本积累中（带 ETA）→ 链路自证（满窗全零）→ 小样本降置信 → 可裁决；
③ 零使用键＝「键宇宙 - 有声键」（键宇宙来自实况 templates.yaml 经线上同一套准入
   谓词），不是只看有声数据；
④ read_rows 只读且库缺失抛 FileNotFoundError（绝不创建空库）。
"""

import sqlite3
import sys
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from tools.quickreply_usage_report import (  # noqa: E402
    QRPT_EPOCH_DAY,
    aggregate,
    build_verdicts,
    curated_team_keys,
    observed_days,
    read_rows,
)


def _rows(*triples):
    return [{"day": d, "action": a, "n": n} for d, a, n in triples]


# ── ① 聚合 ──────────────────────────────────────────────────────────────


def test_aggregate_buckets_and_per_key():
    agg = aggregate(_rows(
        ("2026-08-18", "cpkb_fill_tpl", 3),
        ("2026-08-19", "cpkb_fill_tpl", 2),
        ("2026-08-19", "cpkb_fill_hit", 1),
        ("2026-08-19", "cpkb_use_greeting", 4),
        ("2026-08-20", "cpkb_use_greeting", 1),
        ("2026-08-20", "cpkb_use_order_query", 2),
        ("2026-08-20", "cpkb_mine_add", 1),
        ("2026-08-20", "iflt_more_open", 9),      # 非 cpkb_* 不计
        ("2026-08-20", "cpkb_fill_tpl", -5),      # 负数不计
    ))
    assert agg["totals"]["cpkb_fill_tpl"] == 5
    assert agg["totals"]["cpkb_fill_hit"] == 1
    assert agg["totals"]["cpkb_mine_add"] == 1
    assert agg["per_key"] == {"greeting": 5, "order_query": 2}
    assert agg["days_with_data"] == 3


# ── ② 判词四态 ──────────────────────────────────────────────────────────


def test_verdict_sample_accumulating_with_eta():
    agg = aggregate(_rows((QRPT_EPOCH_DAY, "cpkb_fill_tpl", 3)))
    v = build_verdicts(agg, ["greeting"], min_days=14, today="2026-08-20")
    assert v["verdict"] == "sample_accumulating"
    assert v["observed_days"] == 3
    assert any("2026-08-31" in n for n in v["notes"])  # 纪元+13 天
    assert v["zero_keys"] == []  # 样本未满不给清退证据


def test_verdict_chain_suspect_when_all_zero_after_window():
    v = build_verdicts(aggregate([]), ["greeting"], min_days=14, today="2026-09-10")
    assert v["verdict"] == "chain_suspect"
    assert any("链路" in n for n in v["notes"])


def test_verdict_low_sample_then_ok_and_zero_keys():
    agg = aggregate(_rows(
        ("2026-08-18", "cpkb_fill_tpl", 5),
        ("2026-08-19", "cpkb_use_greeting", 5),
    ))
    v = build_verdicts(agg, ["greeting", "order_query"],
                       min_days=14, min_total=20, today="2026-09-10")
    assert v["verdict"] == "low_sample"
    assert any("别当真理" in n for n in v["notes"])
    agg2 = aggregate(_rows(
        ("2026-08-18", "cpkb_fill_tpl", 30),
        ("2026-08-19", "cpkb_use_greeting", 25),
        ("2026-08-19", "cpkb_use_price_check", 5),
    ))
    v2 = build_verdicts(agg2, ["greeting", "price_check", "order_query"],
                        min_days=14, min_total=20, today="2026-09-10")
    assert v2["verdict"] == "ok"
    assert [it["key"] for it in v2["top_keys"]] == ["greeting", "price_check"]
    assert [it["key"] for it in v2["zero_keys"]] == ["order_query"]


def test_observed_days_counts_from_epoch_inclusive():
    assert observed_days(epoch_day="2026-08-18", today="2026-08-18") == 1
    assert observed_days(epoch_day="2026-08-18", today="2026-08-31") == 14


# ── ③ 键宇宙 ────────────────────────────────────────────────────────────


def test_curated_team_keys_uses_live_curation_predicates(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "templates.yaml").write_text(
        "greeting:\n- 你好\n"
        "farewell: 再见啦\n"
        "test:\n- 自检\n"
        "gxp_expired: 单号过期\n"
        "error:\n  general: 出错了\n",
        encoding="utf-8")
    keys = curated_team_keys(tmp_path)
    assert set(keys) == {"greeting", "farewell"}


# ── ④ 只读读取 ──────────────────────────────────────────────────────────


def test_read_rows_missing_db_raises_and_never_creates(tmp_path):
    p = tmp_path / "config" / "ui_event_trend.db"
    try:
        read_rows(p, days=30)
        raise AssertionError("缺库必须抛 FileNotFoundError")
    except FileNotFoundError:
        pass
    assert not p.exists(), "只读工具绝不创建空库"


def test_read_rows_prefix_filter(tmp_path):
    p = tmp_path / "ui_event_trend.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE ui_event_trend_daily ("
                 "day TEXT NOT NULL, action TEXT NOT NULL, "
                 "n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, action))")
    day = __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime())
    conn.executemany(
        "INSERT INTO ui_event_trend_daily (day, action, n) VALUES (?, ?, ?)",
        [(day, "cpkb_fill_tpl", 3), (day, "cpkb_use_greeting", 2),
         (day, "iflt_more_open", 9), (day, "cpkbx_fake", 1)])
    conn.commit()
    conn.close()
    rows = read_rows(p, days=7)
    acts = sorted(r["action"] for r in rows)
    assert acts == ["cpkb_fill_tpl", "cpkb_use_greeting"]
