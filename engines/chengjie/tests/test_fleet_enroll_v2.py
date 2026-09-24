"""待批准、机房密钥、注册码限速、无实例登记、幻颜只做健康检查。"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from domains.fleet_control.web.routes import register_routes
from src.fleet import agent as agent_mod
from src.fleet.agent import AgentConfig, NodeAgent
from src.fleet.detect import detect_instances, is_loopback_url, sanitize_instances
from src.fleet.protocol import PROTO_VERSION, STATUS_REJECTED, TASK_RESTART_INSTANCE
from src.fleet.store import (
    CODE_FAIL_WINDOW_SEC, FleetStore, resolve_download, set_store,
)

T0 = 1_800_000_000.0
OP_TOKEN = "op-secret"
OP = {"Authorization": f"Bearer {OP_TOKEN}"}
ENGINE = Path(__file__).resolve().parents[1]


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


def _client(st, cfg=None):
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

    ctx = SimpleNamespace(
        config_manager=SimpleNamespace(config=cfg or {"fleet_control": {"public_url": "https://bd2026.cc/fleet"}}),
        api_auth=_auth, api_write_factory=_write, page_auth=_auth, templates=_T())
    register_routes(app, ctx)
    return TestClient(app)


def _pending_body(mid="m-pend", **extra):
    body = {
        "machine_id": mid, "host_name": "PC-NEW", "os": "Windows 11", "proto_version": PROTO_VERSION,
        "agent_version": "0.3.0", "instances": [
            {"name": "chatx", "base_url": "http://127.0.0.1:18799", "domain": "player_care", "role": ""},
            {"name": "avatarhub", "base_url": "http://127.0.0.1:9000", "domain": "avatar_hub", "role": "health"},
            {"name": "nope", "base_url": "http://10.1.1.1:18799", "domain": "player_care", "role": ""},
        ],
    }
    body.update(extra)
    return body


def test_pending_has_no_authority_until_approved_and_claim_window_wipes_key(st):
    c = _client(st)
    hdr = {"Authorization": "Bearer pending", "X-Real-IP": "203.0.113.9"}
    res = c.post("/api/fleet/enroll", json=_pending_body(), headers=hdr)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending" and body["request_id"].startswith("req_") and "node_key" not in body
    rid = body["request_id"]
    again = c.post("/api/fleet/enroll", json=_pending_body(), headers=hdr).json()
    assert again["request_id"] == rid
    listed = c.get("/api/fleet/pending", headers=OP).json()["pending"]
    assert listed[0]["host_name"] == "PC-NEW" and listed[0]["client_ip"] == "203.0.113.9"
    assert listed[0]["os"] == "Windows 11"
    assert [i["name"] for i in listed[0]["instances"]] == ["chatx", "avatarhub"]
    assert "claim_key" not in listed[0] and "node_key" not in listed[0]
    assert c.post("/api/fleet/heartbeat", json={}, headers={"Authorization": f"Bearer {rid}"}).status_code == 401
    assert c.get("/api/fleet/tasks/pull", headers={"Authorization": f"Bearer {rid}"}).status_code == 401
    assert st.enqueue("n_missing", "ping") is None
    approved = c.post(f"/api/fleet/pending/{rid}/approve", json={"group_name": "机房A"}, headers=OP)
    assert approved.status_code == 200 and "node_key" not in approved.json()
    assert c.get("/api/fleet/pending", headers=OP).json()["pending"] == []
    polled = c.post("/api/fleet/enroll/poll", json={"request_id": rid, "machine_id": "m-pend"}).json()
    assert polled["status"] == "active" and polled["node_key"].startswith("nk_") and polled["group_name"] == "机房A"
    key = polled["node_key"]
    again_key = c.post("/api/fleet/enroll/poll", json={"request_id": rid, "machine_id": "m-pend"},
                       headers={"Authorization": f"Bearer {rid}"}).json()
    assert again_key["node_key"] == key
    assert c.post("/api/fleet/heartbeat", json={"host_name": "PC-NEW", "instances": []},
                  headers={"Authorization": f"Bearer {key}"}).status_code == 200
    later = st.poll_pending(rid, "m-pend", now=time.time() + st.claim_window_sec + 5)
    assert later["status"] == "already_claimed" and "node_key" not in later
    leftover = st._conn.execute("SELECT claim_key FROM pending_enrollments WHERE request_id=?", (rid,)).fetchone()
    assert leftover["claim_key"] == ""


def test_pending_reject_expire_and_rate_limit(st):
    c = _client(st)
    rid = c.post("/api/fleet/enroll", json=_pending_body("m-rej"), headers={"Authorization": "Bearer pending"}).json()["request_id"]
    assert c.post(f"/api/fleet/pending/{rid}/reject", headers=OP).json()["status"] == "rejected"
    assert c.post("/api/fleet/enroll/poll", json={"request_id": rid, "machine_id": "m-rej"}).json()["status"] == "rejected"
    assert c.post(f"/api/fleet/pending/{rid}/approve", headers=OP).status_code == 404

    st.request_pending(machine_id="m-exp", proto_version=1, now=T0, ttl_sec=60)
    rows = st.list_pending(now=T0)
    assert any(r["machine_id"] == "m-exp" for r in rows)
    assert all(r["machine_id"] != "m-exp" for r in st.list_pending(now=T0 + 120))
    exp = [r for r in rows if r["machine_id"] == "m-exp"][0]
    assert st.approve_pending(exp["request_id"], now=T0 + 120)["error"] == "expired"

    st.pending_ip_max = 1
    assert st.request_pending(machine_id="m-a", proto_version=1, client_ip="198.51.100.4", now=T0)["ok"]
    assert st.request_pending(machine_id="m-a", proto_version=1, client_ip="198.51.100.4", now=T0 + 1)["status"] == "pending"
    blocked = st.request_pending(machine_id="m-b", proto_version=1, client_ip="198.51.100.4", now=T0 + 2)
    assert blocked["error"] == "rate_limited"
    assert c.post("/api/fleet/heartbeat", json={}, headers={"Authorization": "Bearer nk_nope"}).status_code == 401


def test_one_time_code_stays_8_digits_and_is_rate_limited(st):
    code = st.create_enroll_code(now=T0)["code"]
    assert len(code) == 8 and code.isdigit()
    st.code_fail_max = 1
    assert st.enroll(code="00000000", machine_id="m", proto_version=1, now=T0, client_ip="203.0.113.8")["error"] == "invalid_or_expired_code"
    assert st.enroll(code=code, machine_id="m", proto_version=1, now=T0 + 1, client_ip="203.0.113.8")["error"] == "rate_limited"
    ok = st.enroll(code=code, machine_id="m", proto_version=1, now=T0 + CODE_FAIL_WINDOW_SEC + 5, client_ip="203.0.113.8")
    assert ok["ok"] and ok["node_key"].startswith("nk_")


def test_room_key_uses_expiry_revoke_and_zip_never_logs_plaintext(st, caplog):
    caplog.set_level("INFO")
    c = _client(st)
    minted = c.post("/api/fleet/room-keys", json={"group_name": "机房A", "label": "A", "max_uses": 2, "ttl_hours": 24},
                    headers=OP)
    assert minted.status_code == 200, minted.text
    rec = minted.json()
    token = rec["room_key"]
    assert token.startswith("rk_") and len(token) >= 40
    assert token.encode() not in Path(st.db_path).read_bytes()
    assert token not in caplog.text
    listed = c.get("/api/fleet/room-keys", headers=OP).json()["room_keys"]
    assert listed[0]["key_id"] == rec["key_id"] and "room_key" not in listed[0]
    assert token not in c.get("/fleet/").text

    pack = c.get(f"/fleet/dl/{token}")
    assert pack.status_code == 200
    assert "attachment" in pack.headers["content-disposition"]
    assert pack.headers["cache-control"] == "no-store"
    z = zipfile.ZipFile(io.BytesIO(pack.content))
    assert set(z.namelist()) == {"room.key", "Install.cmd", "Install-Silent.cmd", "README.txt"}
    assert z.read("room.key").decode().strip() == token
    cmd = z.read("Install.cmd").decode()
    assert token not in cmd and token not in z.read("README.txt").decode()
    assert "/VERYSILENT" in z.read("Install-Silent.cmd").decode()

    def redeem(mid, now=None):
        return st.redeem_room_key(token, machine_id=mid, proto_version=1, host_name=mid, now=now, client_ip="203.0.113.7")

    first = redeem("m-1")
    assert first["ok"] and first["node_key"].startswith("nk_") and first["uses_left"] == 1
    second = redeem("m-2")
    assert second["ok"] and second["group_name"] == "机房A" and second["uses_left"] == 0
    assert redeem("m-3")["error"] == "exhausted"
    assert c.get(f"/fleet/dl/{token}").status_code == 404
    assert st.revoke_room_key(rec["key_id"]) is True
    assert redeem("m-4")["error"] == "revoked"
    other = st.create_room_key(group_name="B", max_uses=1, ttl_hours=1, now=T0)
    assert st.redeem_room_key(other["room_key"], machine_id="m-x", proto_version=1, now=T0 + 3600 + 5)["error"] == "expired"
    assert st.revoke_room_key(other["key_id"], now=T0) is True
    live = st.create_room_key(group_name="C", max_uses=3, ttl_hours=5, now=T0)
    assert st.revoke_room_key(live["key_id"], now=T0) is True
    assert st.redeem_room_key(live["room_key"], machine_id="m-y", proto_version=1, now=T0 + 1)["error"] == "revoked"
    assert c.get(f"/fleet/dl/{live['room_key']}").status_code == 404
    assert live["room_key"].encode() not in Path(st.db_path).read_bytes()


def test_agent_pending_then_poll_and_no_instance_fallback(st, tmp_path, monkeypatch):
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    c = _client(st)

    class Net:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, body, headers, timeout):
            self.calls.append((method, url, body))
            path = url.split("/fleet", 1)[-1]
            r = c.request(method, path, json=body, headers=headers)
            return r.status_code, r.json()

    net = Net()
    cfg = AgentConfig(tmp_path / "state")
    agent = NodeAgent(cfg, http=net, app_version="1.0.40")
    monkeypatch.setattr(agent_mod, "detect_instances", lambda **k: [])
    res = agent.enroll("", controller_url="https://ctl.test/fleet", detect=True)
    assert res["status"] == "pending" and cfg.instances == [] and not cfg.node_key
    saved = json.loads((tmp_path / "state" / "agent.json").read_text(encoding="utf-8"))
    assert saved["pending_request_id"].startswith("req_") and not saved.get("node_key")
    assert agent.poll_enrollment()["status"] == "pending"
    rid = saved["pending_request_id"]
    nid = c.post(f"/api/fleet/pending/{rid}/approve", json={}, headers=OP).json()["node_id"]
    got = agent.poll_enrollment()
    assert got["status"] == "active" and got["node_id"] == nid and cfg.node_key.startswith("nk_")
    hb = agent.build_heartbeat()
    assert hb["instances"] == [] and hb["accounts"] == {"total": 0, "online": 0}
    assert agent_mod.AGENT_VERSION == "0.3.0"


def test_detect_chatx_and_avatar_health_only(st, tmp_path, monkeypatch):
    cfg_path = tmp_path / "data" / "config" / "config.local.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("domain: player_care\nweb_admin:\n  port: 18799\n  auth_token: super-secret\n", encoding="utf-8")
    skipped = tmp_path / "data" / "config" / "other" / "config.yaml"
    skipped.parent.mkdir(parents=True)
    skipped.write_text("domain: fleet_control\nweb_admin:\n  port: 1\n", encoding="utf-8")

    def probe(url):
        return url.startswith("http://127.0.0.1:9000/")

    found = detect_instances(probe=probe, config_paths=[cfg_path, skipped], search=False, avatar_url="http://127.0.0.1:9000")
    assert [i["name"] for i in found] == ["chatx", "avatarhub"]
    assert found[0]["base_url"] == "http://127.0.0.1:18799" and found[0]["config_path"].endswith("config.local.yaml")
    assert "super-secret" not in json.dumps(sanitize_instances(found))
    assert found[1]["role"] == "health" and found[1]["domain"] == "avatar_hub"
    assert detect_instances(probe=lambda url: False, config_paths=[], search=False) == []
    assert is_loopback_url("http://127.0.0.1:18799") and not is_loopback_url("http://10.0.0.8:18799")

    c = _client(st)

    class Net:
        def __call__(self, method, url, body, headers, timeout):
            if url.startswith("http://127.0.0.1"):
                from urllib.parse import urlsplit
                if urlsplit(url).path in ("/health", "/api/health"):
                    return 200, {"ok": True}
                return 404, {"detail": "down"}
            path = url.split("/fleet", 1)[-1]
            r = c.request(method, path, json=body, headers=headers)
            return r.status_code, r.json()

    cfg = AgentConfig(tmp_path / "agent-state")
    agent = NodeAgent(cfg, http=Net(), app_version="")
    with pytest.raises(agent_mod.AgentError):
        cfg.add_instance("bad", "http://10.0.0.8:18799")
    monkeypatch.setattr(agent_mod, "detect_instances", lambda **k: found)
    res = agent.enroll("", controller_url="https://ctl.test/fleet", detect=True)
    assert res["status"] == "pending"
    assert [i["name"] for i in cfg.instances] == ["chatx", "avatarhub"]
    hb = agent.build_heartbeat()
    assert hb["instances"][1]["role"] == "health" and hb["instances"][1]["up"] is True
    assert "super-secret" not in json.dumps(hb)
    status, _result, detail = agent.execute({"kind": TASK_RESTART_INSTANCE, "target": {"instance": "avatarhub"}, "payload": {}})
    assert status == STATUS_REJECTED and detail == "health_only"


def test_download_page_installer_and_script_attachment(st):
    c = _client(st, cfg={"fleet_control": {"public_url": "https://bd2026.cc/fleet", "download": {
        "installer_url": "https://bd2026.cc/downloads/fleet/chatx-agent.exe", "manifest_url": ""}}})
    page = c.get("/fleet/")
    assert page.status_code == 200
    assert "ChatXAgentSetup.exe" in page.text
    script = c.get("/fleet/advanced/Install-ChatXAgent.ps1")
    assert script.status_code == 200
    assert "attachment" in script.headers.get("content-disposition", "")
    assert script.content.startswith(b"# Install-ChatXAgent.ps1")
    env = Environment(loader=FileSystemLoader(str(ENGINE / "domains/fleet_control/web/templates")),
                      autoescape=select_autoescape(["html"]))
    html = env.get_template("fleet_home.html").render(
        download={"setup_url": "https://bd2026.cc/downloads/fleet/ChatXAgentSetup.exe", "version": "0.3.0",
                  "installer_url": "https://bd2026.cc/downloads/fleet/chatx-agent.exe"},
        public_url="https://bd2026.cc/fleet", proto_version=1)
    assert "待批准" in html and "/fleet/advanced/Install-ChatXAgent.ps1" in html
    assert html.index("ChatXAgentSetup.exe") < html.index("Install-ChatXAgent.ps1")
    console = (ENGINE / "domains/fleet_control/web/templates/fleet_console.html").read_text(encoding="utf-8")
    assert "待批准" in console and "/api/fleet/room-keys" in console
    dl = resolve_download({"download": {"manifest_url": "", "installer_url": "https://d/chatx-agent.exe"}})
    assert dl["setup_url"] == "https://d/ChatXAgentSetup.exe"


def test_publish_and_deploy_ops_fixes_are_in_the_scripts():
    publish = (ENGINE / "deploy/fleet/publish_agent.ps1").read_text(encoding="utf-8")
    assert "tar -rf $raw config/presets" in publish
    assert "--exclude=config" in publish
    deploy = (ENGINE / "deploy/fleet/deploy_controller.sh").read_text(encoding="utf-8")
    assert "chmod 770" in deploy and 'chgrp "$SVC_USER"' in deploy
    nginx = (ENGINE / "deploy/fleet/nginx-fleet.conf").read_text(encoding="utf-8")
    assert "location ^~ /fleet/dl/" in nginx and "access_log off" in nginx
    iss = (ENGINE / "fleet_agent/setup/ChatXAgent.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=admin" in iss and "/VERYSILENT" in iss and "ROOMKEYFILE" in iss
    assert "rk_" not in iss
    bootstrap = (ENGINE / "fleet_agent/setup/bootstrap.ps1").read_text(encoding="utf-8")
    assert "--detect" in bootstrap and "room.key" in bootstrap
    ps1 = (ENGINE / "fleet_agent/Install-ChatXAgent.ps1").read_text(encoding="utf-8")
    assert "-Code <enroll code> is required" not in ps1
    assert "--detect" in ps1
