# -*- coding: utf-8 -*-
"""#168 SOP 工作链「执行失败」解释层：重试带原因 / last_error / 卡三态 / 开链限流。"""

from __future__ import annotations

import json
import logging
import time

from src.inbox.store import InboxStore
from src.inbox.workflow_autorun import resolve_auto_start_cfg
from src.inbox.workflow_monitor import (
    enrich_execution,
    explain_execution_card,
    human_wait,
)
from src.inbox.workflow_runner import WorkflowRunner, format_step_fail_reason


CONV = "whatsapp:17345893506:13308422244"


def _store_with_task_chain():
    store = InboxStore(":memory:")
    store.upsert_workflow_chain({
        "chain_id": "sop_task", "name": "跟进 SOP",
        "steps": [{"action_type": "task", "note": "创建任务", "delay_hours": 0}],
        "enabled": True,
    })
    n = time.time()
    store._conn.execute(
        """INSERT INTO conversations
           (conversation_id, platform, display_name, last_ts, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (CONV, "whatsapp", "BABY BEAR", n, n, n))
    store._conn.commit()
    return store


def test_format_step_fail_reason_joins_error_and_reason():
    assert format_step_fail_reason(
        {"error": "no_contacts_store"}, "step_failed"
    ) == "step_failed:no_contacts_store"
    assert format_step_fail_reason({"detail": "deferred_quiet"}) == "deferred_quiet"
    assert format_step_fail_reason(None, "chain_not_found") == "chain_not_found"


def test_retry_logs_warning_with_reason(caplog):
    store = _store_with_task_chain()
    eid = store.start_chain_execution(
        "sop_task", CONV, {}, schedule_first_step=True)
    runner = WorkflowRunner(store, contacts_store=None)
    with caplog.at_level(logging.WARNING, logger="src.inbox.workflow_runner"):
        runner.process_due_executions()
    warn = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("步骤重试" in m and "no_contacts_store" in m for m in warn)
    ex = store.get_workflow_execution(eid)
    last = json.loads(ex.get("last_result_json") or "{}")
    ctx = json.loads(ex.get("context_json") or "{}")
    assert "no_contacts_store" in str(last.get("last_error") or "")
    assert "no_contacts_store" in str(ctx.get("last_error") or "")
    assert last.get("retry") == 1


def test_fail_after_retry_writes_last_error_and_warns(caplog):
    store = _store_with_task_chain()
    eid = store.start_chain_execution(
        "sop_task", CONV, {}, schedule_first_step=True)
    runner = WorkflowRunner(store, contacts_store=None)
    runner.process_due_executions()
    ex = store.get_workflow_execution(eid)
    store.update_workflow_execution(
        eid, current_step=0, next_step_at=1,
        last_result=json.loads(ex.get("last_result_json") or "{}"),
        context_json=json.loads(ex.get("context_json") or "{}"))
    with caplog.at_level(logging.WARNING, logger="src.inbox.workflow_runner"):
        runner.process_due_executions()
    ex2 = store.get_workflow_execution(eid)
    assert ex2["status"] == "failed"
    last = json.loads(ex2.get("last_result_json") or "{}")
    ctx = json.loads(ex2.get("context_json") or "{}")
    assert "no_contacts_store" in str(last.get("last_error") or last.get("error") or "")
    assert ctx.get("last_error")
    warn = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("步骤失败" in m and "no_contacts_store" in m for m in warn)


def test_card_three_states():
    running = explain_execution_card(
        status="running", countdown_sec=9 * 3600 + 56 * 60,
        step_name="创建任务")
    assert running["card_state"] == "running"
    assert "运行中" in running["card_label"]
    assert "9 小时 56 分" in running["card_label"]
    assert "创建任务" in running["card_label"]

    yielded = explain_execution_card(
        status="paused", countdown_sec=30 * 60,
        step_name="创建任务",
        context={"reply_yield_ack_ts": time.mktime((2026, 9, 4, 22, 56, 28, 0, 0, -1))},
    )
    assert yielded["card_state"] == "yielded"
    assert yielded["card_label"].startswith("已让路暂停")
    assert "22:56" in yielded["card_label"]
    assert "静默 30 分钟后续跑" in yielded["card_label"]

    failed = explain_execution_card(
        status="failed", last_result={"last_error": "no_contacts_store"})
    assert failed["card_state"] == "failed"
    assert failed["card_label"] == "失败（任务步未接线）"


def test_enrich_execution_exposes_card_fields():
    row = {
        "exec_id": "e1", "chain_id": "c1", "chain_name": "跟进 SOP",
        "conversation_id": CONV, "status": "running",
        "current_step": 0, "started_at": 100, "updated_at": 200,
        "next_step_at": 400 + 10 * 3600,
        "steps_json": json.dumps([
            {"action_type": "task", "note": "创建任务", "delay_hours": 10},
        ]),
        "last_result_json": "{}",
        "context_json": "{}",
    }
    r = enrich_execution(row, now=400)
    assert r["card_state"] == "running"
    assert "运行中" in r["card_label"] and "创建任务" in r["card_label"]
    assert human_wait(10 * 3600) == "10 小时"


def test_auto_start_max_per_tick_caps(monkeypatch, caplog):
    store = InboxStore(":memory:")
    store.upsert_workflow_chain({
        "chain_id": "trig", "name": "沉默唤回",
        "steps": [{"action_type": "note", "note": "hi"}],
        "trigger_conditions": {"silence_days": 3},
        "enabled": True,
    })
    runner = WorkflowRunner(store)
    monkeypatch.setattr(
        runner, "_find_chain_candidates",
        lambda *a, **k: [f"t:a:c{i}" for i in range(8)])
    with caplog.at_level(logging.INFO, logger="src.inbox.workflow_runner"):
        n = runner.auto_start_chains(max_per_tick=5)
    assert n == 5
    infos = [r.getMessage() for r in caplog.records]
    assert sum(1 for m in infos if "自动开链 chain=" in m and "silence_days=3" in m) == 5
    assert any("本轮上限（5）" in m for m in infos)


def test_autorun_cfg_defaults_max_per_tick_5():
    assert resolve_auto_start_cfg({})["max_per_tick"] == 5
    assert resolve_auto_start_cfg(None)["max_per_day"] == 30
    cfg = resolve_auto_start_cfg(
        {"inbox": {"workflows": {"auto_start": {"max_per_tick": 2, "max_per_day": 10}}}})
    assert cfg == {"max_per_day": 10, "max_per_tick": 2}
