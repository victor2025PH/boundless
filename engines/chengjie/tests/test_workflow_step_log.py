# -*- coding: utf-8 -*-
"""工作链环节执行日志门禁（P1 2026-08-09）。

此前每步只留 ``executions.last_result_json``（新步覆盖旧步），「每环节转化率 /
哪一步在损耗」无数据可算。本批：runner 每次执行尝试落 ``workflow_step_log``
一行（成功/失败/重试如实分行）→ ``workflow_step_stats`` 窗口聚合 →
``chain_funnel`` 出 ``by_step``（带链定义的动作类型与文案摘要）。

覆盖：真 store 端到端两步链落账；失败行聚合口径；窗口过滤；funnel by_step
拼装（含步骤标签对齐与超界容忍）；假 store（MagicMock）零阻断。
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

from src.inbox.store import InboxStore
from src.inbox.workflow_monitor import chain_funnel
from src.inbox.workflow_runner import WorkflowRunner

NOW = time.time()


def _store_with_chain(steps):
    store = InboxStore(":memory:")
    store.upsert_workflow_chain(
        {"chain_id": "c1", "name": "测试链", "steps": steps})
    n = time.time()
    store._conn.execute(
        """INSERT INTO conversations
           (conversation_id, platform, display_name, last_ts, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        ("telegram:a1:100", "telegram", "客户A", n, n, n))
    store._conn.commit()
    return store


def test_runner_logs_each_step_end_to_end():
    store = _store_with_chain([
        {"action_type": "note", "note": "第一步备注"},
        {"action_type": "note", "note": "第二步备注"},
    ])
    eid = store.start_chain_execution(
        "c1", "telegram:a1:100", {}, schedule_first_step=True)
    runner = WorkflowRunner(store)
    runner.process_due_executions()
    ex = store.get_workflow_execution(eid)
    assert ex["status"] == "completed"
    rows = store._conn.execute(
        "SELECT step_idx, action_type, ok FROM workflow_step_log"
        " WHERE exec_id = ? ORDER BY step_idx", (eid,)).fetchall()
    assert [(int(r["step_idx"]), str(r["action_type"]), int(r["ok"]))
            for r in rows] == [(0, "note", 1), (1, "note", 1)]


def test_step_stats_aggregates_ok_and_failed_with_window():
    store = InboxStore(":memory:")
    for idx, ok in ((0, True), (0, True), (0, False), (1, True)):
        store.log_workflow_step(
            exec_id="e1", chain_id="c1", conversation_id="cv",
            step_idx=idx, action_type="note", ok=ok, now=NOW)
    # 窗口外旧行不计
    store.log_workflow_step(
        exec_id="e0", chain_id="c1", conversation_id="cv",
        step_idx=0, action_type="note", ok=True, now=NOW - 40 * 86400)
    stats = store.workflow_step_stats(NOW - 86400)
    s0 = stats["c1"]["0"]
    assert s0 == {"attempts": 3, "ok": 2, "failed": 1}
    assert stats["c1"]["1"] == {"attempts": 1, "ok": 1, "failed": 0}
    # chain_id 过滤
    assert store.workflow_step_stats(NOW - 86400, chain_id="nope") == {}


def test_chain_funnel_emits_by_step_with_labels():
    store = _store_with_chain([
        {"action_type": "note", "note": "先记一笔"},
        {"action_type": "note", "note": "再记一笔"},
    ])
    eid = store.start_chain_execution(
        "c1", "telegram:a1:100", {}, schedule_first_step=True)
    WorkflowRunner(store).process_due_executions()
    out = chain_funnel(store, days=14)
    chains = {c["chain_id"]: c for c in out["chains"]}
    steps = chains["c1"].get("by_step")
    assert steps and len(steps) == 2
    assert steps[0]["step_idx"] == 0 and steps[0]["attempts"] == 1
    assert steps[0]["ok"] == 1 and steps[0]["failed"] == 0
    assert steps[0]["action_type"] == "note" and steps[0]["note"]
    assert eid  # 执行本体也在漏斗里
    assert chains["c1"]["started"] == 1 and chains["c1"]["completed"] == 1


def test_step_labels_tolerate_definition_drift():
    """链定义被改短后，超界 idx 只出统计不出标签（不抛不丢数）。"""
    store = _store_with_chain([{"action_type": "note", "note": "唯一步"}])
    store.log_workflow_step(
        exec_id="e9", chain_id="c1", conversation_id="cv",
        step_idx=5, action_type="note", ok=True, now=time.time())
    out = chain_funnel(store, days=14)
    # 没有执行行时链不入漏斗；构造一条执行让链出现
    store.start_chain_execution("c1", "telegram:a1:100", {},
                                schedule_first_step=True)
    WorkflowRunner(store).process_due_executions()
    out = chain_funnel(store, days=14)
    chains = {c["chain_id"]: c for c in out["chains"]}
    steps = {s["step_idx"]: s for s in chains["c1"]["by_step"]}
    assert steps[5]["action_type"] == "" and steps[5]["attempts"] == 1


def test_runner_survives_store_without_log_method(monkeypatch):
    """假 store/旧 store 没有 log_workflow_step → 推进照常（try/except 兜底）。"""
    published = []
    store = MagicMock(spec_set=None)
    ex = {"exec_id": "e1", "chain_id": "c1", "conversation_id": "conv1",
          "current_step": 0, "status": "running", "context_json": "{}"}
    store.get_workflow_chain.return_value = {
        "chain_id": "c1",
        "steps_json": json.dumps([{"action_type": "template", "note": "hi"}]),
    }
    store.list_due_workflow_executions.return_value = [ex]
    store.get_workflow_execution.return_value = ex
    del store.log_workflow_step          # 模拟旧接口缺方法

    class FakeBus:
        def publish(self, t, d):
            published.append((t, d))

    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: FakeBus())
    n = WorkflowRunner(store).process_due_executions()
    assert n == 1
    store.complete_workflow_execution.assert_called_once()
