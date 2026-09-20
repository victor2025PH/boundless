# -*- coding: utf-8 -*-
"""迟到订单对账门禁（P6 2026-08-09）。

事故背景：acquire 目标 10 天到期批量记「失守」，而官网成交发生在站外——
晚到的订单此前只记一行 ``matched=False`` 日志，**赢单被永久记成流失**
（P5 三分法读数坐实这是系统性空洞）。修法：订单 ref 匹配不上活跃目标时，
近窗（默认 14 天）内 expired/failed 的同会话目标按硬事实复活为 done。

覆盖：复活主路（状态/result/事件痕迹/late 标记）；cancelled 不复活（人的
明示决定不越权）；超窗不复活；0=关；账号锁在迟到路同样生效；幂等（同
order_id 二回 dup）；活跃目标优先于迟到路；复活后被完成通知扫描器自然拾取
（零新接线的喜报闭环）。
"""

from __future__ import annotations

import time

import pytest

from src.companion.goals import notify as goal_notify
from src.companion.goals import service as goal_service
from src.companion.goals.store import GoalStore, reset_goal_store

NOW = time.time()
DAY = 86400.0
CFG = {"companion": {"goals": {"enabled": True,
                               "order_late_settle_days": 14}}}


@pytest.fixture(autouse=True)
def _reset():
    reset_goal_store()
    yield
    reset_goal_store()


def _mk_store() -> GoalStore:
    return GoalStore(":memory:")


def _terminal_goal(store, *, status="expired", done_ago_days=3.0,
                   account_id="a1", chat_key="100"):
    g = store.create_goal(
        conversation_id=f"telegram:{account_id}:{chat_key}",
        platform="telegram", account_id=account_id, chat_key=chat_key,
        template="acquire_and_convert")
    store.update_goal_fields(
        g["goal_id"], status=status, done_at=NOW - done_ago_days * DAY,
        result="deadline")
    return store.get_goal(g["goal_id"])


def test_late_order_resurrects_expired_goal():
    store = _mk_store()
    g = _terminal_goal(store, done_ago_days=3)
    out = goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="ORD-1", plan="pro",
        now=NOW, cfg_root=CFG)
    assert out["matched"] and out["updated"] and out["late"] is True
    row = store.get_goal(g["goal_id"])
    assert row["status"] == "done"
    assert row["result"].startswith("order:pro:ORD-1")
    assert row["done_at"] == pytest.approx(NOW, abs=1)   # 赢在订单时刻
    kinds = [e["kind"] for e in store.list_events(g["goal_id"])]
    assert "order" in kinds
    details = [e["detail"] for e in store.list_events(g["goal_id"])]
    assert any("->done:late_order" in d for d in details)  # 复活痕迹进台账


def test_cancelled_goal_never_resurrected():
    """运营手动叫停是人的明示决定——订单也不越权复活。"""
    store = _mk_store()
    _terminal_goal(store, status="cancelled", done_ago_days=2)
    out = goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="ORD-2", now=NOW,
        cfg_root=CFG)
    assert out["matched"] is False


def test_out_of_window_and_disabled():
    store = _mk_store()
    _terminal_goal(store, done_ago_days=20)               # 超 14 天窗
    out = goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="O", now=NOW, cfg_root=CFG)
    assert out["matched"] is False
    # 窗内但显式关闭（0=关）
    _terminal_goal(store, done_ago_days=1, chat_key="200")
    off = {"companion": {"goals": {"order_late_settle_days": 0}}}
    out2 = goal_service.settle_order_ref(
        store, ref="telegram:a1:200", order_id="O2", now=NOW, cfg_root=off)
    assert out2["matched"] is False


def test_account_lock_applies_to_late_path():
    """迟到路同样锁账号：B 账号的单不复活 A 账号的到期目标（chat_key 撞车面）。"""
    store = _mk_store()
    _terminal_goal(store, account_id="8244899900", chat_key="me",
                   done_ago_days=2)
    out = goal_service.settle_order_ref(
        store, ref="telegram:8041810715:me", order_id="O-X", now=NOW,
        cfg_root=CFG)
    assert out["matched"] is False


def test_late_settle_idempotent_on_order_id():
    store = _mk_store()
    _terminal_goal(store, done_ago_days=3)
    out1 = goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="ORD-9", now=NOW,
        cfg_root=CFG)
    assert out1["updated"] is True
    # 同单重放：目标已 done，走「近窗终态」找不到（done 不在迟到口），
    # 活跃也没有 → matched=False，绝不重复结算/重复起留存环
    out2 = goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="ORD-9", now=NOW + 60,
        cfg_root=CFG)
    assert out2["updated"] is False


def test_active_goal_preferred_over_late():
    store = _mk_store()
    stale = _terminal_goal(store, done_ago_days=2)
    fresh = store.create_goal(
        conversation_id="telegram:a1:100", platform="telegram",
        account_id="a1", chat_key="100", template="conversion_unlock")
    out = goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="O-A", now=NOW, cfg_root=CFG)
    assert out["late"] is False
    assert out["goal_id"] == fresh["goal_id"]
    assert store.get_goal(stale["goal_id"])["status"] == "expired"  # 不动


def test_late_settled_goal_flows_into_completion_notify():
    """复活即喜报：done + 无 completed_notified 标记 → 既有扫描器自然拾取，
    零新接线（miss_notified 历史标记不阻塞完成通知）。"""
    store = _mk_store()
    g = _terminal_goal(store, done_ago_days=3)
    store.add_event(g["goal_id"], goal_notify.MISS_EVENT_KIND, "{}")
    goal_service.settle_order_ref(
        store, ref="telegram:a1:100", order_id="ORD-N", now=NOW,
        cfg_root=CFG)
    rows = store.list_done_unnotified(since_ts=NOW - 3600, limit=10)
    assert [r["goal_id"] for r in rows] == [g["goal_id"]]
