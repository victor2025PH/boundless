# -*- coding: utf-8 -*-
"""C 线整环 E2E（路由级）：目标 × 工作链弱联动闭环的最终验收网。

单测各环节此前已分文件覆盖（record_chain_event / runner 钩子 / goal_id 透传 /
漏斗归因分段）；本文件把**真实路由 app** 串成一条链跑通：

    种子导入 → 建目标 → 带 goal_id 挂链（目标时间线出 chain_started）
    → Runner 推进到终态（时间线出 chain_completed）→ 漏斗 attributed 计数/归因组回复率

与生产 bootstrap 同构的接线：goal 路由与 workflow 路由挂同一 app、共享同一
config_manager（workflow 路由的 _goal_chain_event 与 Runner 的
chain_event_recorder 都从它现读配置）。goals 店用 :memory: 进程单例
→ autouse fixture 每测复位防串味（与 test_goal_routes 同约定）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from src.companion.goals.service import chain_event_recorder
from src.companion.goals.store import reset_goal_store
from src.inbox.store import InboxStore
from src.inbox.workflow_runner import WorkflowRunner
from src.inbox.workflow_starter import STARTER_CHAINS
from src.web.routes.goal_routes import register_goal_routes
from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes

CONV = "telegram:acc1:peer_e2e"
REACTIVATE = "starter_reactivate_3step"


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _api_auth(request: Request):
    return None


def _build(tmp_path):
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}
    cm = SimpleNamespace(config=cfg, config_path=None)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": "", "user": "tester"}

    register_goal_routes(app, auth_dep, cm)
    register_workflow_routes(app, api_auth=_api_auth)
    store = InboxStore(tmp_path / "e2e_inbox.db")
    app.state.inbox_store = store
    app.state.config_manager = cm
    return TestClient(app), store, cm


def test_full_loop_goal_chain_attribution(tmp_path):
    client, store, cm = _build(tmp_path)

    # ① 种子导入（会话选择器/工作流页同一端点）
    r = client.post("/api/workspace/workflow-chains/seed")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(r.json()["imported"]) == len(STARTER_CHAINS)

    # ② 建目标（沉默唤回——推荐映射的原始动机样例）
    r = client.post("/api/goals", json={
        "template": "engagement_reactivate", "conversation_id": CONV})
    assert r.status_code == 200
    goal_id = r.json()["goal"]["goal_id"]

    # ③ 带 goal_id 挂链（cp-goal「挂上」按钮 / 选择器推荐入口同一路由口径）
    r = client.post(f"/api/workspace/conv/{CONV}/start-chain",
                    json={"chain_id": REACTIVATE, "goal_id": goal_id})
    assert r.status_code == 200 and r.json()["ok"] is True

    # ④ 目标时间线即刻可见 chain_started（弱联动 C2 事件回写）
    r = client.get(f"/api/goals/{goal_id}")
    assert r.status_code == 200
    events = r.json()["events"]
    started = [e for e in events if e["kind"] == "chain_started"]
    assert len(started) == 1
    assert "沉默唤回" in started[0]["detail"]

    # ⑤ Runner 推进（与 ScheduledReporter 同构接线：chain_event_recorder(cm)）
    #    唤回链 3 步（0h/48h/72h），逐窗推进；末步 task 无 contacts store →
    #    失败重试后判 failed —— 这正是真实部署里最常见的终态之一，如实走 fail 路径。
    runner = WorkflowRunner(store, goal_event_hook=chain_event_recorder(cm))
    t0 = time.time()
    runner.process_due_executions(now=t0)                    # step0 template
    runner.process_due_executions(now=t0 + 49 * 3600)        # step1 template
    runner.process_due_executions(now=t0 + 122 * 3600)       # step2 task → 失败+调度重试
    runner.process_due_executions(now=t0 + 122 * 3600 + 60)  # 重试仍失败 → failed

    r = client.get(f"/api/goals/{goal_id}")
    kinds = [e["kind"] for e in r.json()["events"]]
    assert "chain_failed" in kinds, f"实际事件: {kinds}"

    # ⑥ 干净的 completed 路径：单步链带 goal_id → 一 tick 完成 → 时间线 chain_completed
    store.upsert_workflow_chain({
        "chain_id": "e2e_one", "name": "E2E单步链",
        "steps": [{"action_type": "template", "note": "hi", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    r = client.post(f"/api/workspace/conv/{CONV}/start-chain",
                    json={"chain_id": "e2e_one", "goal_id": goal_id})
    assert r.json()["ok"] is True
    runner.process_due_executions()
    r = client.get(f"/api/goals/{goal_id}")
    kinds = [e["kind"] for e in r.json()["events"]]
    assert "chain_completed" in kinds

    # ⑦ 弱联动硬边界：事件写了一堆，目标本体状态/进度纹丝不动
    g = r.json()["goal"]
    assert g["status"] == "active"

    # ⑧ 漏斗归因：两次启动都带 goal_id → attributed=2；per-chain 计数正确
    r = client.get("/api/workspace/chain-funnel?days=14")
    assert r.status_code == 200
    d = r.json()
    assert d["total"]["started"] == 2
    assert d["total"]["attributed"] == 2
    by_id = {c["chain_id"]: c for c in d["chains"]}
    assert by_id[REACTIVATE]["attributed"] == 1
    assert by_id["e2e_one"]["attributed"] == 1


def test_loop_without_goal_stays_unattributed(tmp_path):
    """对照组：不带 goal_id 的散链启动——无目标事件、漏斗归因为 0。"""
    client, store, cm = _build(tmp_path)
    client.post("/api/workspace/workflow-chains/seed")
    r = client.post(f"/api/workspace/conv/{CONV}/start-chain",
                    json={"chain_id": REACTIVATE})
    assert r.json()["ok"] is True
    r = client.get("/api/workspace/chain-funnel?days=14")
    d = r.json()
    assert d["total"]["started"] == 1
    assert d["total"]["attributed"] == 0
    assert d["total"]["attr_reply_rate"] is None


def test_goal_events_survive_goal_pause(tmp_path):
    """暂停目标后链事件不再归因到它（find_active_goal 只认 active）——
    「挂着的链继续跑、目标暂停了」时不往暂停目标里塞事件，语义如实。"""
    client, store, cm = _build(tmp_path)
    client.post("/api/workspace/workflow-chains/seed")
    r = client.post("/api/goals", json={
        "template": "engagement_reactivate", "conversation_id": CONV})
    goal_id = r.json()["goal"]["goal_id"]
    client.post(f"/api/goals/{goal_id}/status", json={"action": "pause"})

    r = client.post(f"/api/workspace/conv/{CONV}/start-chain",
                    json={"chain_id": REACTIVATE, "goal_id": goal_id})
    assert r.json()["ok"] is True   # 启动本身不受目标状态影响
    r = client.get(f"/api/goals/{goal_id}")
    kinds = [e["kind"] for e in r.json()["events"]]
    assert "chain_started" not in kinds, "暂停目标不应收到链事件（只认 active）"
