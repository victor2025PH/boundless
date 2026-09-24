"""待批准、机房密钥、注册码限速、无实例登记、幻颜只做健康检查。"""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from domains.fleet_control.web.routes import (
    RedactFleetDownloadFilter, _client_ip, redact_download_path, register_routes,
)
from src.fleet import agent as agent_mod
from src.fleet.agent import AgentConfig, NodeAgent
from src.fleet.detect import detect_instances, is_loopback_url, sanitize_instances
from src.fleet.identity import lock_state_dir, state_dir_acl_command
from src.fleet.protocol import PROTO_VERSION, STATUS_REJECTED, TASK_RESTART_INSTANCE
from src.fleet.store import (
    CODE_FAIL_WINDOW_SEC, PENDING_DEFAULT_GROUP, PENDING_IP_MAX, FleetStore, resolve_download, set_store,
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
        "agent_version": "0.3.1", "enroll_secret": "es_pend-secret-0123456789ab",
        "label": "should-drop", "group_name": "should-drop", "instances": [
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
    sent = _pending_body()
    res = c.post("/api/fleet/enroll", json=sent, headers=hdr)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending" and body["request_id"].startswith("req_") and "node_key" not in body
    assert body["pairing_code"] and "enroll_secret" not in body
    rid = body["request_id"]
    again = c.post("/api/fleet/enroll", json=sent, headers=hdr).json()
    assert again["request_id"] == rid
    listed = c.get("/api/fleet/pending", headers=OP).json()["pending"]
    # TestClient's peer is not loopback, so the spoofed X-Real-IP is ignored.
    assert listed[0]["host_name"] == "PC-NEW" and listed[0]["client_ip"] != "203.0.113.9"
    assert listed[0]["label"] == "" and listed[0]["group_name"] == ""
    assert listed[0]["effective_group"] == PENDING_DEFAULT_GROUP and listed[0]["requested_group"] == ""
    assert listed[0]["os"] == "Windows 11"
    assert [i["name"] for i in listed[0]["instances"]] == ["chatx", "avatarhub"]
    assert "claim_key" not in listed[0] and "node_key" not in listed[0]
    assert c.post("/api/fleet/heartbeat", json={}, headers={"Authorization": f"Bearer {rid}"}).status_code == 401
    assert c.get("/api/fleet/tasks/pull", headers={"Authorization": f"Bearer {rid}"}).status_code == 401
    assert st.enqueue("n_missing", "ping") is None
    approved = c.post(f"/api/fleet/pending/{rid}/approve", json={"group_name": "机房A"}, headers=OP)
    assert approved.status_code == 200 and "node_key" not in approved.json()
    assert c.get("/api/fleet/pending", headers=OP).json()["pending"] == []
    poll_body = {"request_id": rid, "machine_id": "m-pend", "enroll_secret": sent["enroll_secret"]}
    assert c.post("/api/fleet/enroll/poll", json={"request_id": rid, "machine_id": "m-pend"}).json()["status"] == "unknown"
    polled = c.post("/api/fleet/enroll/poll", json=poll_body).json()
    assert polled["status"] == "active" and polled["node_key"].startswith("nk_") and polled["group_name"] == "机房A"
    key = polled["node_key"]
    again_key = c.post("/api/fleet/enroll/poll", json=poll_body,
                       headers={"Authorization": f"Bearer {rid}"}).json()
    assert again_key["node_key"] == key
    assert c.post("/api/fleet/heartbeat", json={"host_name": "PC-NEW", "instances": []},
                  headers={"Authorization": f"Bearer {key}"}).status_code == 200
    later = st.poll_pending(rid, "m-pend", enroll_secret=sent["enroll_secret"], now=time.time() + st.claim_window_sec + 5)
    assert later["status"] == "already_claimed" and "node_key" not in later
    leftover = st._conn.execute("SELECT claim_key FROM pending_enrollments WHERE request_id=?", (rid,)).fetchone()
    assert leftover["claim_key"] == ""


def test_pending_reject_expire_and_rate_limit(st):
    c = _client(st)
    sent = _pending_body("m-rej")
    rid = c.post("/api/fleet/enroll", json=sent, headers={"Authorization": "Bearer pending"}).json()["request_id"]
    assert c.post(f"/api/fleet/pending/{rid}/reject", headers=OP).json()["status"] == "rejected"
    assert c.post("/api/fleet/enroll/poll", json={
        "request_id": rid, "machine_id": "m-rej", "enroll_secret": sent["enroll_secret"]}).json()["status"] == "rejected"
    assert c.post(f"/api/fleet/pending/{rid}/approve", headers=OP).status_code == 404

    st.request_pending(machine_id="m-exp", proto_version=1, enroll_secret="es_expire-secret-0123456789", now=T0, ttl_sec=60)
    rows = st.list_pending(now=T0)
    assert any(r["machine_id"] == "m-exp" for r in rows)
    assert all(r["machine_id"] != "m-exp" for r in st.list_pending(now=T0 + 120))
    exp = [r for r in rows if r["machine_id"] == "m-exp"][0]
    assert st.approve_pending(exp["request_id"], now=T0 + 120)["error"] == "expired"

    st.pending_ip_max = 1
    sec_a = "es_machine-a-secret-0123456789"
    assert st.request_pending(machine_id="m-a", proto_version=1, client_ip="198.51.100.4", enroll_secret=sec_a, now=T0)["ok"]
    assert st.request_pending(machine_id="m-a", proto_version=1, client_ip="198.51.100.4", enroll_secret=sec_a, now=T0 + 1)["status"] == "pending"
    blocked = st.request_pending(machine_id="m-b", proto_version=1, client_ip="198.51.100.4",
                                 enroll_secret="es_machine-b-secret-0123456789", now=T0 + 2)
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
    cmd = z.read("Install.cmd").decode()
    silent = z.read("Install-Silent.cmd").decode()
    assert "/VERYSILENT" in silent
    assert "%RANDOM%" in cmd and "https://" in cmd and "Get-FileHash" in cmd
    assert 'if not exist "%SETUP%"' not in cmd
    assert "setup sha256 is not pinned" in cmd

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
    assert saved["enroll_secret"].startswith("es_") and saved["enroll_secret"].encode() not in Path(st.db_path).read_bytes()
    assert res["pairing_code"] and "enroll_secret" not in res
    assert agent.poll_enrollment()["status"] == "pending"
    rid = saved["pending_request_id"]
    nid = c.post(f"/api/fleet/pending/{rid}/approve", json={}, headers=OP).json()["node_id"]
    got = agent.poll_enrollment()
    assert got["status"] == "active" and got["node_id"] == nid and cfg.node_key.startswith("nk_")
    hb = agent.build_heartbeat()
    assert hb["instances"] == [] and hb["accounts"] == {"total": 0, "online": 0}
    assert agent_mod.AGENT_VERSION == "0.3.1"


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
    assert "ChatXAgentSetup.exe" not in page.text
    assert "尚未发布" in page.text or "setup_url" not in page.text or '"setup_url": ""' in page.text
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
    unpublished = env.get_template("fleet_home.html").render(
        download={"setup_url": "", "installer_url": "https://bd2026.cc/downloads/fleet/chatx-agent.exe", "version": "0.3.1"},
        public_url="https://bd2026.cc/fleet", proto_version=1)
    assert "尚未发布" in unpublished and "<a class=\"btn p\"" not in unpublished
    console = (ENGINE / "domains/fleet_control/web/templates/fleet_console.html").read_text(encoding="utf-8")
    assert "待批准" in console and "/api/fleet/room-keys" in console
    assert "pairing_code" in console and "requested_group" in console and "effective_group" in console
    assert "approving will rotate key of " in console and "confirm_rotate" in console
    dl = resolve_download({"download": {"manifest_url": "", "installer_url": "https://d/chatx-agent.exe"}})
    assert not dl.get("setup_url")


def test_publish_and_deploy_ops_fixes_are_in_the_scripts():
    publish = (ENGINE / "deploy/fleet/publish_agent.ps1").read_text(encoding="utf-8")
    assert "tar -rf $raw config/presets" in publish
    assert "--exclude=config" in publish
    deploy = (ENGINE / "deploy/fleet/deploy_controller.sh").read_text(encoding="utf-8")
    assert "chmod 750" in deploy and "chmod 770" in deploy and 'chown root:"$SVC_USER"' in deploy
    check_body = deploy.split("check()")[1].split("rollback()")[0]
    assert "-X POST" not in check_body and "/api/fleet/overview" in check_body
    unit = (ENGINE / "deploy/fleet/chatx-fleet.service").read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in unit and "/etc/chatx-fleet" not in unit.split("ReadWritePaths=")[1].split("\n")[0]
    nginx = (ENGINE / "deploy/fleet/nginx-fleet.conf").read_text(encoding="utf-8")
    assert "location ^~ /fleet/dl/" in nginx and "access_log off" in nginx
    iss = (ENGINE / "fleet_agent/setup/ChatXAgent.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=admin" in iss and "/VERYSILENT" in iss and "ROOMKEYFILE" in iss
    assert "rk_" not in iss
    assert "PrepareToInstall" in iss and "uninstall-service" in iss and "S-1-5-18" in iss and "REMOVESTATE" in iss
    assert iss.index("LockStateDir") < iss.index("FileCopy")
    bootstrap = (ENGINE / "fleet_agent/setup/bootstrap.ps1").read_text(encoding="utf-8")
    assert "--detect" in bootstrap and "room.key" in bootstrap
    assert "S-1-5-18" in bootstrap and "S-1-5-32-544" in bootstrap
    assert bootstrap.index("enroll failed") < bootstrap.index("install-service")
    ps1 = (ENGINE / "fleet_agent/Install-ChatXAgent.ps1").read_text(encoding="utf-8")
    assert "-Code <enroll code> is required" not in ps1
    assert "--detect" in ps1 and "--room-key'" not in ps1 and "-RoomKey " not in ps1
    assert "S-1-5-18" in ps1
    acl = state_dir_acl_command(Path(r"C:\ProgramData\ChatX\fleet"))
    assert acl[:3] == ["icacls", r"C:\ProgramData\ChatX\fleet", "/inheritance:r"]
    assert "*S-1-5-18:(OI)(CI)F" in acl and "*S-1-5-32-544:(OI)(CI)F" in acl


def test_pending_secret_separates_requests_and_approve_must_confirm_rotate(st):
    owner = "es_owner-secret-0123456789abcd"
    attacker = "es_attacker-secret-0123456789"
    first = st.request_pending(
        machine_id="m-same", host_name="REAL-PC", proto_version=1, enroll_secret=owner,
        os_label="Windows\u202e11", agent_version="0.3.1\x00evil", label="pwn", group_name="root", now=T0)
    second = st.request_pending(
        machine_id="m-same", host_name="FAKE-PC", proto_version=1, enroll_secret=attacker, now=T0)
    assert first["request_id"] != second["request_id"]
    rows = {r["request_id"]: r for r in st.list_pending(now=T0)}
    assert len(rows) == 2
    real = rows[first["request_id"]]
    assert real["host_name"] == "REAL-PC" and real["os"] == "Windows11" and "\x00" not in real["agent_version"]
    assert real["label"] == "" and real["group_name"] == "" and real["effective_group"] == PENDING_DEFAULT_GROUP
    assert len(real["pairing_code"]) == 6
    assert owner.encode() not in Path(st.db_path).read_bytes()
    assert st.poll_pending(first["request_id"], "m-same", enroll_secret=attacker, now=T0)["status"] == "unknown"
    approved = st.approve_pending(first["request_id"], now=T0)
    assert approved["group_name"] == PENDING_DEFAULT_GROUP
    assert st.poll_pending(first["request_id"], "m-same", enroll_secret=attacker, now=T0)["status"] == "unknown"
    claimed = st.poll_pending(first["request_id"], "m-same", enroll_secret=owner, now=T0)
    assert claimed["node_key"].startswith("nk_")
    third = st.request_pending(
        machine_id="m-same", proto_version=1, enroll_secret="es_third-secret-0123456789abcd", now=T0 + 1)
    listed = [r for r in st.list_pending(now=T0 + 1) if r["request_id"] == third["request_id"]][0]
    assert listed["warning"] == f"approving will rotate key of {approved['node_id']}"
    denied = st.approve_pending(third["request_id"], group_name="other", now=T0 + 1)
    assert denied["error"] == "confirm_rotate" and denied["warning"] == listed["warning"]
    assert st.authenticate(claimed["node_key"])["node_id"] == approved["node_id"]
    rotated = st.approve_pending(third["request_id"], group_name="other", confirm_rotate=True, now=T0 + 2)
    assert rotated["ok"] and rotated["group_name"] == "other"
    assert st.authenticate(claimed["node_key"]) is None


def test_unclaimed_key_is_wiped_from_decided_at(st):
    sec = "es_wipe-secret-0123456789abcd"
    req = st.request_pending(machine_id="m-wipe", proto_version=1, enroll_secret=sec, now=T0)
    assert st.approve_pending(req["request_id"], now=T0)["ok"]
    st.list_pending(now=T0 + st.claim_window_sec + 5)
    leftover = st._conn.execute(
        "SELECT claim_key FROM pending_enrollments WHERE request_id=?", (req["request_id"],)).fetchone()
    assert leftover["claim_key"] == ""
    later = st.poll_pending(req["request_id"], "m-wipe", enroll_secret=sec, now=T0 + st.claim_window_sec + 6)
    assert later["status"] == "already_claimed"


def test_room_key_pending_for_revoked_or_other_group(st):
    code = st.create_enroll_code(group_name="机房A", now=T0)["code"]
    first = st.enroll(code=code, machine_id="m-room", proto_version=1, now=T0)
    other = st.create_room_key(group_name="机房B", max_uses=2, ttl_hours=5, now=T0)
    sec = "es_room-divert-secret-0123456"
    diverted = st.redeem_room_key(
        other["room_key"], machine_id="m-room", proto_version=1, enroll_secret=sec, now=T0 + 1)
    assert diverted["status"] == "pending" and "node_key" not in diverted
    assert st.list_room_keys(now=T0 + 1)[0]["uses"] == 0
    pending = st.list_pending(now=T0 + 1)[0]
    assert pending["requested_group"] == "机房B"
    assert pending["warning"] == f"approving will rotate key of {first['node_id']}"
    st.revoke(first["node_id"])
    same = st.create_room_key(group_name="机房A", max_uses=1, ttl_hours=5, now=T0)
    revived = st.redeem_room_key(
        same["room_key"], machine_id="m-room", proto_version=1,
        enroll_secret="es_revived-secret-0123456789", now=T0 + 2)
    assert revived["status"] == "pending"
    keys = {k["key_id"]: k for k in st.list_room_keys(now=T0 + 2)}
    assert keys[same["key_id"]]["uses"] == 0
    live_code = st.create_enroll_code(group_name="机房C", now=T0)["code"]
    live = st.enroll(code=live_code, machine_id="m-live", proto_version=1, now=T0)
    room = st.create_room_key(group_name="机房C", max_uses=1, ttl_hours=5, now=T0)
    got = st.redeem_room_key(room["room_key"], machine_id="m-live", proto_version=1, now=T0 + 3)
    assert got["ok"] and got["node_key"] != live["node_key"]
    assert st.authenticate(live["node_key"]) is None
    assert st.authenticate(got["node_key"])["group_name"] == "机房C"


def test_shared_egress_ip_can_enroll_a_room(st):
    assert PENDING_IP_MAX >= 64
    for i in range(9):
        res = st.request_pending(
            machine_id=f"m-room-{i}", proto_version=1, client_ip="198.51.100.20",
            enroll_secret=f"es_room-{i}-secret-0123456789", now=T0 + i)
        assert res["ok"], res


def test_forwarded_ip_only_from_loopback_and_download_log_is_redacted():
    class _Req:
        def __init__(self, host, headers):
            self.client = SimpleNamespace(host=host)
            self.headers = headers

    hdr = {"x-real-ip": "203.0.113.9", "x-forwarded-for": "198.51.100.8"}
    assert _client_ip(_Req("127.0.0.1", hdr)) == "203.0.113.9"
    assert _client_ip(_Req("::1", {"x-forwarded-for": "198.51.100.8"})) == "198.51.100.8"
    assert _client_ip(_Req("203.0.113.50", hdr)) == "203.0.113.50"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/1.1" %s',
        ("127.0.0.1", "GET", "/fleet/dl/rk_supersecretvalue", "200"), None)
    assert RedactFleetDownloadFilter().filter(record) is True
    assert "rk_supersecretvalue" not in record.getMessage()
    assert "/fleet/dl/<redacted>" in record.getMessage()
    assert redact_download_path("GET /fleet/dl/rk_abc HTTP/1.1") == "GET /fleet/dl/<redacted> HTTP/1.1"


def test_state_dir_locked_before_writes(tmp_path):
    lock_state_dir(tmp_path / "fleet")
    assert (tmp_path / "fleet").is_dir()
    assert (tmp_path / "fleet").stat().st_mode & 0o777 == 0o700


def test_poll_keeps_pending_id_on_unknown_and_room_key_is_file_only(tmp_path, capsys):
    cfg = AgentConfig(tmp_path)
    cfg.data.update({
        "controller_url": "https://ctl.test/fleet",
        "pending_request_id": "req_keep",
        "enroll_secret": "es_keep-secret-0123456789",
    })
    cfg.save()

    def http(method, url, body, headers, timeout):
        assert body["enroll_secret"] == "es_keep-secret-0123456789"
        return 200, {"ok": False, "status": http.status}

    http.status = "unknown"
    agent = NodeAgent(cfg, http=http)
    assert agent.poll_enrollment()["status"] == "unknown"
    assert cfg.data["pending_request_id"] == "req_keep"
    http.status = "already_claimed"
    assert agent.poll_enrollment()["status"] == "already_claimed"
    assert cfg.data["pending_request_id"] == "req_keep"
    with pytest.raises(SystemExit) as exc:
        agent_mod.main(["enroll", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--room-key-file" in text
    import re
    assert re.search(r"--room-key(?!-file)", text) is None


def test_approve_route_returns_409_until_confirm_rotate(st):
    code = st.create_enroll_code(group_name="机房A", now=T0)["code"]
    node = st.enroll(code=code, machine_id="m-http", proto_version=1, host_name="BOX", now=T0)
    sec = "es_http-secret-0123456789ab"
    req = st.request_pending(machine_id="m-http", proto_version=1, enroll_secret=sec, host_name="BOX", now=T0 + 1)
    c = _client(st)
    denied = c.post(f"/api/fleet/pending/{req['request_id']}/approve", json={"group_name": "机房A"}, headers=OP)
    assert denied.status_code == 409
    assert denied.json()["detail"] == f"approving will rotate key of {node['node_id']}"
    assert st.authenticate(node["node_key"]) is not None
    ok = c.post(
        f"/api/fleet/pending/{req['request_id']}/approve",
        json={"group_name": "机房A", "confirm_rotate": True}, headers=OP)
    assert ok.status_code == 200 and "node_key" not in ok.json()
    assert st.authenticate(node["node_key"]) is None
