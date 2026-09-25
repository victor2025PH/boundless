# -*- coding: utf-8 -*-
"""P3 2026-08-13：建目标即自动挂推荐链 + 漏斗档位分组 门禁。

- ``maybe_auto_attach_chain`` 五重闸：配置关（默认）/ 非 auto 档 / 模板无推荐 /
  链缺失或停用 / 同链在途 → 全部 None；全过 → 启动执行且 context 带 goal_id
  （归因与手动「推荐」入口同口径）。
- 路由集成：goal 创建响应带 ``auto_attached_chain``、目标时间线出 chain_started、
  开关关时零副作用。
- ``chain_funnel`` 档位分组：remind/auto 各一桶（started/reply_rate），
  per-chain 行带 exec_mode。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import Session, SessionMiddleware
from starlette.testclient import TestClient

from src.companion.goals.store import reset_goal_store
from src.inbox.store import InboxStore
from src.inbox.workflow_starter import (
    GOAL_CHAIN_REC,
    ensure_starter_chains,
    goal_auto_attach_enabled,
    maybe_auto_attach_chain,
)

CONV = "telegram:acc1:peer_attach"
RECO_CHAIN = GOAL_CHAIN_REC["engagement_reactivate"]

CFG_ON = {"inbox": {"workflows": {"goal_auto_attach": True}}}


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


@pytest.fixture()
def store(tmp_path):
    s = InboxStore(tmp_path / "attach.db")
    ensure_starter_chains(s)
    return s


def _attach(store, cfg, **kw):
    args = {"conversation_id": CONV, "goal_id": "g1",
            "goal_template": "engagement_reactivate", "goal_autonomy": "auto"}
    args.update(kw)
    return maybe_auto_attach_chain(store, cfg, **args)


class TestHelperGates:
    def test_config_default_off(self, store):
        assert goal_auto_attach_enabled({}) is False
        assert _attach(store, {}) is None
        assert _attach(store, None) is None

    def test_non_auto_autonomy_skipped(self, store):
        assert _attach(store, CFG_ON, goal_autonomy="suggest") is None
        assert _attach(store, CFG_ON, goal_autonomy="observe") is None

    def test_template_without_reco_skipped(self, store):
        assert _attach(store, CFG_ON, goal_template="custom") is None
        assert _attach(store, CFG_ON, goal_template="relationship_intimacy") is None

    def test_missing_or_disabled_chain_skipped(self, tmp_path):
        bare = InboxStore(tmp_path / "bare.db")     # 未导种子
        assert _attach(bare, CFG_ON) is None
        ensure_starter_chains(bare)
        ch = bare.get_workflow_chain(RECO_CHAIN)
        bare.upsert_workflow_chain({**ch, "steps": json.loads(ch["steps_json"]),
                                    "enabled": False})
        assert _attach(bare, CFG_ON) is None        # 停用链不挂

    def test_workflows_module_off_skipped(self, store):
        cfg = {"inbox": {"workflows": {"enabled": False,
                                       "goal_auto_attach": True}}}
        assert _attach(store, cfg) is None

    def test_ok_attaches_with_goal_attribution(self, store):
        out = _attach(store, CFG_ON)
        assert out and out["chain_id"] == RECO_CHAIN and out["name"]
        rows = store.list_chain_executions(conversation_id=CONV)
        assert len(rows) == 1
        ctx = json.loads(rows[0]["context_json"])
        assert ctx["goal_id"] == "g1"               # 漏斗归因口径
        assert ctx["agent"] == "goal_auto_attach"
        # 同链在途 → 再建目标不重复挂
        assert _attach(store, CFG_ON, goal_id="g2") is None


# ── 路由集成：建目标 → 自动挂链 → 响应/时间线 ───────────────────────────────

def _api_auth(request: Request):
    return None


def _build(tmp_path, *, attach_on=True):
    from src.web.routes.goal_routes import register_goal_routes
    from src.web.routes.unified_inbox_workflow_routes import (
        register_workflow_routes,
    )
    cfg = {
        "companion": {"goals": {"enabled": True, "db_path": ":memory:"}},
        "inbox": {"workflows": {"goal_auto_attach": bool(attach_on)}},
    }
    cm = SimpleNamespace(config=cfg, config_path=None)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    def auth_dep(request: Request) -> None:
        request.scope["session"] = Session({"role": "", "user": "tester"})

    register_goal_routes(app, auth_dep, cm)
    register_workflow_routes(app, api_auth=_api_auth)
    store = InboxStore(tmp_path / "attach_api.db")
    ensure_starter_chains(store)
    app.state.inbox_store = store
    app.state.config_manager = cm
    return TestClient(app), store


class TestRouteIntegration:
    def test_create_goal_auto_attaches_and_reports(self, tmp_path):
        client, store = _build(tmp_path)
        r = client.post("/api/goals", json={
            "template": "engagement_reactivate", "conversation_id": CONV})
        assert r.status_code == 200
        d = r.json()
        assert d["goal"]["autonomy"] == "auto"      # 缺省档=auto → 闸 2 过
        att = d.get("auto_attached_chain")
        assert att and att["chain_id"] == RECO_CHAIN
        rows = store.list_chain_executions(conversation_id=CONV)
        assert len(rows) == 1 and rows[0]["status"] == "running"
        gid = d["goal"]["goal_id"]
        assert json.loads(rows[0]["context_json"])["goal_id"] == gid
        # 目标时间线出 chain_started（与手动 start-chain 同口径的弱联动回写）
        ev = client.get(f"/api/goals/{gid}").json()["events"]
        assert any(e["kind"] == "chain_started" for e in ev)

    def test_switch_off_zero_side_effect(self, tmp_path):
        client, store = _build(tmp_path, attach_on=False)
        r = client.post("/api/goals", json={
            "template": "engagement_reactivate", "conversation_id": CONV})
        assert r.status_code == 200
        assert "auto_attached_chain" not in r.json()
        assert store.list_chain_executions(conversation_id=CONV) == []

    def test_suggest_goal_not_attached(self, tmp_path):
        client, store = _build(tmp_path)
        r = client.post("/api/goals", json={
            "template": "engagement_reactivate", "conversation_id": CONV,
            "autonomy": "suggest"})
        assert r.status_code == 200
        assert "auto_attached_chain" not in r.json()
        assert store.list_chain_executions(conversation_id=CONV) == []


# ── 漏斗档位分组 ────────────────────────────────────────────────────────────

class TestFunnelByMode:
    def test_by_mode_buckets_and_chain_field(self, tmp_path):
        from src.inbox.workflow_monitor import chain_funnel
        store = InboxStore(tmp_path / "funnel.db")
        store.upsert_workflow_chain({
            "chain_id": "c_auto", "name": "自动链", "exec_mode": "auto",
            "steps": [{"action_type": "template", "note": "x", "delay_hours": 0}],
            "enabled": True,
        })
        store.upsert_workflow_chain({
            "chain_id": "c_remind", "name": "提醒链",
            "steps": [{"action_type": "template", "note": "x", "delay_hours": 0}],
            "enabled": True,
        })
        store.start_chain_execution("c_auto", "t:a:1", {}, schedule_first_step=False)
        store.start_chain_execution("c_auto", "t:a:2", {}, schedule_first_step=False)
        store.start_chain_execution("c_remind", "t:a:3", {}, schedule_first_step=False)
        d = chain_funnel(store, days=14)
        bm = d["by_mode"]
        assert bm["auto"]["started"] == 2
        assert bm["remind"]["started"] == 1
        modes = {c["chain_id"]: c["exec_mode"] for c in d["chains"]}
        assert modes == {"c_auto": "auto", "c_remind": "remind"}
