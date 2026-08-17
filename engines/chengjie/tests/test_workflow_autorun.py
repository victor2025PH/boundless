# -*- coding: utf-8 -*-
"""工作链推进常备宿主门禁（P3 2026-08-09）。

事故背景：链推进原挂 ScheduledReporter（``report.enabled`` 闸、生产常年关）
＝坐席点「启动工作链」后步骤**永不推进**（产线实锤：4 条种子链 0 执行）。
现宿主＝bootstrap 常备循环 ``src/inbox/workflow_autorun.py``，配置热自闸。

覆盖：真 store 端到端到期步骤推进；三个闸门（无 store / autorun 关 /
workflows.enabled 关）；自动启动 1h 节奏；心跳快照字段（「没跑」和「没货」
从外面分得出来）；单 tick 异常不逃逸。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from src.inbox import workflow_autorun as wa
from src.inbox.store import InboxStore


def _app_state(store, cfg=None):
    return SimpleNamespace(
        inbox_store=store,
        config_manager=SimpleNamespace(config=cfg or {}, config_path=None),
        contacts=None,
    )


def _store_with_due_chain():
    store = InboxStore(":memory:")
    store.upsert_workflow_chain({
        "chain_id": "c1", "name": "测试链",
        "steps": [{"action_type": "note", "note": "一"},
                  {"action_type": "note", "note": "二"}],
    })
    n = time.time()
    store._conn.execute(
        """INSERT INTO conversations
           (conversation_id, platform, display_name, last_ts, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        ("telegram:a1:100", "telegram", "客户A", n, n, n))
    store._conn.commit()
    store.start_chain_execution(
        "c1", "telegram:a1:100", {}, schedule_first_step=True)
    return store


def test_tick_advances_due_execution_end_to_end():
    store = _store_with_due_chain()
    state: dict = {}
    wa.workflow_tick(state, _app_state(store))
    ex = store.list_chain_executions(status="completed")
    assert len(ex) == 1                      # 零延迟两步同 tick 走完
    # process_due_executions 口径=处理的执行条数（非步数）
    assert state["gated"] == "" and state["processed_total"] == 1
    assert state["last_step_ts"] > 0 and state["ticks"] == 1
    # 步级痕迹由 P1 的环节日志表如实记两行
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM workflow_step_log").fetchone()
    assert int(rows[0]) == 2


def test_gates():
    state: dict = {}
    wa.workflow_tick(state, SimpleNamespace(
        inbox_store=None, config_manager=None, contacts=None))
    assert state["gated"] == "no_store"

    store = InboxStore(":memory:")
    st2: dict = {}
    wa.workflow_tick(st2, _app_state(
        store, {"inbox": {"workflows": {"autorun": False}}}))
    assert st2["gated"] == "autorun_disabled"
    wa.workflow_tick(st2, _app_state(
        store, {"inbox": {"workflows": {"enabled": False}}}))
    assert st2["gated"] == "autorun_disabled"
    # 默认全开（修复既有断线，非新增行为）
    assert wa.autorun_enabled({}) and wa.autorun_enabled(None)


def test_auto_start_runs_on_hourly_cadence(monkeypatch):
    from src.inbox.workflow_runner import WorkflowRunner
    calls = {"auto": 0}
    monkeypatch.setattr(WorkflowRunner, "process_due_executions",
                        lambda self: 0)
    # P2 起 auto_start_chains 带 max_per_day 预算 kwarg（tick 从配置透传，缺省 30）
    monkeypatch.setattr(
        WorkflowRunner, "auto_start_chains",
        lambda self, **kw: calls.__setitem__("auto", calls["auto"] + 1) or 2)
    store = InboxStore(":memory:")
    state: dict = {"auto_start_tick": wa.AUTO_START_EVERY_TICKS - 2}
    wa.workflow_tick(state, _app_state(store))      # 第 59 tick：不触发
    assert calls["auto"] == 0
    wa.workflow_tick(state, _app_state(store))      # 第 60 tick：触发
    assert calls["auto"] == 1
    assert state["auto_started_total"] == 2
    assert state["auto_start_tick"] == 0            # 计数复位


def test_tick_never_raises(monkeypatch):
    from src.inbox.workflow_runner import WorkflowRunner
    monkeypatch.setattr(
        WorkflowRunner, "process_due_executions",
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    store = InboxStore(":memory:")
    state: dict = {}
    wa.workflow_tick(state, _app_state(store))      # 不抛即过
    assert state["ticks"] == 1
