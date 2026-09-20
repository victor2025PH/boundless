# -*- coding: utf-8 -*-
"""人设考题报告持久化（H3）门禁——全部离线，绝不真调 LLM。

覆盖：save/list/get/prune/summary、模块级软失败（建库失败 / get→None）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils import persona_quiz_store as pqs
from src.utils.persona_quiz_store import PersonaQuizStore


@pytest.fixture(autouse=True)
def _iso_store(tmp_path):
    pqs.reset()
    pqs.configure(str(tmp_path / "quiz_test.db"))
    yield
    pqs.reset()


def _sample_report(score=100, passed=5, total=5, n=5, name="美月"):
    return {
        "score": score,
        "passed": passed,
        "total": total,
        "n": n,
        "persona_name": name,
        "items": [
            {"q": "你今年多大？", "expect": ["32"], "pass": True,
             "answer": "我今年 32 岁呀"},
        ],
    }


def test_save_list_get_roundtrip():
    rid = pqs.save_report("mizuki", _sample_report(score=100))
    assert isinstance(rid, int) and rid > 0
    rows = pqs.list_reports("mizuki", limit=10)
    assert len(rows) == 1
    assert rows[0]["id"] == rid
    assert rows[0]["score"] == 100
    assert rows[0]["persona_name"] == "美月"
    assert rows[0]["items"][0]["q"] == "你今年多大？"
    one = pqs.get_report("mizuki", rid)
    assert one is not None and one["score"] == 100
    assert pqs.get_report("mizuki", 99999) is None
    assert pqs.get_report("other", rid) is None


def test_judged_count_derived_from_items():
    """「关键词判错、LLM 裁判改判对」的题数派生自 items（不占列 = 无需迁移）。
    持续走高＝判分越来越靠兜底，是先行指标，必须能被读出来。"""
    report = _sample_report(score=100, passed=3, total=3, n=3)
    report["items"] = [
        {"q": "a", "expect": ["x"], "pass": True, "answer": "x"},
        {"q": "b", "expect": ["y"], "pass": True, "answer": "同义说法", "judged": True},
        {"q": "c", "expect": ["z"], "pass": True, "answer": "换个说法", "judged": True},
    ]
    rid = pqs.save_report("mizuki", report)
    assert pqs.list_reports("mizuki")[0]["judged"] == 2
    assert pqs.get_report("mizuki", rid)["judged"] == 2
    # 旧报告（items 里没有 judged 键）→ 0，不炸
    pqs.save_report("legacy", _sample_report())
    assert pqs.list_reports("legacy")[0]["judged"] == 0


def test_list_newest_first_and_summary():
    pqs.save_report("p1", _sample_report(score=80))
    time.sleep(0.02)
    pqs.save_report("p1", _sample_report(score=100))
    rows = pqs.list_reports("p1")
    assert [r["score"] for r in rows] == [100, 80]
    summ = pqs.summary("p1")
    assert summ["count"] == 2
    assert summ["avg_score"] == pytest.approx(90.0)
    assert summ["last_score"] == 100
    assert summ["last_ts"] > 0
    assert pqs.summary("nobody") == {}


def test_prune_keeps_newest():
    store = PersonaQuizStore(":memory:")
    for i in range(5):
        store.save_report("p", _sample_report(score=i * 10))
        time.sleep(0.01)
    # save_report 已按 DEFAULT_KEEP=20 自动 prune；这里显式 keep=2
    deleted = store.prune("p", keep=2)
    assert deleted == 3
    rows = store.list_reports("p", limit=10)
    assert len(rows) == 2
    assert [r["score"] for r in rows] == [40, 30]  # 最新两份


def test_save_rejects_bad_input():
    assert pqs.save_report("", _sample_report()) is None
    assert pqs.save_report("p", None) is None  # type: ignore[arg-type]
    assert pqs.save_report("p", "oops") is None  # type: ignore[arg-type]


def test_module_soft_fail_when_store_missing(monkeypatch):
    monkeypatch.setattr(pqs, "get", lambda: None)
    assert pqs.save_report("p", _sample_report()) is None
    assert pqs.list_reports("p") == []
    assert pqs.get_report("p", 1) is None
    assert pqs.prune("p") == 0
    assert pqs.summary("p") == {}
    assert pqs.overview() == {
        "personas": [],
        "totals": {"personas": 0, "reports": 0, "avg_score": None, "last_ts": None},
    }
    assert pqs.daily_scores() == []


def test_module_soft_fail_on_exception(monkeypatch):
    class Boom:
        def save_report(self, *a, **k):
            raise RuntimeError("db down")

        def list_reports(self, *a, **k):
            raise RuntimeError("db down")

        def get_report(self, *a, **k):
            raise RuntimeError("db down")

        def prune(self, *a, **k):
            raise RuntimeError("db down")

        def summary(self, *a, **k):
            raise RuntimeError("db down")

        def overview(self, *a, **k):
            raise RuntimeError("db down")

        def daily_scores(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(pqs, "get", lambda: Boom())
    assert pqs.save_report("p", _sample_report()) is None
    assert pqs.list_reports("p") == []
    assert pqs.get_report("p", 1) is None
    assert pqs.prune("p") == 0
    assert pqs.summary("p") == {}
    assert pqs.overview()["personas"] == []
    assert pqs.overview()["totals"]["reports"] == 0
    assert pqs.daily_scores() == []


# ── I3：跨进程聚合（overview / daily_scores）────────────────────────────────

def _seed(store, persona_id, scores, *, base_ts, step=60.0, name="美月"):
    """按给定 ts 直写行（绕开 save_report 的 now，日期口径可控）。"""
    for i, sc in enumerate(scores):
        store._conn.execute(
            "INSERT INTO quiz_reports"
            " (persona_id, ts, score, passed, total, n, persona_name, items_json)"
            " VALUES (?,?,?,?,?,?,?,'[]')",
            (persona_id, base_ts + i * step, sc, 0, 10, 10, name),
        )
    store._conn.commit()


def test_overview_empty_db():
    ov = pqs.overview()
    assert ov["personas"] == []
    assert ov["totals"] == {
        "personas": 0, "reports": 0, "avg_score": None, "last_ts": None}
    assert pqs.daily_scores() == []


def test_overview_shape_sorting_and_trend():
    store = PersonaQuizStore(":memory:")
    now = time.time()
    # p_old 先考（last_ts 更早），p_new 后考 → 排序按 last_ts 降序 = p_new 在前
    _seed(store, "p_old", [60, 80], base_ts=now - 5000, name="旧人设")
    _seed(store, "p_new", [50, 70, 90], base_ts=now - 1000, name="新人设")

    ov = store.overview()
    assert [p["persona_id"] for p in ov["personas"]] == ["p_new", "p_old"]
    new = ov["personas"][0]
    assert new["persona_name"] == "新人设"
    assert new["count"] == 3
    assert new["avg_score"] == pytest.approx(70.0)
    assert new["last_score"] == 90
    assert new["last_ts"] > ov["personas"][1]["last_ts"]
    assert new["trend"] == [50, 70, 90]          # 旧 → 新
    assert ov["totals"] == {
        "personas": 2, "reports": 5,
        "avg_score": pytest.approx(70.0),        # (60+80+50+70+90)/5
        "last_ts": pytest.approx(new["last_ts"]),
    }


def test_overview_trend_capped_at_8_and_limit_personas():
    store = PersonaQuizStore(":memory:")
    now = time.time()
    _seed(store, "many", list(range(0, 120, 10)), base_ts=now - 9000)   # 12 份
    for i in range(3):
        _seed(store, f"other{i}", [50], base_ts=now - 100 + i)

    ov = store.overview()
    many = [p for p in ov["personas"] if p["persona_id"] == "many"][0]
    assert many["count"] == 12
    assert len(many["trend"]) == pqs.TREND_POINTS == 8
    assert many["trend"] == [40, 50, 60, 70, 80, 90, 100, 110]   # 最近 8 份，旧→新
    assert many["last_score"] == 110

    # limit 只截 personas 列表，totals 仍是全库口径
    ov2 = store.overview(limit_personas=2)
    assert len(ov2["personas"]) == 2
    assert ov2["totals"]["personas"] == 4
    assert ov2["totals"]["reports"] == 15


def _local_hour_today(hour: float) -> float:
    """今天本地 ``hour`` 点的时间戳——日期分桶断言不能用「此刻」（跨午夜会飘）。"""
    lt = time.localtime()
    h = int(hour)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h,
                        int(round((hour - h) * 60)), 0, 0, 0, -1))


def test_daily_scores_local_date_buckets_and_window():
    store = PersonaQuizStore(":memory:")
    now = _local_hour_today(12)
    # 今天两份（80/100 → 均分 90）、昨天一份、20 天前一份（窗口外）
    _seed(store, "p", [80, 100], base_ts=now - 300, step=10)
    _seed(store, "p", [60], base_ts=now - 86400)
    _seed(store, "p", [10], base_ts=now - 20 * 86400)

    days = store.daily_scores(days=14, now=now)
    assert [d["date"] for d in days] == sorted(d["date"] for d in days)  # 升序
    assert len(days) == 2                                    # 20 天前被窗口截掉
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    last = days[-1]
    assert last["date"] == today
    assert last["runs"] == 2
    assert last["avg_score"] == pytest.approx(90.0)
    assert days[0]["runs"] == 1 and days[0]["avg_score"] == pytest.approx(60.0)
    # days=1 → 只剩今天；非法入参回落默认窗口
    assert [d["date"] for d in store.daily_scores(days=1, now=now)] == [today]
    assert len(store.daily_scores(days="oops", now=now)) == 2  # type: ignore[arg-type]


def test_daily_scores_uses_local_date_not_utc():
    """日期口径抄 isolation_trend_store._day_str（time.localtime），
    不是 SQLite 的 date(ts,'unixepoch')（UTC，会把本地凌晨算成前一天）。"""
    store = PersonaQuizStore(":memory:")
    now = _local_hour_today(0.5)      # 本地 00:30：东八区下 UTC 日期已是前一天
    _seed(store, "p", [70], base_ts=now)
    rows = store.daily_scores(days=3, now=now)
    assert len(rows) == 1
    assert rows[0]["date"] == time.strftime("%Y-%m-%d", time.localtime(now))


def test_module_level_overview_and_daily_roundtrip():
    pqs.save_report("mizuki", _sample_report(score=100))
    pqs.save_report("mizuki", _sample_report(score=80))
    pqs.save_report("other", _sample_report(score=60, name="小薄"))
    ov = pqs.overview()
    assert ov["totals"]["personas"] == 2 and ov["totals"]["reports"] == 3
    mz = [p for p in ov["personas"] if p["persona_id"] == "mizuki"][0]
    assert mz["count"] == 2 and mz["avg_score"] == pytest.approx(90.0)
    assert len(mz["trend"]) == 2
    daily = pqs.daily_scores(days=14)
    assert len(daily) == 1 and daily[0]["runs"] == 3
