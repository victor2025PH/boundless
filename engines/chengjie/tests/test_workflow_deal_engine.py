# -*- coding: utf-8 -*-
"""实施92 P0-3/P0-4/P0-5 门禁：成交事件路由 + stage_enter 自动挂链 + 成交引擎预设。

不变量：
- stage_enter 触发只在 journey_enabled 时消费；同一次阶段进入只挂一次
  （终态也算挂过）；预算/在途判据与 silence 面共用；
- 漏斗营收归因：成交归「之前最近启动且在窗内」的执行，多链并行不双计；
- 预设幂等：重复开/关安全；只动 stage_enter 与空 on_reply，运营改动保留；
- 新端点随 workflows flag 一起 403。
"""

import json
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.store import InboxStore
from src.inbox.workflow_monitor import chain_funnel
from src.inbox.workflow_runner import WorkflowRunner
from src.inbox.workflow_starter import (
    STAGE_CHAIN_REC,
    STARTER_CHAINS,
    apply_deal_engine,
    deal_engine_status,
    ensure_starter_chains,
)
from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes


def _api_auth(request: Request):
    return None


def _store(tmp_path):
    return InboxStore(tmp_path / "inbox.db")


def _client(tmp_path, cfg=None):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_workflow_routes(app, api_auth=_api_auth)
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    app.state.config_manager = _FakeCM(cfg if cfg is not None else {})
    return TestClient(app)


class _FakeCM:
    """set_overlay_flag 记录写入并同步内存 config（预设端点测试用）。"""

    def __init__(self, cfg):
        self.config = cfg
        self.flags = []

    def set_overlay_flag(self, path, value):
        self.flags.append((path, value))
        node = self.config
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        return True, "已保存"


def _seed_conv(store, cid, *, chat_type="private"):
    with store._lock:
        store._conn.execute(
            """INSERT OR IGNORE INTO conversations
               (conversation_id, platform, account_id, chat_key, chat_type,
                last_ts, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid, "telegram", "a1", cid.split(":")[-1], chat_type,
             time.time(), time.time(), time.time()),
        )
        store._conn.commit()


def _mk_stage_chain(store, chain_id, stage):
    store.upsert_workflow_chain({
        "chain_id": chain_id,
        "name": chain_id,
        "steps": [{"action_type": "note", "note": "跟进", "delay_hours": 0}],
        "trigger_conditions": {"stage_enter": stage},
    })


# ── P0-4：stage_enter 自动挂链 ──────────────────────────────────────────────

def test_stage_autostart_attaches_once(tmp_path):
    store = _store(tmp_path)
    _mk_stage_chain(store, "cq", "quoting")
    cid = "tg:a1:s1"
    _seed_conv(store, cid)
    store.set_journey_stage(cid, "quoting", src="auto")
    runner = WorkflowRunner(store)
    assert runner.auto_start_chains(journey_enabled=True) == 1
    execs = store.list_chain_executions(conversation_id=cid)
    assert len(execs) == 1
    ctx = json.loads(execs[0]["context_json"])
    assert ctx.get("auto") is True
    # 该执行完成后（终态），同一次阶段进入不再重复挂
    store.complete_workflow_execution(execs[0]["exec_id"])
    assert runner.auto_start_chains(journey_enabled=True) == 0


def test_stage_autostart_gated_by_journey_flag(tmp_path):
    store = _store(tmp_path)
    _mk_stage_chain(store, "cq", "quoting")
    cid = "tg:a1:s2"
    _seed_conv(store, cid)
    store.set_journey_stage(cid, "quoting", src="auto")
    runner = WorkflowRunner(store)
    assert runner.auto_start_chains(journey_enabled=False) == 0


def test_stage_autostart_reenters_on_new_stage_ts(tmp_path):
    """阶段再次进入（如撤销成交回到 quoting 后又成交→关怀链再挂）按新 ts 判重。"""
    store = _store(tmp_path)
    _mk_stage_chain(store, "cd", "deal")
    cid = "tg:a1:s3"
    _seed_conv(store, cid)
    store.set_journey_stage(cid, "deal", src="deal", ts=time.time() - 3600)
    runner = WorkflowRunner(store)
    assert runner.auto_start_chains(journey_enabled=True) == 1
    ex = store.list_chain_executions(conversation_id=cid)[0]
    store.complete_workflow_execution(ex["exec_id"])
    # 同一 ts 不重复
    assert runner.auto_start_chains(journey_enabled=True) == 0
    # 阶段时间戳前移（新一次进入）→ 允许再挂
    store.set_journey_stage(cid, "deal", src="deal", ts=time.time() + 1)
    assert runner.auto_start_chains(journey_enabled=True) == 1


def test_stage_autostart_respects_budget(tmp_path):
    store = _store(tmp_path)
    _mk_stage_chain(store, "cq", "quoting")
    for i in range(3):
        cid = f"tg:a1:b{i}"
        _seed_conv(store, cid)
        store.set_journey_stage(cid, "quoting", src="auto")
    runner = WorkflowRunner(store)
    assert runner.auto_start_chains(journey_enabled=True, max_per_day=2) == 2


def test_stage_autostart_running_chain_not_duplicated(tmp_path):
    store = _store(tmp_path)
    _mk_stage_chain(store, "cq", "quoting")
    cid = "tg:a1:s4"
    _seed_conv(store, cid)
    store.set_journey_stage(cid, "quoting", src="auto")
    store.start_chain_execution("cq", cid, {})   # 已有在途
    runner = WorkflowRunner(store)
    assert runner.auto_start_chains(journey_enabled=True) == 0


# ── P0-3：漏斗营收归因 ──────────────────────────────────────────────────────

def test_funnel_attributes_deal_to_latest_chain(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    for c in ("cA", "cB"):
        store.upsert_workflow_chain({
            "chain_id": c, "name": c,
            "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
            "trigger_conditions": {},
        })
    cid = "tg:a1:f1"
    _seed_conv(store, cid)
    # 先 cA 后 cB，成交发生在两者之后 → 归 cB（最近启动），营收不双计
    with store._lock:
        store._conn.execute(
            """INSERT INTO workflow_executions
               (exec_id, chain_id, conversation_id, current_step, status,
                context_json, started_at, updated_at, next_step_at, last_result_json)
               VALUES ('e1','cA',?,0,'completed','{}',?,?,0,'')""",
            (cid, now - 7200, now - 7200))
        store._conn.execute(
            """INSERT INTO workflow_executions
               (exec_id, chain_id, conversation_id, current_step, status,
                context_json, started_at, updated_at, next_step_at, last_result_json)
               VALUES ('e2','cB',?,0,'completed','{}',?,?,0,'')""",
            (cid, now - 3600, now - 3600))
        store._conn.commit()
    store.record_deal_event(cid, amount=88.0, ts=now - 60)
    d = chain_funnel(store, now=now)
    assert d["total"]["deals_n"] == 1
    assert d["total"]["deal_amount"] == 88.0
    per = {c["chain_id"]: c for c in d["chains"]}
    assert per["cB"]["deals_n"] == 1 and per["cB"]["deal_amount"] == 88.0
    assert per["cA"]["deals_n"] == 0


def test_funnel_deal_before_start_not_attributed(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.upsert_workflow_chain({
        "chain_id": "cA", "name": "cA",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    cid = "tg:a1:f2"
    _seed_conv(store, cid)
    store.record_deal_event(cid, amount=10.0, ts=now - 3600)
    with store._lock:
        store._conn.execute(
            """INSERT INTO workflow_executions
               (exec_id, chain_id, conversation_id, current_step, status,
                context_json, started_at, updated_at, next_step_at, last_result_json)
               VALUES ('e1','cA',?,0,'running','{}',?,?,0,'')""",
            (cid, now - 60, now - 60))
        store._conn.commit()
    d = chain_funnel(store, now=now)
    assert d["total"]["deals_n"] == 0


def test_funnel_revoked_deal_excluded(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.upsert_workflow_chain({
        "chain_id": "cA", "name": "cA",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    cid = "tg:a1:f3"
    _seed_conv(store, cid)
    with store._lock:
        store._conn.execute(
            """INSERT INTO workflow_executions
               (exec_id, chain_id, conversation_id, current_step, status,
                context_json, started_at, updated_at, next_step_at, last_result_json)
               VALUES ('e1','cA',?,0,'running','{}',?,?,0,'')""",
            (cid, now - 3600, now - 3600))
        store._conn.commit()
    did = store.record_deal_event(cid, amount=10.0, ts=now - 60)
    store.revoke_deal_event(did)
    d = chain_funnel(store, now=now)
    assert d["total"]["deals_n"] == 0


# ── P0-5：成交引擎预设 ──────────────────────────────────────────────────────

def test_apply_deal_engine_enable_wires_everything(tmp_path):
    store = _store(tmp_path)
    cm = _FakeCM({})
    res = apply_deal_engine(store, cm, True)
    assert res["ok"] is True and res["flag_ok"] is True
    assert ("inbox.workflows.journey.enabled", True) in cm.flags
    for stage, cid in STAGE_CHAIN_REC.items():
        chain = store.get_workflow_chain(cid)
        conds = json.loads(chain["trigger_conditions"])
        assert conds.get("stage_enter") == stage, cid
    # 唤回链 on_reply 语义补齐
    re_chain = store.get_workflow_chain("starter_reactivate_3step")
    assert re_chain["on_reply"] == "complete"
    st = deal_engine_status(store, cm.config)
    assert st["engine_on"] is True and st["journey_enabled"] is True


def test_apply_deal_engine_disable_reverses(tmp_path):
    store = _store(tmp_path)
    cm = _FakeCM({})
    apply_deal_engine(store, cm, True)
    res = apply_deal_engine(store, cm, False)
    assert res["ok"] is True
    for _stage, cid in STAGE_CHAIN_REC.items():
        conds = json.loads(store.get_workflow_chain(cid)["trigger_conditions"])
        assert "stage_enter" not in conds
    st = deal_engine_status(store, cm.config)
    assert st["engine_on"] is False and st["journey_enabled"] is False


def test_apply_deal_engine_preserves_operator_edits(tmp_path):
    """运营改过的链（改名/停用/自配 silence_days）预设只动 stage_enter。"""
    store = _store(tmp_path)
    ensure_starter_chains(store)
    qid = STAGE_CHAIN_REC["quoting"]
    chain = store.get_workflow_chain(qid)
    store.upsert_workflow_chain({
        "chain_id": qid, "name": "我的跟单链",
        "steps": json.loads(chain["steps_json"]),
        "trigger_conditions": {"silence_days": 3},
        "enabled": 0,
    })
    cm = _FakeCM({})
    apply_deal_engine(store, cm, True)
    after = store.get_workflow_chain(qid)
    assert after["name"] == "我的跟单链"
    assert after["enabled"] == 0
    conds = json.loads(after["trigger_conditions"])
    assert conds == {"silence_days": 3, "stage_enter": "quoting"}
    # 停用的链不算接好 → engine_on False（状态如实，不装绿）
    assert deal_engine_status(store, cm.config)["engine_on"] is False


def test_apply_deal_engine_idempotent(tmp_path):
    store = _store(tmp_path)
    cm = _FakeCM({})
    apply_deal_engine(store, cm, True)
    res = apply_deal_engine(store, cm, True)
    assert res["ok"] is True
    st = deal_engine_status(store, cm.config)
    assert st["engine_on"] is True


def test_apply_deal_engine_without_cm_reports_failure(tmp_path):
    store = _store(tmp_path)
    res = apply_deal_engine(store, None, True)
    assert res["ok"] is False and res["flag_ok"] is False


def test_stage_rec_targets_are_real_seeds():
    starter_ids = {c["chain_id"] for c in STARTER_CHAINS}
    for stage, cid in STAGE_CHAIN_REC.items():
        assert cid in starter_ids, f"{stage} 推荐指向幽灵链 {cid}"


# ── 实施92b：自动发送档（替坐席发消息自动推进）─────────────────────────────

def test_auto_send_enable_wires_flag_and_exec_mode(tmp_path):
    store = _store(tmp_path)
    cm = _FakeCM({})
    res = apply_deal_engine(store, cm, True, auto_send=True)
    assert res["ok"] is True and res["auto_flag_ok"] is True
    assert ("inbox.workflows.auto_advance.enabled", True) in cm.flags
    for _stage, cid in STAGE_CHAIN_REC.items():
        assert store.get_workflow_chain(cid)["exec_mode"] == "auto", cid
    st = deal_engine_status(store, cm.config)
    assert st["auto_send"]["on"] is True
    assert st["auto_send"]["auto_advance_enabled"] is True
    assert st["auto_send"]["chains_auto"] is True


def test_auto_send_off_reverts_to_remind(tmp_path):
    store = _store(tmp_path)
    cm = _FakeCM({})
    apply_deal_engine(store, cm, True, auto_send=True)
    res = apply_deal_engine(store, cm, True, auto_send=False)
    assert res["ok"] is True
    assert ("inbox.workflows.auto_advance.enabled", False) in cm.flags
    for _stage, cid in STAGE_CHAIN_REC.items():
        assert store.get_workflow_chain(cid)["exec_mode"] == "remind", cid
    assert deal_engine_status(store, cm.config)["auto_send"]["on"] is False


def test_auto_send_none_preserves_operator_mode(tmp_path):
    """主开关重复 apply（不带 auto_send）不得清掉运营已选的自动档。"""
    store = _store(tmp_path)
    cm = _FakeCM({})
    apply_deal_engine(store, cm, True, auto_send=True)
    apply_deal_engine(store, cm, True)   # auto_send 缺省 None
    for _stage, cid in STAGE_CHAIN_REC.items():
        assert store.get_workflow_chain(cid)["exec_mode"] == "auto", cid
    assert deal_engine_status(store, cm.config)["auto_send"]["on"] is True


def test_engine_disable_forces_auto_send_off(tmp_path):
    """关引擎必须回到最安全档——不许留「引擎关了链还在自动发」的半开态。"""
    store = _store(tmp_path)
    cm = _FakeCM({})
    apply_deal_engine(store, cm, True, auto_send=True)
    apply_deal_engine(store, cm, False)
    assert ("inbox.workflows.auto_advance.enabled", False) in cm.flags
    for _stage, cid in STAGE_CHAIN_REC.items():
        assert store.get_workflow_chain(cid)["exec_mode"] == "remind", cid
    st = deal_engine_status(store, cm.config)
    assert st["engine_on"] is False and st["auto_send"]["on"] is False


def test_auto_send_status_reports_deliver_wiring(tmp_path):
    store = _store(tmp_path)
    cm = _FakeCM({"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}})
    apply_deal_engine(store, cm, True, auto_send=True)
    assert deal_engine_status(store, cm.config)["auto_send"][
        "deliver_wired"] is True
    cm2 = _FakeCM({})
    st2 = deal_engine_status(store, cm2.config)
    assert st2["auto_send"]["deliver_wired"] is False


def test_deal_engine_route_passes_auto_send(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/workspace/workflows/deal-engine",
               json={"enable": True, "auto_send": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    flags = c.app.state.config_manager.flags
    assert ("inbox.workflows.journey.enabled", True) in flags
    assert ("inbox.workflows.auto_advance.enabled", True) in flags
    d = c.get("/api/workspace/workflows/deal-engine").json()
    assert d["auto_send"]["on"] is True
    # 不带 auto_send 的普通开关调用不写 auto_advance 键
    n_before = len([f for f in flags if f[0].endswith("auto_advance.enabled")])
    c.post("/api/workspace/workflows/deal-engine", json={"enable": True})
    n_after = len([f for f in c.app.state.config_manager.flags
                   if f[0].endswith("auto_advance.enabled")])
    assert n_after == n_before


# ── 实施92b：批量挂链 ───────────────────────────────────────────────────────

def _seed_silent_conv(store, cid, days_silent):
    _seed_conv(store, cid)
    with store._lock:
        store._conn.execute(
            "UPDATE conversations SET last_ts = ? WHERE conversation_id = ?",
            (time.time() - days_silent * 86400, cid))
        store._conn.commit()


def test_bulk_start_dry_run_then_real(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    store.upsert_workflow_chain({
        "chain_id": "cb", "name": "cb",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    for i in range(3):
        _seed_silent_conv(store, f"tg:a1:bk{i}", 10)
    _seed_silent_conv(store, "tg:a1:fresh", 0.1)   # 不够沉默
    r = c.post("/api/workspace/workflow-chains/cb/bulk-start",
               json={"silent_days_min": 7})
    d = r.json()
    assert r.status_code == 200 and d["dry_run"] is True
    assert d["candidates"] == 3 and len(d["sample"]) == 3
    # 默认 dry_run=true：没落任何执行
    assert store.list_chain_executions(status="running") == []
    r2 = c.post("/api/workspace/workflow-chains/cb/bulk-start",
                json={"silent_days_min": 7, "dry_run": False})
    assert r2.json()["started"] == 3
    assert len(store.list_chain_executions(status="running")) == 3
    ctx = json.loads(store.list_chain_executions(
        status="running")[0]["context_json"])
    assert ctx.get("bulk") is True


def test_bulk_start_skips_inflight_and_recent(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    store.upsert_workflow_chain({
        "chain_id": "cb", "name": "cb",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    _seed_silent_conv(store, "tg:a1:in1", 10)
    _seed_silent_conv(store, "tg:a1:done1", 10)
    _seed_silent_conv(store, "tg:a1:ok1", 10)
    store.start_chain_execution("cb", "tg:a1:in1", {})          # 在途
    eid = store.start_chain_execution("cb", "tg:a1:done1", {})  # 7 天内挂过
    store.complete_workflow_execution(eid)
    d = c.post("/api/workspace/workflow-chains/cb/bulk-start",
               json={"silent_days_min": 7}).json()
    assert d["candidates"] == 1
    assert d["sample"][0]["conversation_id"] == "tg:a1:ok1"


def test_bulk_start_stage_filter_and_guards(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    store.upsert_workflow_chain({
        "chain_id": "cb", "name": "cb",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    _seed_conv(store, "tg:a1:q1")
    store.set_journey_stage("tg:a1:q1", "quoting", src="auto")
    d = c.post("/api/workspace/workflow-chains/cb/bulk-start",
               json={"stage": "quoting"}).json()
    assert d["candidates"] == 1
    # 守卫：无筛选 422 / 链不存在 404 / 停用 422
    assert c.post("/api/workspace/workflow-chains/cb/bulk-start",
                  json={}).status_code == 422
    assert c.post("/api/workspace/workflow-chains/ghost/bulk-start",
                  json={"silent_days_min": 7}).status_code == 404
    store.upsert_workflow_chain({
        "chain_id": "cb", "name": "cb",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {}, "enabled": 0,
    })
    assert c.post("/api/workspace/workflow-chains/cb/bulk-start",
                  json={"silent_days_min": 7}).status_code == 422


def test_bulk_start_conversation_ids(tmp_path):
    """实施92e：显式会话清单（收件箱筛选即选择）——幽灵 id/群聊剔除、
    与既有防重共用、可与其他筛选并集。"""
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    store.upsert_workflow_chain({
        "chain_id": "cb", "name": "cb",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    _seed_conv(store, "tg:a1:ok1")
    _seed_conv(store, "tg:a1:ok2")
    _seed_conv(store, "tg:a1:grp1", chat_type="group")
    _seed_conv(store, "tg:a1:busy1")
    store.start_chain_execution("cb", "tg:a1:busy1", {})
    body = {"conversation_ids": [
        "tg:a1:ok1", "tg:a1:ok2", "tg:a1:grp1", "ghost:x", "tg:a1:busy1",
    ]}
    d = c.post("/api/workspace/workflow-chains/cb/bulk-start",
               json=body).json()
    assert d["dry_run"] is True and d["candidates"] == 2
    got = {s["conversation_id"] for s in d["sample"]}
    assert got == {"tg:a1:ok1", "tg:a1:ok2"}
    d2 = c.post("/api/workspace/workflow-chains/cb/bulk-start",
                json={**body, "dry_run": False}).json()
    assert d2["started"] == 2
    # 落地后同清单重跑 → 全被在途防重剔除
    d3 = c.post("/api/workspace/workflow-chains/cb/bulk-start",
                json=body).json()
    assert d3["candidates"] == 0


def test_bulk_start_respects_limit_cap(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    store.upsert_workflow_chain({
        "chain_id": "cb", "name": "cb",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    for i in range(5):
        _seed_silent_conv(store, f"tg:a1:cap{i}", 10)
    d = c.post("/api/workspace/workflow-chains/cb/bulk-start",
               json={"silent_days_min": 7, "limit": 2}).json()
    assert d["candidates"] == 2


# ── 路由 ────────────────────────────────────────────────────────────────────

def test_journey_routes_end_to_end(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    cid = "tg:a1:r1"
    _seed_conv(store, cid)
    # 初始：空阶段
    d = c.get(f"/api/workspace/conv/{cid}/journey").json()
    assert d["ok"] and d["stage"] == "" and d["stages"][0] == "new"
    assert d["journey_enabled"] is False
    # 手动设阶段
    r = c.post(f"/api/workspace/conv/{cid}/journey/stage",
               json={"stage": "quoting"})
    assert r.status_code == 200 and r.json()["stage"] == "quoting"
    assert c.post(f"/api/workspace/conv/{cid}/journey/stage",
                  json={"stage": "vip"}).status_code == 422
    # 标记成交
    r2 = c.post(f"/api/workspace/conv/{cid}/deal",
                json={"amount": 66.6, "currency": "USD", "note": "套餐A"})
    assert r2.status_code == 200
    deal_id = r2.json()["deal_id"]
    assert r2.json()["stage"] == "deal"
    d2 = c.get(f"/api/workspace/conv/{cid}/journey").json()
    assert d2["stage"] == "deal" and len(d2["deals"]) == 1
    assert d2["deals"][0]["amount"] == 66.6
    # 撤销 → 回退 quoting
    r3 = c.post(f"/api/workspace/conv/{cid}/deal/{deal_id}/revoke")
    assert r3.status_code == 200 and r3.json()["stage"] == "quoting"
    # 撤销不存在的 → 404
    assert c.post(
        f"/api/workspace/conv/{cid}/deal/99999/revoke").status_code == 404


def test_deal_amount_garbage_tolerated(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    cid = "tg:a1:r2"
    _seed_conv(store, cid)
    r = c.post(f"/api/workspace/conv/{cid}/deal",
               json={"amount": "not-a-number"})
    assert r.status_code == 200
    assert store.list_deal_events(cid)[0]["amount"] == 0.0


def test_deal_engine_routes(tmp_path):
    c = _client(tmp_path)
    d = c.get("/api/workspace/workflows/deal-engine").json()
    assert d["ok"] is True and d["engine_on"] is False
    r = c.post("/api/workspace/workflows/deal-engine", json={"enable": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    d2 = c.get("/api/workspace/workflows/deal-engine").json()
    assert d2["engine_on"] is True
    assert c.app.state.config_manager.flags == [
        ("inbox.workflows.journey.enabled", True)]
    r2 = c.post("/api/workspace/workflows/deal-engine", json={"enable": False})
    assert r2.status_code == 200
    assert c.get("/api/workspace/workflows/deal-engine").json()[
        "engine_on"] is False


def test_monetize_grant_bridges_deal_event(tmp_path):
    """实施92 P0-3 桥：变现入账（contact_key 恰为会话 id）→ 成交台账 + 阶段；
    contact_key 对不上会话 → 静默跳过；unlock ref 幂等重放不双记。"""
    from src.utils.entitlement_store import EntitlementStore
    from src.web.routes.monetization_routes import register_monetization_routes

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    cm = SimpleNamespace(
        config={"monetization": {"enabled": True}}, config_path="")
    app.state.config_manager = cm
    register_monetization_routes(app, api_auth=_api_auth, config_manager=cm)
    app.state.entitlement_store = EntitlementStore(":memory:")
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    c = TestClient(app)
    cid = "tg:a1:m1"
    _seed_conv(store, cid)
    # 命中会话 → 桥落成交事件 + 阶段 deal
    r = c.post("/api/monetize/grant", json={
        "contact_key": cid, "kind": "unlock", "item_id": "bazi_reading",
        "amount": 4.99, "ref": "ref-1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    deals = store.list_deal_events(cid)
    assert len(deals) == 1
    assert deals[0]["source"] == "monetize" and deals[0]["amount"] == 4.99
    assert store.get_journey_stage(cid)["stage"] == "deal"
    # ref 幂等重放：入账层跳过 → 桥不再落第二笔
    r2 = c.post("/api/monetize/grant", json={
        "contact_key": cid, "kind": "unlock", "item_id": "bazi_reading",
        "amount": 4.99, "ref": "ref-1"})
    assert r2.status_code == 200
    assert len(store.list_deal_events(cid)) == 1
    # contact_key 不是收件箱会话 → 静默跳过、入账照常
    r3 = c.post("/api/monetize/grant", json={
        "contact_key": "not-a-conversation", "kind": "gift",
        "item_id": "rose", "amount": 1.0})
    assert r3.status_code == 200 and r3.json()["ok"] is True
    assert store.list_deal_events("not-a-conversation") == []


def test_journey_funnel_endpoint(tmp_path):
    """实施92c：阶段分布 + 窗口成交汇总（/funnel 页读数口径）。"""
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    for i, stage in enumerate(["contacted", "contacted", "quoting"]):
        cid = f"tg:a1:jf{i}"
        _seed_conv(store, cid)
        store.set_journey_stage(cid, stage, src="auto")
    _seed_conv(store, "tg:a1:jfd")
    store.set_journey_stage("tg:a1:jfd", "deal", src="deal")
    store.record_deal_event("tg:a1:jfd", amount=120.0)
    old = store.record_deal_event("tg:a1:jfd", amount=999.0,
                                  ts=time.time() - 30 * 86400)   # 窗外
    assert old
    d = c.get("/api/workspace/journey-funnel").json()
    assert d["ok"] is True and d["order"][0] == "new"
    assert d["stages"] == {"contacted": 2, "quoting": 1, "deal": 1}
    assert d["deals"]["n"] == 1 and d["deals"]["amount"] == 120.0
    # 群聊不计入
    _seed_conv(store, "tg:a1:jfg", chat_type="group")
    store.set_journey_stage("tg:a1:jfg", "quoting", src="auto")
    d2 = c.get("/api/workspace/journey-funnel").json()
    assert d2["stages"]["quoting"] == 1


def _add_msg(store, cid, direction, ts):
    with store._lock:
        store._conn.execute(
            """INSERT INTO messages
               (message_id, conversation_id, direction, text, ts, ingested_at)
               VALUES (?,?,?,?,?,?)""",
            (f"{cid}:{direction}:{ts}", cid, direction, "x", float(ts),
             float(ts)))
        store._conn.commit()


def test_journey_evidence_and_suggestion(tmp_path):
    """实施92d：「为什么是现在」证据 + 阶段推荐链（有界建议卡数据位）。"""
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    ensure_starter_chains(store)
    cid = "tg:a1:ev1"
    _seed_conv(store, cid)
    now = time.time()
    _add_msg(store, cid, "out", now - 7200)   # 我方报价后 2h 客户没回
    store.set_journey_stage(cid, "quoting", src="manual")
    d = c.get(f"/api/workspace/conv/{cid}/journey").json()
    assert d["evidence"]["waiting_on"] == "customer"
    assert 1.8 <= d["evidence"]["wait_hours"] <= 2.2
    assert d["evidence"]["stage_age_hours"] >= 0
    assert d["suggested_chain"]["chain_id"] == "starter_quote_followup"
    # 在途执行 → 不再建议（链已接管节奏）
    store.start_chain_execution("starter_quote_followup", cid, {})
    assert c.get(f"/api/workspace/conv/{cid}/journey").json()[
        "suggested_chain"] is None
    # 完成后 7 天窗内也不建议（防连环挂）
    ex = store.list_chain_executions(conversation_id=cid)[0]
    store.complete_workflow_execution(ex["exec_id"])
    assert c.get(f"/api/workspace/conv/{cid}/journey").json()[
        "suggested_chain"] is None


def test_journey_evidence_waiting_on_us(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    cid = "tg:a1:ev2"
    _seed_conv(store, cid)
    _add_msg(store, cid, "in", time.time() - 3 * 3600)   # 客户 3h 前来消息没人回
    d = c.get(f"/api/workspace/conv/{cid}/journey").json()
    assert d["evidence"]["waiting_on"] == "us"
    assert 2.8 <= d["evidence"]["wait_hours"] <= 3.2
    # 无推荐映射的阶段 → 不建议
    store.set_journey_stage(cid, "nurturing", src="auto")
    assert c.get(f"/api/workspace/conv/{cid}/journey").json()[
        "suggested_chain"] is None


def test_value_report_deals_section(tmp_path):
    """实施92d：价值周报成交段（两周全零不出段 / 撤销不计 / SOP 归因注脚）。"""
    from src.ops.value_report import build_weekly_value
    store = _store(tmp_path)
    v0 = build_weekly_value(store)
    assert "deals" not in v0["this_week"], "零成交不该出段"
    cid = "tg:a1:vr1"
    _seed_conv(store, cid)
    now = time.time()
    store.record_deal_event(cid, amount=100.0, ts=now - 60)          # 本周
    store.record_deal_event(cid, amount=50.0, ts=now - 10 * 86400)   # 上周窗
    rid = store.record_deal_event(cid, amount=999.0, ts=now - 30)    # 撤销不计
    store.revoke_deal_event(rid)
    # 成交前 1h 启动过一条链 → 归因注脚
    store.upsert_workflow_chain({
        "chain_id": "cv", "name": "cv",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    with store._lock:
        store._conn.execute(
            """INSERT INTO workflow_executions
               (exec_id, chain_id, conversation_id, current_step, status,
                context_json, started_at, updated_at, next_step_at, last_result_json)
               VALUES ('ev1','cv',?,0,'completed','{}',?,?,0,'')""",
            (cid, now - 3600, now - 3600))
        store._conn.commit()
    v = build_weekly_value(store, now=now)
    de = v["this_week"]["deals"]
    assert de["n"] == 1 and de["amount"] == 100.0
    assert de["chain_attributed"] == 1
    assert v["last_week"]["deals"]["n"] == 1
    assert any("促成成交 1 单" in ln and "经跟进 SOP 归因" in ln
               for ln in v["text_lines"])


def test_value_report_cta_section(tmp_path):
    """实施93d：价值周报追踪短链段（cohort 口径点开率 / 零铸链不出段 / 摘要行）。"""
    from src.ops.value_report import build_weekly_value
    store = _store(tmp_path)
    assert "cta" not in build_weekly_value(store)["this_week"], "零铸链不该出段"
    now = time.time()
    for cid in ("tg:a1:k1", "tg:a1:k2", "tg:a1:k3"):
        _seed_conv(store, cid)
    tid = store.upsert_cta_target({"name": "站点", "url": "https://x.example"})
    # 本周 2 条（1 条被点开）+ 上周窗 1 条（未点开）
    store.insert_cta_link("tok-a", "tg:a1:k1", tid, ts=now - 120)
    store.insert_cta_link("tok-b", "tg:a1:k2", tid, ts=now - 60)
    store.record_cta_click("tok-a", ts=now - 30)
    store.insert_cta_link("tok-c", "tg:a1:k3", tid, ts=now - 10 * 86400)
    v = build_weekly_value(store, now=now)
    k = v["this_week"]["cta"]
    assert k["links"] == 2 and k["clicked"] == 1 and k["click_rate"] == 50.0
    assert v["last_week"]["cta"]["links"] == 1
    assert any("发出追踪链接 2 条" in ln and "点开率 50.0%" in ln
               for ln in v["text_lines"])


def test_new_endpoints_blocked_when_workflows_off(tmp_path):
    c = _client(tmp_path, cfg={"inbox": {"workflows": {"enabled": False}}})
    paths = [
        ("GET", "/api/workspace/conv/tg:a:x/journey", None),
        ("POST", "/api/workspace/conv/tg:a:x/journey/stage", {"stage": "new"}),
        ("POST", "/api/workspace/conv/tg:a:x/deal", {}),
        ("POST", "/api/workspace/conv/tg:a:x/deal/1/revoke", None),
        ("GET", "/api/workspace/workflows/deal-engine", None),
        ("POST", "/api/workspace/workflows/deal-engine", {"enable": True}),
    ]
    for method, path, body in paths:
        r = c.request(method, path) if body is None else c.request(
            method, path, json=body)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
