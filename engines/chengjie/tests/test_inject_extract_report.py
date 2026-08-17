# -*- coding: utf-8 -*-
"""注入抽取率校准读数 CLI 门禁：判词/聚合/ETA 纯函数 + 端到端（seeded :memory: store）。

覆盖三类判词（样本不足带 ETA / 可升级到比率阈值 / 维持归零判）+ 直方图聚合 +
DB 不存在时不创建空库。只读工具，绝不改告警行为。
"""
from __future__ import annotations

import time

from src.web.inject_extract_trend import InjectExtractTrendStore
from tools.inject_extract_report import build_report, _report_for_root, render_text


def _seed_ready_clean(store, platform, n, now):
    # 近乎全装饰（b4）→ ready，可升级到高阈值
    for _ in range(n):
        store.add_sample(platform=platform, account_id="a1",
                         decorated=10, unresolved=0, now=now)


def _seed_low_decoration(store, platform, n, now):
    # 一半归零 → 天然低装饰 → ready 但维持归零判（升阈会误报）
    for i in range(n):
        if i % 2 == 0:
            store.add_sample(platform=platform, account_id="a1",
                             decorated=0, unresolved=8, now=now)
        else:
            store.add_sample(platform=platform, account_id="a1",
                             decorated=9, unresolved=1, now=now)


def test_verdict_insufficient_with_eta():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    # 只 50 条,门槛 300 → 样本不足;单日 → ndays=1,可算 ETA
    _seed_ready_clean(store, "telegram", 50, now)
    rows = store.daily(days=14, now=now)
    rep = build_report(rows, days=14, min_samples=300)
    p = rep["platforms"][0]
    assert p["platform"] == "telegram"
    assert p["status"] == "insufficient"
    assert p["threshold"] == 0.0            # 不足 → 维持归零判(0)
    assert p["eta_days"] is not None and p["eta_days"] > 0
    assert "样本不足 50/300" in p["verdict"]


def test_verdict_ready_upgradeable():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    _seed_ready_clean(store, "telegram", 400, now)   # 干净分布,过门槛
    rows = store.daily(days=14, now=now)
    rep = build_report(rows, days=14, min_samples=300, max_fp=0.05)
    p = rep["platforms"][0]
    assert p["status"] == "ready"
    assert p["threshold"] == 0.7             # 几乎全 b4,可升到最高候选
    assert "[可升级]" in p["verdict"]
    # 直方图应几乎全在 b4(=1)
    assert p["buckets"][4] >= 390


def test_verdict_ready_but_keep_zero_when_low_decoration():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    _seed_low_decoration(store, "whatsapp", 400, now)  # 50% 归零 → 天然低装饰
    rows = store.daily(days=14, now=now)
    rep = build_report(rows, days=14, min_samples=300)
    p = rep["platforms"][0]
    assert p["status"] == "ready"
    assert p["threshold"] == 0.0             # 升阈会误报 → 维持归零判
    assert "[维持归零判]" in p["verdict"]
    assert p["zero_rate"] > 0.4


def test_empty_rows_render_and_no_platforms():
    rep = build_report([], days=14, min_samples=300)
    assert rep["platforms"] == []
    txt = render_text(rep, root="/tmp/x")
    assert "无样本" in txt


def test_multi_platform_sorted_and_ingest_zero_rate():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    _seed_ready_clean(store, "telegram", 300, now)
    # whatsapp 只有回流侧、且全归零
    for _ in range(10):
        store.add_sample(platform="whatsapp", account_id="w1",
                         ingest_tried=5, ingest_keyed=0, now=now)
    rows = store.daily(days=14, now=now)
    rep = build_report(rows, days=14, min_samples=300)
    names = [p["platform"] for p in rep["platforms"]]
    assert names == sorted(names)                       # 平台名字典序
    wa = next(p for p in rep["platforms"] if p["platform"] == "whatsapp")
    assert wa["ingest_zero_rate"] == 1.0                # 10/10 归零


def test_report_for_root_missing_db_returns_none(tmp_path):
    # DB 不存在 → None,且不得创建空库（只读工具不污染）
    assert _report_for_root(tmp_path, days=14, min_samples=300) is None
    assert not (tmp_path / "config" / "inject_extract_trend.db").exists()


def test_report_for_root_reads_existing_db(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    db = cfg / "inject_extract_trend.db"
    store = InjectExtractTrendStore(str(db))
    now = time.time()
    for _ in range(320):
        store.add_sample(platform="telegram", account_id="a1",
                         decorated=10, unresolved=0, now=now)
    rep = _report_for_root(tmp_path, days=14, min_samples=300)
    assert rep is not None
    assert rep["platforms"][0]["status"] == "ready"
