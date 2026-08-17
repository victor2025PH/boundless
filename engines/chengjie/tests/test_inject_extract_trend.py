"""注入抽取率按日落库（阈值校准数据面）门禁。

覆盖：装饰率分桶纯函数边界 / 聚合与直方图 / 无信号不落行 / 回流归零计数 /
(日,平台) 分组读数与派生率 / prune / 默认关闸门 / record 归一防御 / 静态接线三闸
（路由旁路、lifecycle 装配、main 初始化方法）——store 写好但没人调用是这类
观测地基最常见的静默死法。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from src.web.inject_extract_trend import (
    InjectExtractTrendStore,
    configure_inject_extract_trend,
    get_inject_extract_trend_store,
    ratio_bucket,
    record_inject_extract_trend,
    reset_inject_extract_trend,
    suggest_extract_threshold,
    suggest_extract_thresholds,
)


def _agg(reports, b0=0, b1=0, b2=0, b3=0, b4=0, platform="telegram"):
    return {"platform": platform, "reports": reports, "ratio_b0": b0,
            "ratio_b1": b1, "ratio_b2": b2, "ratio_b3": b3, "ratio_b4": b4}

_REPO = Path(__file__).resolve().parents[1]


# ─────────────── ratio_bucket 纯函数边界 ───────────────

def test_ratio_bucket_no_attempt_is_none():
    assert ratio_bucket(0, 0) is None
    assert ratio_bucket(-3, 0) is None  # 负数当 0 夹紧


def test_ratio_bucket_edges():
    assert ratio_bucket(0, 5) == 0          # 全失败 = 归零判命中区
    assert ratio_bucket(3, 7) == 1          # 0.3 → 边界含在低桶
    assert ratio_bucket(31, 69) == 2        # 0.31
    assert ratio_bucket(7, 3) == 2          # 0.7 → 边界含在中桶
    assert ratio_bucket(71, 29) == 3        # 0.71
    assert ratio_bucket(99, 1) == 3         # <1.0 仍是 b3
    assert ratio_bucket(5, 0) == 4          # 全成功


# ─────────────── add_sample 聚合语义 ───────────────

def test_add_sample_aggregates_and_buckets():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    assert store.add_sample(platform="telegram", account_id="a1",
                            decorated=10, unresolved=0, now=now)   # b4
    assert store.add_sample(platform="telegram", account_id="a1",
                            decorated=0, unresolved=8, now=now)    # b0
    assert store.add_sample(platform="telegram", account_id="a1",
                            decorated=5, unresolved=5, now=now)    # b2
    rows = store.daily(days=1, now=now)
    assert len(rows) == 1
    r = rows[0]
    assert r["platform"] == "telegram" and r["accounts"] == 1
    assert r["reports"] == 3
    assert r["attempted_sum"] == 28 and r["decorated_sum"] == 15
    assert r["ratio_b4"] == 1 and r["ratio_b0"] == 1 and r["ratio_b2"] == 1
    assert r["decorated_rate"] == round(15 / 28, 4)
    assert r["zero_rate"] == round(1 / 3, 4)


def test_add_sample_skips_no_signal_and_keyless():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    # 两侧都没尝试（空会话）→ 不落行，校准数据集保持干净
    assert store.add_sample(platform="telegram", account_id="a1",
                            decorated=0, unresolved=0,
                            ingest_tried=0, ingest_keyed=0, now=now) is False
    # 无主键 → 不落行
    assert store.add_sample(platform="", account_id="a1",
                            decorated=3, now=now) is False
    assert store.add_sample(platform="telegram", account_id="",
                            decorated=3, now=now) is False
    assert store.daily(days=1, now=now) == []


def test_ingest_side_counts_zero_and_ok():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    # 只有回流信号（正文侧没尝试）也计样本
    assert store.add_sample(platform="whatsapp", account_id="w1",
                            ingest_tried=6, ingest_keyed=0, now=now)
    assert store.add_sample(platform="whatsapp", account_id="w1",
                            ingest_tried=4, ingest_keyed=4, now=now)
    r = store.daily(days=1, now=now)[0]
    assert r["reports"] == 0                       # 正文侧无样本
    assert r["ingest_reports"] == 2
    assert r["ingest_tried_sum"] == 10 and r["ingest_keyed_sum"] == 4
    assert r["ingest_zero"] == 1
    assert r["ingest_zero_rate"] == 0.5


def test_daily_groups_by_platform_and_counts_accounts():
    store = InjectExtractTrendStore(":memory:")
    now = time.time()
    store.add_sample(platform="telegram", account_id="a1", decorated=9,
                     unresolved=1, now=now)
    store.add_sample(platform="telegram", account_id="a2", decorated=0,
                     unresolved=5, now=now)
    store.add_sample(platform="whatsapp", account_id="w1", decorated=4,
                     now=now)
    rows = store.daily(days=1, now=now)
    assert [(r["platform"], r["accounts"], r["reports"]) for r in rows] == [
        ("telegram", 2, 2), ("whatsapp", 1, 1)]
    tg = rows[0]
    # 单个坏账号（a2 全挂）体现在直方图与账号数里，而不是被平台均值抹掉
    assert tg["ratio_b0"] == 1 and tg["ratio_b3"] == 1


def test_prune_uses_instance_retention_default():
    store = InjectExtractTrendStore(":memory:", retention_days=30)
    now = time.time()
    store.add_sample(platform="telegram", account_id="a1", decorated=1,
                     now=now - 60 * 86400)
    store.add_sample(platform="telegram", account_id="a1", decorated=1,
                     now=now)
    assert store.prune(now=now) == 1          # 缺省用构造时的 30 天
    assert len(store.daily(days=120, now=now)) == 1


# ─────────────── 模块级闸门（默认关 = 恒 no-op）───────────────

def test_record_noop_until_configured():
    reset_inject_extract_trend()
    record_inject_extract_trend({"platform": "telegram", "account_id": "a1",
                                 "extract": {"decorated": 5}})
    assert get_inject_extract_trend_store() is None
    store = configure_inject_extract_trend(enabled=True, db_path=":memory:")
    assert store is not None
    record_inject_extract_trend({"platform": "telegram", "account_id": "a1",
                                 "extract": {"decorated": 5, "unresolved": 0}})
    assert store.daily(days=1)[0]["reports"] == 1
    reset_inject_extract_trend()


def test_configure_disabled_keeps_noop():
    reset_inject_extract_trend()
    assert configure_inject_extract_trend(enabled=False) is None
    record_inject_extract_trend({"platform": "telegram", "account_id": "a1",
                                 "extract": {"decorated": 5}})
    assert get_inject_extract_trend_store() is None


def test_record_normalizes_camelcase_extract():
    """防御性归一：即使拿到原始 camelCase 上报（而非库内规范记录）也不丢数。"""
    reset_inject_extract_trend()
    store = configure_inject_extract_trend(enabled=True, db_path=":memory:")
    record_inject_extract_trend({
        "platform": "telegram", "account_id": "a1",
        "extract": {"decorated": 3, "unresolved": 1,
                    "ingestTried": 2, "ingestKeyed": 0},
    })
    r = store.daily(days=1)[0]
    assert r["attempted_sum"] == 4 and r["ingest_tried_sum"] == 2
    assert r["ingest_zero"] == 1
    reset_inject_extract_trend()


def test_record_flows_from_inject_health_store_record():
    """端到端（进程内）：InjectHealthStore.record 的规范记录直接可喂趋势库。"""
    from src.web.desktop_inject_health import InjectHealthStore
    reset_inject_extract_trend()
    trend = configure_inject_extract_trend(enabled=True, db_path=":memory:")
    rec = InjectHealthStore().record({
        "platform": "telegram", "account_id": "a1", "supported": True,
        "composer": True, "bubbles": 12, "chatOpen": True,
        "extract": {"decorated": 12, "unresolved": 0,
                    "ingestTried": 3, "ingestKeyed": 3},
    })
    record_inject_extract_trend(rec)
    r = trend.daily(days=1)[0]
    assert r["reports"] == 1 and r["ratio_b4"] == 1
    assert r["ingest_reports"] == 1 and r["ingest_zero"] == 0
    reset_inject_extract_trend()


# ─────────────── 阈值校准建议（纯函数，不改告警行为）───────────────

def test_suggest_insufficient_below_min_samples():
    out = suggest_extract_threshold(_agg(50, b4=50), min_samples=300)
    assert out["status"] == "insufficient" and out["threshold"] == 0.0
    assert out["samples"] == 50


def test_suggest_ready_high_threshold_when_clean():
    # 500 报告几乎全在 b4（>1.0），仅 2% 落 ≤0.3 → 0.7 阈值误伤 2% ≤ 5% 预算 → 可升到 0.7
    out = suggest_extract_threshold(
        _agg(500, b0=5, b1=5, b3=10, b4=480), min_samples=300,
        max_false_positive=0.05)
    assert out["status"] == "ready" and out["threshold"] == 0.7
    assert out["frac_le_07"] <= 0.05


def test_suggest_mid_threshold_when_some_partial():
    # ≤0.3 占 3%（可升 0.3），但 ≤0.7 占 12%（超 5% 预算，不可升 0.7）→ 取 0.3
    out = suggest_extract_threshold(
        _agg(1000, b0=10, b1=20, b2=90, b3=100, b4=780), min_samples=300,
        max_false_positive=0.05)
    assert out["status"] == "ready" and out["threshold"] == 0.3


def test_suggest_stays_zero_when_naturally_low_decoration():
    # 平台天然低装饰（40% 报告 ≤0.3）→ 连 0.3 都会误伤 40% → 维持归零判 0.0
    out = suggest_extract_threshold(
        _agg(500, b0=100, b1=100, b2=100, b4=200), min_samples=300)
    assert out["status"] == "ready" and out["threshold"] == 0.0
    assert out["zero_rate"] == round(100 / 500, 4)


def test_suggest_conservative_when_currently_broken():
    # 当前正在坏（80% 在 b0）→ 拒绝升级（归零判已在告警，不因坏得多反而放松）
    out = suggest_extract_threshold(_agg(500, b0=400, b4=100), min_samples=300)
    assert out["threshold"] == 0.0 and out["zero_rate"] == 0.8


def test_suggest_thresholds_groups_by_platform_across_days():
    rows = [
        _agg(200, b4=200, platform="telegram"),
        _agg(200, b0=2, b4=198, platform="telegram"),   # tg 跨两天共 400，clean
        _agg(50, b0=25, b4=25, platform="whatsapp"),     # wa 样本不足
    ]
    out = suggest_extract_thresholds(rows, min_samples=300)
    by = {r["platform"]: r for r in out}
    assert by["telegram"]["status"] == "ready" and by["telegram"]["samples"] == 400
    assert by["telegram"]["threshold"] == 0.7
    assert by["whatsapp"]["status"] == "insufficient"  # 50 < 300
    # 排序稳定（平台名字典序）
    assert [r["platform"] for r in out] == ["telegram", "whatsapp"]


def test_suggest_thresholds_ignores_nondict_and_keyless():
    rows = [None, "x", {"reports": 999}, _agg(400, b4=400, platform="tg")]
    out = suggest_extract_thresholds(rows, min_samples=300)
    assert [r["platform"] for r in out] == ["tg"]  # 无平台名的行被跳过


# ─────────────── 静态接线闸（store 写好没人调 = 最常见静默死法）───────────────

def test_route_bypass_wired_after_record():
    src = (_REPO / "src" / "web" / "routes"
           / "unified_inbox_desktop_routes.py").read_text(encoding="utf-8")
    # POST 健康上报 handler 里：先 record 入库，再旁路趋势
    m = re.search(
        r"get_inject_health_store\(\)\.record\(body or \{\}\)"
        r"[\s\S]{0,600}?record_inject_extract_trend\(rec\)", src)
    assert m, "POST /api/desktop/inject-health 应在 record 后旁路 record_inject_extract_trend"
    assert "/api/desktop/inject-health/extract-trend" in src, "读端点缺失"
    assert "suggest_extract_thresholds(rows)" in src, \
        "extract-trend 读端点应带 calibration 建议阈值段"


def test_lifecycle_calls_init():
    src = (_REPO / "src" / "bootstrap" / "lifecycle.py").read_text(encoding="utf-8")
    assert "_maybe_init_inject_extract_trend_log()" in src, \
        "lifecycle 未装配 inject extract 趋势落库（store 永远不会被 configure）"


def test_main_init_reads_config_key():
    src = (_REPO / "main.py").read_text(encoding="utf-8")
    m = re.search(
        r"def _maybe_init_inject_extract_trend_log[\s\S]{0,900}?"
        r"desktop_inject[\s\S]{0,300}?trend_log", src)
    assert m, "main.py 初始化方法缺失或未读 inbox.desktop_inject.trend_log"
