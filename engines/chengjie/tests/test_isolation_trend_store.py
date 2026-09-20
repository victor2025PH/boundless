"""「账号隔离健康」按日趋势落库门禁（isolation_trend_store）。

覆盖：同日 upsert 幂等（覆盖不累加）、recent 升序+窗口、regression 三态
（上升/持平或下降/单日）、坏 snapshot 容错、单例三件套（get 不懒建）。
"""
from __future__ import annotations

import time

from src.utils.isolation_trend_store import (
    IsolationTrendStore,
    configure_isolation_trend_store,
    get_isolation_trend_store,
    reset_isolation_trend_store,
)


def _snap(scoped=5, legacy=1, bare=0, online=2, bound=2, fatex_rows=3,
          complete=1, scoped_rows=50, legacy_rows=4, bare_rows=0):
    """build_isolation_health 快照形状的最小仿制（含路由会带上的 ok 键）。"""
    return {
        "ok": True,
        "level": "ok",
        "keys": {"scoped": scoped, "legacy_platform": legacy, "bare": bare,
                 "other": 0, "scoped_rows": scoped_rows,
                 "legacy_rows": legacy_rows, "bare_rows": bare_rows},
        "legacy_ratio": 0.0,
        "personas": {"online": online, "bound": bound, "unbound": []},
        "fatex": {"birth_profiles": fatex_rows, "by_platform": {}, "complete": complete},
    }


# ── upsert 幂等（gauge 覆盖语义，非 counter 累加） ────────────────────────────

def test_upsert_today_same_day_overwrites_single_row():
    store = IsolationTrendStore(":memory:")
    now = time.time()
    assert store.upsert_today(_snap(legacy=3, bare=2), now=now) is True
    assert store.upsert_today(_snap(legacy=1, bare=0, online=4, bound=3), now=now) is True
    rows = store.recent(7, now=now)
    assert len(rows) == 1   # 同日两次 = 一行
    r = rows[-1]
    assert r["legacy_keys"] == 1 and r["bare_keys"] == 0   # 覆盖不是累加
    assert r["online_accounts"] == 4
    assert r["unbound_accounts"] == 1   # online - bound（点名列表封顶，不用它计数）
    assert r["scoped_keys"] == 5 and r["scoped_rows"] == 50
    assert r["fatex_rows"] == 3 and r["fatex_complete"] == 1


# ── recent 排序 + 窗口 ───────────────────────────────────────────────────────

def test_recent_ascending_window_and_no_zero_fill():
    store = IsolationTrendStore(":memory:")
    now = time.time()
    store.upsert_today(_snap(legacy=9), now=now - 2 * 86400)
    store.upsert_today(_snap(legacy=8), now=now - 86400)
    store.upsert_today(_snap(legacy=7), now=now)
    rows = store.recent(7, now=now)
    assert [r["legacy_keys"] for r in rows] == [9, 8, 7]   # 按日升序
    assert rows == sorted(rows, key=lambda r: r["day"])
    # 只出有数据的日（不补零假谷底）
    assert len(rows) == 3
    # 窗口外旧行不出现
    store.upsert_today(_snap(legacy=99), now=now - 30 * 86400)
    assert all(r["legacy_keys"] != 99 for r in store.recent(7, now=now))


# ── regression 三态 ──────────────────────────────────────────────────────────

def test_regression_single_day_is_false():
    store = IsolationTrendStore(":memory:")
    store.upsert_today(_snap(legacy=5), now=time.time())
    assert store.regression_signal() == {"regressed": False, "detail": ""}


def test_regression_legacy_rise_flat_and_fall():
    store = IsolationTrendStore(":memory:")
    now = time.time()
    store.upsert_today(_snap(legacy=2, bare=1, online=3, bound=3), now=now - 86400)
    # legacy+bare 3 → 5：回升
    store.upsert_today(_snap(legacy=4, bare=1, online=3, bound=3), now=now)
    sig = store.regression_signal()
    assert sig["regressed"] is True
    assert "legacy+bare 3->5" in sig["detail"]
    # 覆盖今日为持平 → False
    store.upsert_today(_snap(legacy=2, bare=1, online=3, bound=3), now=now)
    assert store.regression_signal()["regressed"] is False
    # 覆盖今日为下降 → False
    store.upsert_today(_snap(legacy=0, bare=0, online=3, bound=3), now=now)
    assert store.regression_signal()["regressed"] is False


def test_regression_unbound_rise():
    store = IsolationTrendStore(":memory:")
    now = time.time()
    store.upsert_today(_snap(online=3, bound=3), now=now - 86400)
    store.upsert_today(_snap(online=3, bound=2), now=now)
    sig = store.regression_signal()
    assert sig["regressed"] is True
    assert "unbound 0->1" in sig["detail"]


def test_regression_empty_store_is_false():
    store = IsolationTrendStore(":memory:")
    assert store.regression_signal() == {"regressed": False, "detail": ""}


# ── 坏 snapshot 容错 ─────────────────────────────────────────────────────────

def test_upsert_bad_snapshot_tolerated():
    store = IsolationTrendStore(":memory:")
    now = time.time()
    # 非 dict → 不落行、不抛
    assert store.upsert_today(None, now=now) is False
    assert store.upsert_today("garbage", now=now) is False
    assert store.recent(7, now=now) == []
    # 内层容器坏形状 → 按 0 落行（绝不抛）
    assert store.upsert_today({"keys": "oops", "personas": None, "fatex": 3}, now=now) is True
    r = store.recent(7, now=now)[-1]
    assert r["scoped_keys"] == 0 and r["legacy_keys"] == 0
    assert r["online_accounts"] == 0 and r["unbound_accounts"] == 0
    assert r["fatex_rows"] == 0
    # 计数位是垃圾值 / 负数 → 0 兜底
    snap = _snap()
    snap["keys"]["scoped"] = "x"
    snap["personas"]["online"] = -5
    assert store.upsert_today(snap, now=now) is True
    r2 = store.recent(7, now=now)[-1]
    assert r2["scoped_keys"] == 0 and r2["online_accounts"] == 0


# ── 单例三件套 ───────────────────────────────────────────────────────────────

def test_singleton_trio_no_lazy_default_path():
    reset_isolation_trend_store()
    # 未 configure → None（路由静默跳过；不做默认路径懒建防落野库文件）
    assert get_isolation_trend_store() is None
    store = configure_isolation_trend_store(":memory:")
    assert store is not None
    assert get_isolation_trend_store() is store
    # 幂等：重复 configure 返回既有实例
    assert configure_isolation_trend_store(":memory:") is store
    reset_isolation_trend_store()
    assert get_isolation_trend_store() is None


def test_file_backed_store_roundtrip(tmp_path):
    db = tmp_path / "isolation_trend.db"
    store = IsolationTrendStore(db)
    now = time.time()
    assert store.upsert_today(_snap(legacy=2), now=now)
    assert db.exists()
    # 重新打开（模拟重启）→ 数据还在
    store2 = IsolationTrendStore(db)
    rows = store2.recent(7, now=now)
    assert len(rows) == 1 and rows[-1]["legacy_keys"] == 2
