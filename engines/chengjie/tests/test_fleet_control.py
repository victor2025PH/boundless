"""fleet_control（主控）+ src.fleet.agent（节点）：store / 路由 / Agent 端到端（进程内）。

    $env:PYTHONPATH=""; .\\.venv\\Scripts\\python.exe -m pytest tests\\test_fleet_control.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from domains.fleet_control.web.routes import register_routes
from src.fleet import agent as agent_mod
from src.fleet.agent import AgentConfig, NodeAgent, Unauthorized, reduce_fleet_health, reduce_player_overview
from src.fleet.identity import node_machine_id
from src.fleet.protocol import (
    LEGACY_ALLOWED_KINDS, PROTO_VERSION, STATUS_CANCELLED, STATUS_DONE, STATUS_EXPIRED, STATUS_FAILED, STATUS_PULLED,
    STATUS_QUEUED, STATUS_REJECTED, TASK_ACCOUNT_HEALTH, TASK_KINDS, TASK_PING, TASK_PULL_OVERVIEW, TASK_STOP_ACCOUNT,
    TASK_UPGRADE, sanitize_heartbeat, task_envelope,
)
from src.fleet.store import FleetStore, resolve_fleet_cfg, set_store

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean():
    set_store(None)
    yield
    set_store(None)


@pytest.fixture
def st(tmp_path):
    s = FleetStore(tmp_path / "fleet.db", offline_after_sec=120)
    yield s
    s.close()


def _enroll(st: FleetStore, mid="m-aaaa", now=T0, **kw):
    code = st.create_enroll_code(label=kw.pop("label", "机器A"), group_name=kw.pop("group_name", "菲律宾"), now=now)["code"]
    res = st.enroll(code=code, machine_id=mid, host_name="PC-A", proto_version=PROTO_VERSION, agent_version="0.1.0", now=now, **kw)
    assert res["ok"], res
    return res


# ── protocol ──────────────────────────────────────────────────────────────────

def test_protocol_envelope_and_sanitize():
    env = task_envelope(task_id="t1", kind=TASK_PING, node_id="n1", ttl_sec=60, created_at=T0)
    assert env["expires_at"] == T0 + 60 and env["proto_version"] == PROTO_VERSION and env["target"] == {}
    hb = sanitize_heartbeat({"agent_version": "1", "chat_log": ["hi"], "accounts": {"total": 3}})
    assert "chat_log" not in hb and hb["accounts"] == {"total": 3}
    assert sanitize_heartbeat("nope") == {}
    assert TASK_STOP_ACCOUNT in TASK_KINDS and set(LEGACY_ALLOWED_KINDS) <= set(TASK_KINDS)


# ── store：注册码 / 注册 / 鉴权 ────────────────────────────────────────────────

def test_enroll_code_lifecycle(st):
    c = st.create_enroll_code(label="x", ttl_min=10, now=T0)
    assert len(c["code"]) == 8 and c["code"].isdigit()
    assert [x["code"] for x in st.list_enroll_codes(now=T0)] == [c["code"]]
    assert st.list_enroll_codes(now=T0 + 11 * 60) == []
    assert st.enroll(code=c["code"], machine_id="m-1", proto_version=1, now=T0 + 11 * 60)["error"] == "invalid_or_expired_code"
    res = st.enroll(code=c["code"], machine_id="m-1", proto_version=1, now=T0)
    assert res["ok"] and res["node_key"].startswith("nk_") and res["label"] == "x"
    # 一次性
    assert st.enroll(code=c["code"], machine_id="m-2", proto_version=1, now=T0)["error"] == "invalid_or_expired_code"
    assert st.list_enroll_codes(include_used=True, now=T0)[0]["status"] == "used"


def test_enroll_requires_fields_and_proto(st):
    assert st.enroll(code="", machine_id="m", proto_version=1)["error"] == "code_and_machine_id_required"
    code = st.create_enroll_code(now=T0)["code"]
    assert st.enroll(code=code, machine_id="m", proto_version=999, now=T0)["error"] == "proto_incompatible"
    assert st.enroll(code=code, machine_id="m", proto_version="abc", now=T0)["error"] == "proto_incompatible"


def test_enroll_same_machine_reuses_node_and_rotates_key(st):
    a = _enroll(st, "m-same")
    b = _enroll(st, "m-same", now=T0 + 10, label="", group_name="")
    assert a["node_id"] == b["node_id"] and a["node_key"] != b["node_key"]
    assert st.authenticate(a["node_key"]) is None
    node = st.authenticate(b["node_key"])
    assert node["node_id"] == a["node_id"] and node["label"] == "机器A" and node["group_name"] == "菲律宾"
    assert len(st.list_nodes()) == 1


def test_authenticate_rejects_bad_keys_and_revoked(st):
    a = _enroll(st)
    assert st.authenticate("") is None and st.authenticate("Bearer x") is None and st.authenticate("nk_wrong") is None
    st.enqueue(a["node_id"], TASK_PING, now=T0)
    assert st.revoke(a["node_id"], now=T0)
    assert st.authenticate(a["node_key"]) is None
    assert st.get_node(a["node_id"], now=T0)["state"] == "revoked"
    assert [t["status"] for t in st.list_tasks(node_id=a["node_id"])] == [STATUS_CANCELLED]
    assert st.enqueue(a["node_id"], TASK_PING) is None
    assert not st.revoke("n_nope")
    # 凭新注册码复活
    c = _enroll(st, "m-aaaa", now=T0 + 5)
    assert st.authenticate(c["node_key"])["status"] == "active"


# ── store：心跳 / 在线判定 ─────────────────────────────────────────────────────

def test_heartbeat_updates_node_and_state(st):
    a = _enroll(st)
    nid = a["node_id"]
    assert st.get_node(nid, now=T0 + 10)["state"] == "online"
    assert st.get_node(nid, now=T0 + 121)["state"] == "offline"
    st.heartbeat(nid, {"agent_version": "0.2.0", "app_version": "1.0.40", "accounts": {"total": 5, "online": 4},
                       "instances": [{"name": "player", "up": True}, {"name": "story", "up": False}],
                       "secret_chat": "不该进来", "metrics": {"cpu_pct": 12}}, now=T0 + 200)
    n = st.get_node(nid, now=T0 + 210)
    assert n["state"] == "online" and n["agent_version"] == "0.2.0" and n["app_version"] == "1.0.40"
    assert "secret_chat" not in n["last_heartbeat"] and n["last_heartbeat"]["accounts"]["online"] == 4
    h = st.heartbeat_history(nid)
    assert len(h) == 1 and h[0]["accounts_total"] == 5 and h[0]["instances_up"] == 1 and h[0]["cpu_pct"] == 12


def test_heartbeat_history_capped(st):
    from src.fleet import store as store_mod
    a = _enroll(st)
    for i in range(store_mod.HEARTBEAT_KEEP + 15):
        st.heartbeat(a["node_id"], {"accounts": {"total": i}}, now=T0 + i)
    h = st.heartbeat_history(a["node_id"], limit=500)
    assert len(h) == store_mod.HEARTBEAT_KEEP and h[0]["accounts_total"] == store_mod.HEARTBEAT_KEEP + 14


# ── store：任务 ───────────────────────────────────────────────────────────────

def test_enqueue_pull_ack_priority_and_idempotent(st):
    a = _enroll(st)
    nid = a["node_id"]
    t_ov = st.enqueue(nid, TASK_PULL_OVERVIEW, now=T0)
    t_ping = st.enqueue(nid, TASK_PING, payload={"echo": 1}, now=T0 + 1)
    t_stop = st.enqueue(nid, TASK_STOP_ACCOUNT, target={"phone": "639170000001"}, now=T0 + 2)
    assert st.enqueue(nid, "rm -rf", now=T0) is None
    assert st.enqueue("n_ghost", TASK_PING) is None
    assert st.has_queued(nid)
    pulled = st.pull(nid, now=T0 + 3)
    assert [p["kind"] for p in pulled] == [TASK_STOP_ACCOUNT, TASK_PING, TASK_PULL_OVERVIEW]
    assert pulled[1]["payload"] == {"echo": 1} and pulled[0]["target"] == {"phone": "639170000001"}
    assert set(pulled[0]) == {"task_id", "kind", "node_id", "target", "payload", "ttl_sec", "expires_at", "proto_version"}
    assert st.pull(nid, now=T0 + 4) == [] and not st.has_queued(nid)
    assert st.get_task(t_ping["task_id"])["status"] == STATUS_PULLED
    r = st.ack(t_ping["task_id"], node_id=nid, status=STATUS_DONE, result={"pong": True}, detail="pong", now=T0 + 5)
    assert r["status"] == STATUS_DONE and r["result"] == {"pong": True} and r["acked_at"] == T0 + 5
    # 幂等：再 ack failed 不改
    r2 = st.ack(t_ping["task_id"], node_id=nid, status=STATUS_FAILED, now=T0 + 6)
    assert r2["status"] == STATUS_DONE and r2["acked_at"] == T0 + 5
    # 串号：别的节点 ack 不到
    assert st.ack(t_stop["task_id"], node_id="n_other", status=STATUS_DONE) is None
    assert st.ack("t_nope", node_id=nid, status=STATUS_DONE) is None
    assert st.ack(t_ov["task_id"], node_id=nid, status="sent") is None
    assert st.ack(t_ov["task_id"], node_id=nid, status=STATUS_REJECTED, detail="no_instance")["status"] == STATUS_REJECTED


def test_task_ttl_expires_and_cancel(st):
    a = _enroll(st)
    nid = a["node_id"]
    t = st.enqueue(nid, TASK_PING, ttl_sec=30, now=T0)
    assert t["expires_at"] == T0 + 30 and t["ttl_sec"] == 30
    assert st.enqueue(nid, TASK_PING, ttl_sec=1, now=T0)["ttl_sec"] == 10          # clamp 下限
    assert st.enqueue(nid, TASK_PING, ttl_sec="bad", now=T0)["ttl_sec"] == 900     # 缺省
    assert st.pull(nid, now=T0 + 31) and st.get_task(t["task_id"])["status"] == STATUS_EXPIRED
    t2 = st.enqueue(nid, TASK_PING, now=T0 + 40)
    assert st.cancel(t2["task_id"]) and st.get_task(t2["task_id"])["status"] == STATUS_CANCELLED
    assert not st.cancel(t2["task_id"])
    assert len(st.list_tasks(node_id=nid, status=STATUS_EXPIRED)) == 2  # 30s 与 10s 两条都过期


def test_stop_account_supersedes_queued_tasks_for_same_phone(st):
    a = _enroll(st)
    nid = a["node_id"]
    t1 = st.enqueue(nid, "login_qr", target={"phone": "639170000001"}, now=T0)
    t2 = st.enqueue(nid, "login_qr", target={"phone": "639170000002"}, now=T0)
    st.enqueue(nid, TASK_STOP_ACCOUNT, target={"phone": "639170000001"}, now=T0 + 1)
    assert st.get_task(t1["task_id"])["status"] == STATUS_CANCELLED
    assert st.get_task(t2["task_id"])["status"] == STATUS_QUEUED


def test_pull_legacy_proto_only_gets_ping_upgrade(st):
    a = _enroll(st)
    nid = a["node_id"]
    st.enqueue(nid, TASK_PULL_OVERVIEW, now=T0)
    st.enqueue(nid, TASK_UPGRADE, now=T0)
    st.enqueue(nid, TASK_PING, now=T0)
    kinds = [t["kind"] for t in st.pull(nid, node_proto=0, now=T0 + 1)]
    assert sorted(kinds) == sorted([TASK_UPGRADE, TASK_PING])
    assert [t["kind"] for t in st.pull(nid, node_proto=PROTO_VERSION, now=T0 + 2)] == [TASK_PULL_OVERVIEW]


def test_overview_aggregates(st):
    a = _enroll(st, "m-1")
    b = _enroll(st, "m-2", label="机器B", group_name="越南")
    st.heartbeat(a["node_id"], {"accounts": {"total": 3, "online": 2}, "fleet_health": {"by_state": {"active": 2, "banned": 1}}}, now=T0)
    st.heartbeat(b["node_id"], {"accounts": {"total": 4, "online": 4}, "fleet_health": {"by_state": {"active": 4}}}, now=T0 - 500)
    st.enqueue(a["node_id"], TASK_PING, now=T0)
    ov = st.overview(now=T0)
    assert ov["nodes"] == {"total": 2, "online": 1, "offline": 1}
    assert ov["groups"] == {"菲律宾": {"nodes": 1, "online": 1}, "越南": {"nodes": 1, "online": 0}}
    assert ov["accounts"] == {"total": 7, "online": 6} and ov["fleet_health"]["by_state"] == {"active": 6, "banned": 1}
    assert ov["tasks"]["by_status"] == {STATUS_QUEUED: 1}


def test_resolve_fleet_cfg_defaults():
    c = resolve_fleet_cfg({})
    assert c["heartbeat_sec"] == 30 and c["offline_after_sec"] == 120 and c["download"]["installer_url"] == ""
    c2 = resolve_fleet_cfg(SimpleNamespace(config={"fleet_control": {"heartbeat_sec": 10, "public_url": "https://bd2026.cc/fleet",
                                                                     "download": {"version": "1.0.40"}}}))
    assert c2["heartbeat_sec"] == 10 and c2["public_url"] == "https://bd2026.cc/fleet" and c2["download"]["version"] == "1.0.40"


# ── 路由 ─────────────────────────────────────────────────────────────────────

OP_TOKEN = "op-secret"


def _client(st: FleetStore, cfg: Optional[Dict[str, Any]] = None) -> TestClient:
    app = FastAPI()
    set_store(st)

    def _auth(request: Request):
        if request.headers.get("authorization") != f"Bearer {OP_TOKEN}":
            raise HTTPException(status_code=401, detail="op unauthorized")

    def _write(perm):
        assert perm == "fleet_control"
        return _auth

    class _T:
        def TemplateResponse(self, request, name, ctx):
            from fastapi.responses import HTMLResponse
            return HTMLResponse(f"<!--{name}--> {json.dumps(ctx, ensure_ascii=False, default=str)}")

    ctx = SimpleNamespace(config_manager=SimpleNamespace(config=cfg or {"fleet_control": {"public_url": "https://bd2026.cc/fleet"}}),
                          api_auth=_auth, api_write_factory=_write, page_auth=_auth, templates=_T())
    register_routes(app, ctx)
    return TestClient(app)


OP = {"Authorization": f"Bearer {OP_TOKEN}"}


def test_routes_full_loop_enroll_heartbeat_pull_ack(st):
    c = _client(st)
    # 运营侧要鉴权
    assert c.post("/api/fleet/enroll-codes", json={}).status_code == 401
    assert c.get("/api/fleet/nodes").status_code == 401
    code = c.post("/api/fleet/enroll-codes", json={"label": "机器A", "group_name": "菲律宾"}, headers=OP).json()
    assert code["ok"] and code["controller_url"] == "https://bd2026.cc/fleet"
    # 节点注册（无鉴权，凭码）
    assert c.post("/api/fleet/enroll", json={"code": "00000000", "machine_id": "m", "proto_version": 1}).status_code == 403
    assert c.post("/api/fleet/enroll", json={"code": code["code"], "machine_id": "m", "proto_version": 99}).status_code == 426
    r = c.post("/api/fleet/enroll", json={"code": code["code"], "machine_id": "m-A", "host_name": "PC-A",
                                          "proto_version": PROTO_VERSION, "agent_version": "0.1.0"}).json()
    assert r["ok"] and r["heartbeat_sec"] == 30
    nk = {"Authorization": f"Bearer {r['node_key']}"}
    nid = r["node_id"]
    # 注册码也可走 Bearer（核心 CSRF 中间件只放行 Bearer 写请求）
    code2 = c.post("/api/fleet/enroll-codes", json={}, headers=OP).json()["code"]
    r2 = c.post("/api/fleet/enroll", json={"machine_id": "m-B", "proto_version": PROTO_VERSION},
                headers={"Authorization": f"Bearer {code2}"}).json()
    assert r2["ok"] and r2["node_id"] != nid
    # 心跳要 node_key
    assert c.post("/api/fleet/heartbeat", json={}).status_code == 401
    assert c.post("/api/fleet/heartbeat", json={}, headers=OP).status_code == 401
    hb = c.post("/api/fleet/heartbeat", json={"accounts": {"total": 2, "online": 1}, "raw_chat": "x"}, headers=nk).json()
    assert hb["ok"] and hb["has_tasks"] is False and hb["server_proto"] == PROTO_VERSION
    # 运营下任务
    assert c.post(f"/api/fleet/nodes/{nid}/tasks", json={"kind": "bogus"}, headers=OP).status_code == 400
    assert c.post("/api/fleet/nodes/n_ghost/tasks", json={"kind": "ping"}, headers=OP).status_code == 409
    t = c.post(f"/api/fleet/nodes/{nid}/tasks", json={"kind": "ping", "payload": {"echo": "hi"}, "ttl_sec": 60}, headers=OP).json()["task"]
    assert t["status"] == STATUS_QUEUED and t["created_by"] == "operator" and t["ttl_sec"] == 60
    assert c.post("/api/fleet/heartbeat", json={}, headers=nk).json()["has_tasks"] is True
    # 节点领任务
    pulled = c.get("/api/fleet/tasks/pull?limit=5", headers=nk).json()
    assert [x["task_id"] for x in pulled["tasks"]] == [t["task_id"]] and pulled["tasks"][0]["payload"] == {"echo": "hi"}
    assert c.get("/api/fleet/tasks/pull", headers=nk).json()["tasks"] == []
    # ack
    assert c.post("/api/fleet/tasks/ack", json={"task_id": t["task_id"], "status": "sent"}, headers=nk).status_code == 400
    a = c.post("/api/fleet/tasks/ack", json={"task_id": t["task_id"], "status": "done", "result": {"pong": True}}, headers=nk).json()
    assert a == {"ok": True, "known": True, "status": STATUS_DONE}
    assert c.post("/api/fleet/tasks/ack", json={"task_id": "t_nope", "status": "done"}, headers=nk).json()["known"] is False
    # 运营侧查看
    node = c.get(f"/api/fleet/nodes/{nid}", headers=OP).json()["node"]
    assert node["state"] == "online" and node["tasks"][0]["result"] == {"pong": True} and "raw_chat" not in node["last_heartbeat"]
    assert c.get(f"/api/fleet/nodes/{nid}/heartbeats", headers=OP).json()["heartbeats"][0]["accounts_total"] == 0
    assert c.get("/api/fleet/tasks?status=done", headers=OP).json()["tasks"][0]["task_id"] == t["task_id"]
    assert c.get(f"/api/fleet/tasks/{t['task_id']}", headers=OP).json()["task"]["status"] == STATUS_DONE
    assert c.get("/api/fleet/tasks/t_nope", headers=OP).status_code == 404
    ov = c.get("/api/fleet/overview", headers=OP).json()
    assert ov["nodes"]["total"] == 2 and ov["tasks"]["by_status"] == {STATUS_DONE: 1} and "download" in ov
    # 改名 / 吊销
    assert c.post(f"/api/fleet/nodes/{nid}", json={"label": "机器A-改"}, headers=OP).json()["node"]["label"] == "机器A-改"
    assert c.post(f"/api/fleet/nodes/{nid}", json={}, headers=OP).status_code == 404
    assert c.post(f"/api/fleet/nodes/{nid}/revoke", headers=OP).json() == {"ok": True}
    assert c.post("/api/fleet/heartbeat", json={}, headers=nk).status_code == 401
    assert c.post("/api/fleet/nodes/n_ghost/revoke", headers=OP).status_code == 404
    codes = c.get("/api/fleet/enroll-codes?include_used=true", headers=OP).json()["codes"]
    assert all(x["status"] == "used" for x in codes) and {x["used_by_node"] for x in codes} >= {nid}


def test_routes_longpoll_returns_when_task_arrives(st):
    c = _client(st)
    a = _enroll(st)
    nk = {"Authorization": f"Bearer {a['node_key']}"}
    import threading, time as _t

    def _later():
        _t.sleep(1.2)
        st.enqueue(a["node_id"], TASK_PING)

    threading.Thread(target=_later, daemon=True).start()
    t0 = _t.time()
    body = c.get("/api/fleet/tasks/pull?wait=6", headers=nk).json()
    assert [x["kind"] for x in body["tasks"]] == [TASK_PING] and _t.time() - t0 < 5.5


def test_routes_pages(st):
    c = _client(st, cfg={"fleet_control": {"public_url": "https://bd2026.cc/fleet",
                                           "download": {"version": "1.0.40", "installer_url": "https://bd2026.cc/downloads/ChatX-Setup-1.0.40.exe"}}})
    r = c.get("/fleet/")
    assert r.status_code == 200 and "fleet_home.html" in r.text and "ChatX-Setup-1.0.40.exe" in r.text and "bd2026.cc/fleet" in r.text
    assert c.get("/fleet", follow_redirects=False).status_code == 200
    assert c.get("/fleet/console").status_code == 401
    assert "fleet_console.html" in c.get("/fleet/console", headers=OP).text


def test_routes_store_unavailable_is_503(tmp_path):
    set_store(None)
    app = FastAPI()

    def _auth(request: Request):
        return None
    ctx = SimpleNamespace(config_manager=SimpleNamespace(config={}), api_auth=_auth, api_write_factory=lambda p: _auth,
                          page_auth=_auth, templates=None)
    register_routes(app, ctx)
    c = TestClient(app)
    assert c.get("/api/fleet/nodes").status_code == 503
    assert c.post("/api/fleet/enroll", json={"code": "1", "machine_id": "m", "proto_version": 1}).status_code == 503


# ── Agent（进程内端到端：假 HTTP 把主控请求送进 TestClient，本机实例用替身） ───────

class _FakeNet:
    """Agent.http 替身：controller → TestClient；local → 预置响应 / 记录调用。"""

    def __init__(self, client: TestClient, controller="https://ctl.test/fleet"):
        self.client = client
        self.controller = controller
        self.local: Dict[Tuple[str, str], Any] = {}
        self.calls = []

    def __call__(self, method, url, body, headers, timeout):
        if url.startswith(self.controller):
            path = url[len(self.controller):]
            r = self.client.request(method, path, json=body, headers=headers)
            return r.status_code, r.json()
        from urllib.parse import urlsplit
        u = urlsplit(url)
        key = (method, u.path)
        self.calls.append((method, url, body, dict(headers)))
        resp = self.local.get(key)
        if resp is None:
            return 404, {"detail": "not found"}
        if callable(resp):
            resp = resp(body)
        if isinstance(resp, tuple):
            return resp
        return 200, resp


@pytest.fixture
def rig(st, tmp_path, monkeypatch):
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    client = _client(st)
    net = _FakeNet(client)
    cfg = AgentConfig(tmp_path / "state")
    cfg.add_instance("player", "http://127.0.0.1:18797", auth_token="local-tok", domain="player_care")
    agent = NodeAgent(cfg, http=net, app_version="1.0.38")
    return SimpleNamespace(st=st, client=client, net=net, cfg=cfg, agent=agent)


def _op_code(client, **kw):
    return client.post("/api/fleet/enroll-codes", json=kw, headers=OP).json()["code"]


def test_agent_enroll_persists_key_and_machine_id_stable(rig, tmp_path):
    a = rig.agent
    assert a.machine_id.startswith("m-") and len(a.machine_id) == 18
    assert node_machine_id(tmp_path / "state") == a.machine_id
    res = a.enroll(_op_code(rig.client, label="机器A"), controller_url="https://ctl.test/fleet/")
    assert res["label"] == "机器A" and rig.cfg.node_key.startswith("nk_") and rig.cfg.controller_url == "https://ctl.test/fleet"
    saved = json.loads((tmp_path / "state" / "agent.json").read_text(encoding="utf-8"))
    assert saved["node_key"] == rig.cfg.node_key and saved["instances"][0]["name"] == "player"
    with pytest.raises(agent_mod.AgentError):
        a.enroll("00000000")


def test_agent_heartbeat_reduces_local_data_no_chat(rig):
    rig.agent.enroll(_op_code(rig.client), controller_url="https://ctl.test/fleet")
    rig.net.local[("GET", "/api/accounts/fleet-health")] = {
        "ok": True, "total": 3, "lifecycle": {"active": 2, "banned": 1},
        "accounts": [{"platform": "whatsapp", "account_id": "wa-1", "stage": "active", "quota": {"used": 1}, "last_message": "私密"},
                     {"platform": "whatsapp", "account_id": "wa-2", "stage": "active"},
                     {"platform": "telegram", "account_id": "tg-1", "stage": "banned"}],
        "fleet": {"health": "yellow", "by_state": {"green": 2, "red": 1}}, "self_profile": {"big": "blob"},
    }
    rig.net.local[("GET", "/api/player-care/overview")] = {
        "ok": True, "contacts": {"total": 40, "active_7d": 9}, "today": {"inbound": 12, "visible": 2, "gate_hits": 1},
        "gateway": {"status": "ok"}, "conversations": [{"text": "不该上传"}],
    }
    hb = rig.agent.build_heartbeat()
    assert hb["accounts"] == {"total": 3, "online": 2} and hb["fleet_health"]["by_state"] == {"active": 2, "banned": 1}
    assert hb["instances"][0]["up"] is True and hb["instances"][0]["accounts"] == 3
    assert hb["player_overview"]["contacts"] == 40 and hb["player_overview"]["gateway"] == "ok"
    dumped = json.dumps(hb, ensure_ascii=False)
    assert "私密" not in dumped and "不该上传" not in dumped and "blob" not in dumped
    # 本机调用带实例 token
    assert rig.net.calls[0][3]["Authorization"] == "Bearer local-tok"
    res = rig.agent.heartbeat()
    assert res["ok"] and rig.agent.stats["heartbeats"] == 1
    node = rig.st.list_nodes()[0]
    assert node["app_version"] == "1.0.38" and node["last_heartbeat"]["accounts"]["total"] == 3


def test_agent_heartbeat_marks_instance_down(rig):
    rig.agent.enroll(_op_code(rig.client), controller_url="https://ctl.test/fleet")
    hb = rig.agent.build_heartbeat()
    assert hb["instances"][0]["up"] is False and hb["errors"] and hb["accounts"] == {"total": 0, "online": 0}


def test_agent_full_loop_ping_overview_stop_ack(rig):
    rig.agent.enroll(_op_code(rig.client), controller_url="https://ctl.test/fleet")
    nid = rig.cfg.node_id
    rig.net.local[("GET", "/api/accounts/fleet-health")] = {"ok": True, "total": 1, "lifecycle": {"active": 1},
                                                             "accounts": [{"platform": "whatsapp", "account_id": "wa-1", "stage": "active"}]}
    rig.net.local[("GET", "/api/player-care/overview")] = {"ok": True, "contacts": {"total": 7}, "today": {"inbound": 1}, "gateway": {"status": "idle"}}
    rig.net.local[("POST", "/api/player-care/commands")] = lambda body: {"ok": True, "command": {"command_id": "c_1", "kind": body["kind"], "phone": body["phone"]}}
    for kind, extra in ((TASK_PULL_OVERVIEW, {}), (TASK_PING, {"payload": {"echo": "x"}}), (TASK_ACCOUNT_HEALTH, {}),
                        (TASK_STOP_ACCOUNT, {"target": {"phone": "639170000001", "account": "wa-1"}}),
                        ("push_config", {}), ("restart_instance", {}), ("login_qr", {"payload": {"platform": "whatsapp"}})):
        assert rig.client.post(f"/api/fleet/nodes/{nid}/tasks", json={"kind": kind, **extra}, headers=OP).status_code == 200
    rig.net.local[("POST", "/api/platforms/whatsapp/login/start")] = {"ok": True, "login_id": "L1", "status": "pending",
                                                                       "qr_image": "data:image/png;base64,AAA", "qr_url": "wa://x", "secret": "no"}
    out = rig.agent.run_once()
    by_kind = {h["kind"]: h for h in out["tasks"]}
    assert by_kind[TASK_STOP_ACCOUNT]["status"] == STATUS_DONE and out["tasks"][0]["kind"] == TASK_STOP_ACCOUNT  # stop 最先
    assert by_kind[TASK_PING]["status"] == STATUS_DONE and by_kind[TASK_PULL_OVERVIEW]["status"] == STATUS_DONE
    assert by_kind[TASK_ACCOUNT_HEALTH]["status"] == STATUS_DONE
    assert by_kind["push_config"]["status"] == STATUS_REJECTED and by_kind["push_config"]["detail"] == "not_supported_in_agent_v1"
    assert by_kind["restart_instance"]["status"] == STATUS_REJECTED and by_kind["restart_instance"]["detail"] == "no_restart_cmd"
    assert by_kind["login_qr"]["status"] == STATUS_DONE and by_kind["login_qr"]["detail"] == "qr_ready"
    recs = {t["kind"]: t for t in rig.st.list_tasks(node_id=nid)}
    assert recs[TASK_PING]["result"]["pong"] is True and recs[TASK_PING]["result"]["echo"] == "x"
    assert recs[TASK_PULL_OVERVIEW]["result"]["overview"]["contacts"]["total"] == 7
    assert recs[TASK_ACCOUNT_HEALTH]["result"]["accounts"][0]["account_id"] == "wa-1"
    assert recs[TASK_STOP_ACCOUNT]["result"]["command"]["kind"] == "stop"
    assert recs["login_qr"]["result"]["qr_data_url"].startswith("data:image/png") and "secret" not in recs["login_qr"]["result"]
    stop_call = [c for c in rig.net.calls if c[1].endswith("/api/player-care/commands")][0]
    assert stop_call[2] == {"kind": "stop", "phone": "639170000001", "account": "wa-1", "text": "fleet stop"}
    assert rig.st.overview()["tasks"]["by_status"] == {STATUS_DONE: 5, STATUS_REJECTED: 2}
    # 再跑一轮：没任务
    assert rig.agent.run_once()["tasks"] == []


def test_agent_execute_never_raises_and_rejects_expired(rig):
    st_, res, det = rig.agent.execute({"kind": TASK_PULL_OVERVIEW, "task_id": "t", "expires_at": 1.0})
    assert st_ == STATUS_REJECTED and det == "expired_on_arrival"
    rig.net.local[("GET", "/api/player-care/overview")] = (500, {"detail": "boom"})
    st_, res, det = rig.agent.execute({"kind": TASK_PULL_OVERVIEW, "task_id": "t"})
    assert st_ == STATUS_FAILED and "boom" in res["error"]
    st_, _, det = rig.agent.execute({"kind": "weird", "task_id": "t"})
    assert st_ == STATUS_REJECTED and det.startswith("unknown_kind")
    st_, _, det = rig.agent.execute({"kind": TASK_PULL_OVERVIEW, "task_id": "t", "target": {"instance": "nope"}})
    assert st_ == STATUS_REJECTED and det == "no_instance"


def test_agent_revoked_raises_unauthorized_and_loop_stops(rig):
    rig.agent.enroll(_op_code(rig.client), controller_url="https://ctl.test/fleet")
    rig.st.revoke(rig.cfg.node_id)
    with pytest.raises(Unauthorized):
        rig.agent.heartbeat()
    rig.agent.run_forever(sleep=lambda s: None)
    assert rig.agent.revoked and "unauthorized" in rig.agent.last_error


def test_agent_loop_backs_off_on_errors_then_recovers(rig):
    import threading
    rig.agent.enroll(_op_code(rig.client), controller_url="https://ctl.test/fleet")
    good = rig.net
    calls = {"n": 0}

    def flaky(method, url, body, headers, timeout):
        if url.startswith(good.controller):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("network down")
        return good(method, url, body, headers, timeout)

    rig.agent.http = flaky
    stop = threading.Event()
    slept = []

    def _sleep(s):
        slept.append(s)

    rig.st.enqueue(rig.cfg.node_id, TASK_PING)
    orig_pull = rig.agent.pull

    def pull_then_stop(**kw):
        out = orig_pull(wait=0)
        stop.set()
        return out

    rig.agent.pull = pull_then_stop
    rig.agent.run_forever(stop, sleep=_sleep)
    assert slept == [2.0, 4.0] and rig.agent.stats["errors"] == 2 and rig.agent.last_error == ""
    assert rig.st.list_tasks(node_id=rig.cfg.node_id)[0]["status"] == STATUS_DONE


def test_reducers_tolerate_garbage():
    assert reduce_fleet_health(None) == {"total": 0, "online": 0, "lifecycle": {}}
    assert reduce_player_overview("x") == {"contacts": 0, "active_7d": 0, "inbound_today": 0, "visible_today": 0,
                                           "gate_hits_today": 0, "gateway": ""}
    assert reduce_player_overview({"ok": True, "a": 1}, full=True) == {"a": 1}


def test_agent_cli_status_and_add_instance(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "s"))
    assert agent_mod.main(["add-instance", "player=http://127.0.0.1:18797", "--auth-token", "tok", "--domain", "player_care"]) == 0
    assert agent_mod.main(["status"]) == 0
    out = capsys.readouterr().out
    assert '"enrolled": false' in out and '"tok"' not in out and "***" in out and "player_care" in out


def test_agent_cli_help_renders(capsys):
    with pytest.raises(SystemExit) as ei:
        agent_mod.main(["--help"])
    assert ei.value.code == 0
    assert "ProgramData" in capsys.readouterr().out
