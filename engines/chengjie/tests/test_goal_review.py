"""营销目标周报 CLI + GoalStore 只读打开 门禁（P27，全部离线）。

覆盖：
- ``GoalStore.open_readonly``：能读（outcome_report）、不能写（SQLite 只读连接
  写即抛）、不建表不迁移（对活体生产库零写事务的 sanctioned 入口）；
- ``scripts.goal_review.collect_review``：库缺失诚实报 skip；正常库出
  终态聚合 + 摸底专项（填充率/采集天数/按槽分布）+ 自治档快照；
- 渲染函数不抛且关键行在场。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.companion.goals.store import GoalStore

NOW = time.time()


def _seed_db(tmp_path: Path) -> Path:
    """tmp 数据根：config/marketing_goals.db + 1 活跃摸底目标（age 已聊出）。"""
    db = tmp_path / "config" / "marketing_goals.db"
    store = GoalStore(db)
    goal = store.create_goal(
        conversation_id="telegram:default:r1", platform="telegram",
        account_id="default", chat_key="r1", template="profile_discovery",
        params={"slots": "age,occupation"}, autonomy="suggest",
        deadline_days=10, now=NOW - 2 * 86400.0)
    assert goal is not None
    store.upsert_customer_profile(
        "telegram", "r1", {"age": "28岁"}, source="auto",
        now=NOW - 1 * 86400.0)
    store.close()
    return db


def test_open_readonly_reads_but_never_writes(tmp_path):
    db = _seed_db(tmp_path)
    ro = GoalStore.open_readonly(db)
    try:
        report = ro.outcome_report(NOW - 30 * 86400.0, now=NOW)
        assert isinstance(report, dict) and "totals" in report
        assert report["active_now"] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro._conn.execute("UPDATE goals SET status='x'")
    finally:
        ro.close()


def test_open_readonly_missing_db_raises(tmp_path):
    with pytest.raises(Exception):
        GoalStore.open_readonly(tmp_path / "nope" / "marketing_goals.db")


def test_collect_review_missing_db_honest_skip(tmp_path):
    from scripts.goal_review import collect_review
    rep = collect_review(tmp_path, days=14, now=NOW)
    assert rep["ok"] is False and rep["error"] == "db_not_found"


def test_collect_review_sections_and_render(tmp_path):
    _seed_db(tmp_path)
    from scripts.goal_review import collect_review, render_review
    rep = collect_review(tmp_path, days=14, now=NOW)
    assert rep["ok"] is True
    assert rep["active_now"] == 1
    d = rep["discovery"]
    assert d["active"] == 1
    assert d["by_slot"].get("age") == 1
    assert d["avg_fill_active"] == 0.5            # age 已填 / occupation 缺
    # 槽位 ts(1 天前) - 目标 start(2 天前) = 1 天
    assert d["median_days_to_fill"] == pytest.approx(1.0, abs=0.05)
    snap = rep["active_snapshot"]
    assert snap["by_autonomy"].get("suggest") == 1
    assert snap["by_template"].get("profile_discovery") == 1
    text = render_review(rep)
    assert "摸底目标" in text and "活跃目标 1 个" in text


def test_collect_review_readonly_leaves_db_untouched(tmp_path):
    """周报跑完，库文件 mtime/大小不变（只读事务的行为学验证）。"""
    db = _seed_db(tmp_path)
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    from scripts.goal_review import collect_review
    rep = collect_review(tmp_path, days=14, now=NOW)
    assert rep["ok"] is True
    after = (db.stat().st_mtime_ns, db.stat().st_size)
    assert after == before
