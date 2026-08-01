# -*- coding: utf-8 -*-
"""C 批次门禁：工作链 × 目标弱联动（链生命周期回写目标事件台账）。

不变量：
- 只写台账不改目标（状态/进度/拍零触碰）；
- goals 未启用 / 无活跃目标 / 空会话 → False 静默，且未启用时**不得触库**
  （get_configured_store 首调定路径，误触会把进程单例定成错误路径）;
- conversation_id 精确命中优先，回落 platform+account+chat_key 三元组；
- WorkflowRunner 钩子：完成/失败/自动启动各回写一次，钩子异常绝不影响链主流程。
"""

import time

import pytest

from src.companion.goals import service as goal_svc
from src.companion.goals.store import GoalStore
from src.inbox.store import InboxStore
from src.inbox.workflow_runner import WorkflowRunner
from src.inbox.workflow_starter import STARTER_CHAINS, ensure_starter_chains

_CFG_ON = {"companion": {"goals": {"enabled": True}}}
_CFG_OFF = {"companion": {"goals": {"enabled": False}}}
_CONV = "telegram:acc1:peer9"


@pytest.fixture()
def gstore(monkeypatch):
    """本地 :memory: GoalStore，经 monkeypatch 替掉 get_configured_store——
    绝不触碰进程单例（get_goal_store 首调定路径，测试污染=生产数据进内存）。"""
    store = GoalStore(":memory:")
    monkeypatch.setattr(goal_svc, "get_configured_store", lambda *_a, **_k: store)
    return store


def _mk_goal(store, **kw):
    args = dict(conversation_id=_CONV, platform="telegram", account_id="acc1",
                chat_key="peer9", template="engagement_reactivate")
    args.update(kw)
    goal = store.create_goal(**args)
    assert goal, "建目标失败"
    return goal


# ── record_chain_event ───────────────────────────────────────────────────────

def test_disabled_never_touches_store(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("goals 未启用时不得触库")
    monkeypatch.setattr(goal_svc, "get_configured_store", _boom)
    assert goal_svc.record_chain_event(_CFG_OFF, None, _CONV, "chain_started", "x") is False


def test_no_active_goal_returns_false(gstore):
    assert goal_svc.record_chain_event(_CFG_ON, None, _CONV, "chain_started", "x") is False


def test_empty_conversation_returns_false(gstore):
    _mk_goal(gstore)
    assert goal_svc.record_chain_event(_CFG_ON, None, "", "chain_started", "x") is False


def test_hit_writes_event_and_nothing_else(gstore):
    goal = _mk_goal(gstore)
    ok = goal_svc.record_chain_event(_CFG_ON, None, _CONV, "chain_completed", "沉默唤回 · 3步")
    assert ok is True
    events = gstore.list_events(goal["goal_id"])
    hit = [e for e in events if e["kind"] == "chain_completed"]
    assert len(hit) == 1
    assert hit[0]["detail"] == "沉默唤回 · 3步"
    # 只写台账不改目标
    after = gstore.get_goal(goal["goal_id"])
    assert after["status"] == "active"
    assert after.get("milestone_idx", 0) == goal.get("milestone_idx", 0)


def test_fallback_triple_match(gstore):
    """目标行 conversation_id 为空（A 线建的）时，按 platform+account+chat_key 命中。"""
    goal = _mk_goal(gstore, conversation_id="")
    ok = goal_svc.record_chain_event(_CFG_ON, None, _CONV, "chain_failed", "x")
    assert ok is True
    assert any(e["kind"] == "chain_failed" for e in gstore.list_events(goal["goal_id"]))


def test_terminal_goal_not_matched(gstore):
    goal = _mk_goal(gstore)
    gstore.update_goal_fields(goal["goal_id"], status="cancelled")
    assert goal_svc.record_chain_event(_CFG_ON, None, _CONV, "chain_started", "x") is False


def test_recorder_wrapper_reads_config_manager(gstore):
    class _CM:
        config = _CFG_ON
        config_path = None
    _mk_goal(gstore)
    hook = goal_svc.chain_event_recorder(_CM())
    assert hook(_CONV, "chain_started", "破冰") is True


# ── WorkflowRunner 钩子 ──────────────────────────────────────────────────────

def _runner_env(tmp_path, hook):
    store = InboxStore(tmp_path / "wf_link.db")
    ensure_starter_chains(store)
    return store, WorkflowRunner(store, goal_event_hook=hook)


def test_runner_records_completed(tmp_path):
    calls = []
    store, runner = _runner_env(tmp_path, lambda c, k, d: calls.append((c, k, d)))
    # 单步零延迟链 → 一个 tick 内完成
    store.upsert_workflow_chain({
        "chain_id": "one_step", "name": "单步链",
        "steps": [{"action_type": "template", "note": "hi", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    store.start_chain_execution("one_step", _CONV, {}, schedule_first_step=True)
    runner.process_due_executions()
    assert (_CONV, "chain_completed", "单步链") in calls


def test_runner_records_failed_after_retry(tmp_path):
    calls = []
    store, runner = _runner_env(tmp_path, lambda c, k, d: calls.append((c, k, d)))
    # task 步骤且无 contacts store → 步骤失败；重试 1 次后判 failed
    store.upsert_workflow_chain({
        "chain_id": "fail_chain", "name": "会失败的链",
        "steps": [{"action_type": "task", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    store.start_chain_execution("fail_chain", _CONV, {}, schedule_first_step=True)
    t0 = time.time()
    runner.process_due_executions(now=t0)          # 首次失败 → 调度重试(+30s)
    runner.process_due_executions(now=t0 + 31)     # 重试仍失败 → failed
    assert (_CONV, "chain_failed", "会失败的链") in calls


def test_runner_hook_exception_never_breaks_chain(tmp_path):
    def _boom(*_a):
        raise RuntimeError("hook 崩了")
    store, runner = _runner_env(tmp_path, _boom)
    store.upsert_workflow_chain({
        "chain_id": "one_step2", "name": "单步链2",
        "steps": [{"action_type": "template", "note": "hi", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    eid = store.start_chain_execution("one_step2", _CONV, {}, schedule_first_step=True)
    runner.process_due_executions()   # 不抛
    assert store.get_workflow_execution(eid)["status"] == "completed"


def test_runner_no_hook_is_noop(tmp_path):
    store = InboxStore(tmp_path / "wf_nohook.db")
    store.upsert_workflow_chain({
        "chain_id": "one_step3", "name": "单步链3",
        "steps": [{"action_type": "template", "note": "hi", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    eid = store.start_chain_execution("one_step3", _CONV, {}, schedule_first_step=True)
    WorkflowRunner(store).process_due_executions()
    assert store.get_workflow_execution(eid)["status"] == "completed"


def test_auto_start_records_started(tmp_path, monkeypatch):
    calls = []
    store, runner = _runner_env(tmp_path, lambda c, k, d: calls.append((c, k, d)))
    store.upsert_workflow_chain({
        "chain_id": "auto_chain", "name": "自动链",
        "steps": [{"action_type": "template", "note": "hi", "delay_hours": 0}],
        "trigger_conditions": {"silence_days": 3},
    })
    monkeypatch.setattr(
        WorkflowRunner, "_find_chain_candidates", lambda self, *_a, **_k: [_CONV])
    started = runner.auto_start_chains()
    assert started >= 1
    assert (_CONV, "chain_started", "自动链") in calls
