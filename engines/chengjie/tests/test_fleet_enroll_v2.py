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
from src.fleet.agent import (
    DEFAULT_HEARTBEAT_SEC, AgentConfig, NodeAgent, migrate_legacy_agent, migrated_agent_data,
)
from src.fleet.detect import detect_instances, is_loopback_url, sanitize_instances
from src.fleet.identity import (
    StateDirLockError, discard_untrusted_secret, fleet_lock_steps, lock_state_dir,
    parent_dir_lock_plan, state_dir_acl_command, state_dir_lock_plan, state_file_trusted,
)
from src.fleet.protocol import PROTO_VERSION, STATUS_REJECTED, TASK_RESTART_INSTANCE
from src.fleet.store import (
    CODE_FAIL_WINDOW_SEC, PENDING_DEFAULT_GROUP, PENDING_IP_MAX, FleetStore, resolve_download, set_store,
)

T0 = 1_800_000_000.0


def _es(tag: str) -> str:
    """Build a test enroll secret at runtime so the source has no one-line token."""
    return "es_" + tag + "-" + "sec" + "ret" + "-0123456789"


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
        "agent_version": "0.3.1", "enroll_secret": _es("pend"),
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

    st.request_pending(machine_id="m-exp", proto_version=1, enroll_secret=_es("expire"), now=T0, ttl_sec=60)
    rows = st.list_pending(now=T0)
    assert any(r["machine_id"] == "m-exp" for r in rows)
    assert all(r["machine_id"] != "m-exp" for r in st.list_pending(now=T0 + 120))
    exp = [r for r in rows if r["machine_id"] == "m-exp"][0]
    assert st.approve_pending(exp["request_id"], now=T0 + 120)["error"] == "expired"

    st.pending_ip_max = 1
    sec_a = _es("machine-a")
    assert st.request_pending(machine_id="m-a", proto_version=1, client_ip="198.51.100.4", enroll_secret=sec_a, now=T0)["ok"]
    assert st.request_pending(machine_id="m-a", proto_version=1, client_ip="198.51.100.4", enroll_secret=sec_a, now=T0 + 1)["status"] == "pending"
    blocked = st.request_pending(machine_id="m-b", proto_version=1, client_ip="198.51.100.4",
                                 enroll_secret=_es("machine-b"), now=T0 + 2)
    assert blocked["error"] == "rate_limited"
    assert c.post("/api/fleet/heartbeat", json={}, headers={"Authorization": "Bearer nk_nope"}).status_code == 401


def test_one_time_code_is_12_crockford_and_is_rate_limited(st):
    code = st.create_enroll_code(now=T0)["code"]
    compact = code.replace("-", "")
    assert len(compact) >= 12 and all(ch in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for ch in compact)
    assert st.create_enroll_code(now=T0)["expires_at"] == T0 + 15 * 60
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
    assert "setowner" in iss and "ResultCode" in iss and "ChatX Fleet Agent Upgrade" in iss
    assert "/IM chatx-agent.exe" not in iss
    assert "PsLiteral" in iss and "WizardSilent" in iss and "ExitProcess" in iss
    assert "$ErrorActionPreference=''Stop''" in iss
    assert "service is installed and will retry" not in iss
    assert "/T /C" not in iss and "Get-ChildItem" not in iss
    assert ".legacy-" in iss
    lock_body = iss.split("procedure LockStateDir")[1].split("procedure CurStepChanged")[0]
    assert lock_body.index("AssertStateParent(Dir)") < lock_body.index("RetireUnlockedFleet(Dir)")
    assert lock_body.index("RetireUnlockedFleet(Dir)") < lock_body.index("ForceDirectories(Dir)")
    assert lock_body.index("/setowner *S-1-5-32-544')") < lock_body.index("/reset')")
    grant = lock_body.index("/inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F')")
    assert grant < lock_body.index("DirIsReparse(Dir)") < lock_body.index("StateDirWasLocked(Dir)")
    assert "ReparsePoint" in iss and "AssertStateParent(Dir)" in iss
    assert "S-1-5-32-545:(OI)(CI)RX" in iss
    parent = iss.split("procedure AssertStateParent")[1].split("function RetireUnlockedFleet")[0]
    assert "& icacls.exe @ia" in parent
    assert "''*S-1-5-18:(OI)(CI)F''" in parent
    assert "''*S-1-5-32-544:(OI)(CI)F''" in parent
    assert "''*S-1-5-32-545:(OI)(CI)RX''" in parent
    assert "/grant:r *S-1-5-18:" not in parent
    step = iss.split("procedure CurStepChanged")[1]
    assert "pair: AnsiString" in step and "LoadStringFromFile" in step
    assert "ForceDirectories" not in step
    assert "-Snapshot" in step and "finally" in step
    assert step.index("ResultCode <> 0") < step.index("WizardSilent") < step.index("ExitProcess(1)")
    bootstrap = (ENGINE / "fleet_agent/setup/bootstrap.ps1").read_text(encoding="utf-8")
    assert "--detect" in bootstrap and "room.key" in bootstrap
    assert "S-1-5-18" in bootstrap and "S-1-5-32-544" in bootstrap
    assert "/setowner" in bootstrap and bootstrap.index("/setowner") < bootstrap.index("enroll', '--controller'")
    assert "NativeExit" in bootstrap and "exit 3" in bootstrap
    assert "ReparsePoint" in bootstrap and "parent owner is not trusted" in bootstrap
    assert "/T" not in bootstrap and "Get-ChildItem" not in bootstrap and "Assert-NoChildReparse" not in bootstrap
    assert ".legacy-" in bootstrap and "finally" in bootstrap
    b_owner = bootstrap.index("@($Dir, '/setowner', '*S-1-5-32-544')")
    b_reset = bootstrap.index("@($Dir, '/reset')")
    b_grant = bootstrap.index("@($Dir, '/inheritance:r', '/grant:r'")
    assert bootstrap.index("Assert-StateParent $Dir") < bootstrap.index("Rename-Item -LiteralPath $Dir") < b_owner
    assert b_owner < b_reset < b_grant
    assert b_grant < bootstrap.index("Test-Reparse $Dir", b_grant) < bootstrap.index("Test-DirLocked $Dir", b_grant)
    assert "S-1-5-32-545:(OI)(CI)RX" in bootstrap and "Test-ParentLocked" in bootstrap
    assert "migrate-legacy" in bootstrap and "--snapshot" in bootstrap
    assert bootstrap.index("migrate-legacy") < bootstrap.index("enroll', '--controller'")
    assert bootstrap.index("enroll failed") < bootstrap.index("install-service")
    ps1 = (ENGINE / "fleet_agent/Install-ChatXAgent.ps1").read_text(encoding="utf-8")
    assert "-Code <enroll code> is required" not in ps1
    assert "keep existing enrollment" not in ps1
    assert "instances are cleared" in ps1 and "restart_cmd must be set again" in ps1
    assert "--detect" in ps1 and "--room-key'" not in ps1 and "-RoomKey " not in ps1
    assert "S-1-5-18" in ps1 and "setowner" in ps1 and "NativeExit" in ps1
    assert "ReparsePoint" in ps1 and "parent owner is not trusted" in ps1
    assert "'/T'" not in ps1 and "Get-ChildItem" not in ps1 and "Assert-NoChildReparse" not in ps1
    assert ".legacy-" in ps1 and "finally" in ps1
    p_owner = ps1.index("@($Dir, '/setowner', '*S-1-5-32-544')")
    p_reset = ps1.index("@($Dir, '/reset')")
    p_grant = ps1.index("@($Dir, '/inheritance:r', '/grant:r'")
    assert ps1.index("Assert-StateParent $Dir") < ps1.index("Rename-Item -LiteralPath $Dir") < p_owner
    assert p_owner < p_reset < p_grant
    assert p_grant < ps1.index("Test-Reparse $Dir", p_grant) < ps1.index("Test-DirLocked $Dir", p_grant)
    assert "S-1-5-32-545:(OI)(CI)RX" in ps1 and "Test-ParentLocked" in ps1
    assert "migrate-legacy" in ps1 and "--snapshot" in ps1
    folder = Path(r"C:\ProgramData\ChatX\fleet")
    acl = state_dir_acl_command(folder)
    assert acl[:3] == ["icacls", str(folder), "/inheritance:r"]
    assert "*S-1-5-18:(OI)(CI)F" in acl and "*S-1-5-32-544:(OI)(CI)F" in acl
    assert "/T" not in acl


def test_pending_secret_separates_requests_and_approve_must_confirm_rotate(st):
    owner = _es("owner")
    attacker = _es("attacker")
    first = st.request_pending(
        machine_id="m-same", host_name="REAL-PC", proto_version=1, enroll_secret=owner,
        os_label="Windows\u202e11", agent_version="0.3.1\x00evil", label="pwn", group_name="root", now=T0)
    second = st.request_pending(
        machine_id="m-same", host_name="FAKE-PC", proto_version=1, enroll_secret=attacker, now=T0)
    assert first["request_id"] != second["request_id"]
    rows = {r["request_id"]: r for r in st.list_pending(now=T0)}
    assert len(rows) == 2
    assert rows[first["request_id"]]["duplicate_machine"] is True
    assert rows[second["request_id"]]["duplicate_machine"] is True
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
        machine_id="m-same", proto_version=1, enroll_secret=_es("third"), now=T0 + 1)
    listed = [r for r in st.list_pending(now=T0 + 1) if r["request_id"] == third["request_id"]][0]
    assert listed["warning"] == f"approving will rotate key of {approved['node_id']}"
    denied = st.approve_pending(third["request_id"], group_name="other", now=T0 + 1)
    assert denied["error"] == "confirm_rotate" and denied["warning"] == listed["warning"]
    assert st.authenticate(claimed["node_key"])["node_id"] == approved["node_id"]
    rotated = st.approve_pending(third["request_id"], group_name="other", confirm_rotate=True, now=T0 + 2)
    assert rotated["ok"] and rotated["group_name"] == "other"
    assert st.authenticate(claimed["node_key"]) is None


def test_unclaimed_key_is_wiped_from_decided_at(st):
    sec = _es("wipe")
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
    sec = _es("divert")
    diverted = st.redeem_room_key(
        other["room_key"], machine_id="m-room", proto_version=1, enroll_secret=sec, now=T0 + 1)
    assert diverted["status"] == "pending" and "node_key" not in diverted and diverted["was_revoked"] is False
    assert st.list_room_keys(now=T0 + 1)[0]["uses"] == 0
    pending = st.list_pending(now=T0 + 1)[0]
    assert pending["requested_group"] == "机房B"
    assert pending["warning"] == f"approving will rotate key of {first['node_id']}"
    st.revoke(first["node_id"])
    same = st.create_room_key(group_name="机房A", max_uses=1, ttl_hours=5, now=T0)
    revived = st.redeem_room_key(
        same["room_key"], machine_id="m-room", proto_version=1,
        enroll_secret=_es("revived"), now=T0 + 2)
    assert revived["status"] == "pending" and revived["revoked_note"] == "this machine was revoked"
    assert [r for r in st.list_pending(now=T0 + 2) if r["request_id"] == revived["request_id"]][0]["was_revoked"] is True
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
            enroll_secret=_es("room" + str(i)), now=T0 + i)
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
    hidden = "rk_" + "super" + "sec" + "ret" + "value"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/1.1" %s',
        ("127.0.0.1", "GET", "/fleet/dl/" + hidden, "200"), None)
    assert RedactFleetDownloadFilter().filter(record) is True
    assert hidden not in record.getMessage()
    assert "/fleet/dl/<redacted>" in record.getMessage()
    assert redact_download_path("GET /fleet/dl/rk_abc HTTP/1.1") == "GET /fleet/dl/<redacted> HTTP/1.1"


def test_state_dir_locked_before_writes(tmp_path):
    lock_state_dir(tmp_path / "fleet")
    assert (tmp_path / "fleet").is_dir()
    assert (tmp_path / "fleet").stat().st_mode & 0o777 == 0o700


def test_state_dir_acl_command_order_closes_the_toctou_window():
    folder = Path(r"C:\ProgramData\ChatX\fleet")
    plan = state_dir_lock_plan(folder)
    names = [name for name, _argv in plan]
    assert names == ["retire-legacy", "setowner-dir", "reset-dir", "grant-dir", "verify-dir"]
    by_name = dict(plan)
    assert by_name["setowner-dir"] == ["icacls", str(folder), "/setowner", "*S-1-5-32-544"]
    assert by_name["reset-dir"] == ["icacls", str(folder), "/reset"]
    assert "/T" not in by_name["reset-dir"] and "*" not in by_name["reset-dir"][1]
    assert by_name["grant-dir"][2:4] == ["/inheritance:r", "/grant:r"]
    assert "/T" not in by_name["grant-dir"]
    assert by_name["verify-dir"] == []
    assert names.index("retire-legacy") < names.index("setowner-dir") < names.index("reset-dir")
    assert names.index("reset-dir") < names.index("grant-dir") < names.index("verify-dir")
    assert fleet_lock_steps(True) == ["verify-dir"]
    assert "reset-dir" not in fleet_lock_steps(True)
    parent = parent_dir_lock_plan(folder.parent)
    parent_names = [name for name, _argv in parent]
    assert parent_names == ["parent-setowner", "parent-reset", "parent-grant", "parent-verify"]
    parent_by = dict(parent)
    assert "/T" not in parent_by["parent-reset"] and "/T" not in parent_by["parent-grant"]
    assert "*S-1-5-32-545:(OI)(CI)RX" in parent_by["parent-grant"]
    assert "*S-1-5-18:(OI)(CI)F" in parent_by["parent-grant"]
    for _name, argv in list(plan) + list(parent):
        assert "/T" not in argv
    assert state_dir_acl_command(folder) == by_name["grant-dir"]
    assert "os.walk" not in Path(state_dir_lock_plan.__code__.co_filename).read_text(encoding="utf-8").split("def state_dir_is_locked")[0]
    assert state_file_trusted(dir_locked=False, owner_sid="S-1-5-32-544") is False
    assert state_file_trusted(dir_locked=True, owner_sid="S-1-5-32-544") is True
    assert state_file_trusted(dir_locked=True, owner_sid="S-1-5-32-545") is False


def test_windows_reparse_uses_file_attributes(monkeypatch):
    import stat

    from src.fleet import identity as ident

    monkeypatch.setattr(ident.os, "name", "nt")

    def lstat_reparse(_path):
        return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)

    monkeypatch.setattr(ident.os, "lstat", lstat_reparse)
    assert ident._is_reparse(Path(r"C:\ProgramData\ChatX\fleet")) is True

    def lstat_dir(_path):
        return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_DIRECTORY)

    monkeypatch.setattr(ident.os, "lstat", lstat_dir)
    assert ident._is_reparse(Path(r"C:\ProgramData\ChatX\fleet")) is False


def test_unlocked_fleet_is_renamed_before_a_clean_dir_is_created(tmp_path):
    from src.fleet import identity as ident

    fleet = tmp_path / "fleet"
    fleet.mkdir()
    fleet.chmod(0o755)
    (fleet / "agent.json").write_text('{"restart_cmd":"calc"}', encoding="utf-8")
    (fleet / "logs").mkdir()
    lock_state_dir(fleet)
    assert fleet.is_dir() and not fleet.is_symlink()
    assert fleet.stat().st_mode & 0o777 == 0o700
    assert list(fleet.iterdir()) == []
    legacies = list(tmp_path.glob("fleet.legacy-*"))
    assert len(legacies) == 1
    assert "restart_cmd" in (legacies[0] / "agent.json").read_text(encoding="utf-8")
    assert (legacies[0] / "logs").is_dir()
    lock_state_dir(fleet)
    assert len(list(tmp_path.glob("fleet.legacy-*"))) == 1
    src = Path(ident.lock_state_dir.__code__.co_filename).read_text(encoding="utf-8")
    assert "os.walk" not in src and "_reject_child_reparse" not in src


def test_reparse_fleet_is_renamed_without_following(tmp_path):
    from src.fleet import identity as ident

    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "secret.txt").write_text("keep", encoding="utf-8")
    fleet = tmp_path / "fleet"
    fleet.symlink_to(target, target_is_directory=True)
    lock_state_dir(fleet)
    assert fleet.is_dir() and not fleet.is_symlink()
    assert not (fleet / "secret.txt").exists()
    assert (target / "secret.txt").read_text(encoding="utf-8") == "keep"
    legacies = list(tmp_path.glob("fleet.legacy-*"))
    assert len(legacies) == 1 and legacies[0].is_symlink()
    with pytest.raises(StateDirLockError):
        ident._require_locked_dir(legacies[0])


def test_reparse_check_fails_closed_when_lstat_errors(monkeypatch):
    from src.fleet import identity as ident

    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(ident.os, "lstat", denied)
    assert ident._is_reparse(Path("/no/such/fleet")) is True

    def missing(_path):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(ident.os, "lstat", missing)
    assert ident._is_reparse(Path("/no/such/fleet")) is False


def test_locked_dir_skips_reset_and_parent_is_post_checked(tmp_path, monkeypatch):
    from pathlib import PosixPath

    from src.fleet import identity as ident

    fleet = tmp_path / "ChatX" / "fleet"
    fleet.parent.mkdir()
    fleet.mkdir()
    monkeypatch.setattr(ident.os, "name", "nt")
    # Path() follows os.name. Keep the temp dir a PosixPath so the skip logic can run here.
    monkeypatch.setattr(ident, "Path", lambda p: p if isinstance(p, PosixPath) else PosixPath(p))
    monkeypatch.setattr(ident, "_is_reparse", lambda _path: False)
    calls = []
    seen = []

    def locked(path, allow_users_rx=False):
        seen.append(allow_users_rx)
        return True

    monkeypatch.setattr(ident, "_windows_dir_locked", locked)
    monkeypatch.setattr(ident, "_run_icacls", lambda argv: calls.append(list(argv)))
    ident.lock_state_dir(fleet)
    assert calls == []
    assert True in seen
    assert ident._users_rx_mask_ok(0x1200A9) is True
    assert ident._users_rx_mask_ok(0x1200A9 | 0x2) is False


def test_migrate_legacy_keeps_only_identity_fields(tmp_path):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    fleet.chmod(0o755)
    secret = _es("mig")
    source = {
        "node_id": "n1",
        "node_key": "nk_keep",
        "heartbeat_sec": 15,
        "enroll_secret": secret,
        "pending_request_id": "req1",
        "pairing_code": "AB2345",
        "controller_url": "https://evil.example/fleet",
        "reenroll_not_before": 9,
        "instances": [{
            "name": "chatx",
            "restart_cmd": "calc",
            "base_url": "http://127.0.0.1:1",
            "auth_token": "tok",
            "config_path": "C:/secret.yaml",
        }],
    }
    planted = fleet / "agent.json"
    planted.write_text(json.dumps(source), encoding="utf-8")
    planted.chmod(0o600)
    out = migrate_legacy_agent(fleet, "https://ctl.test/fleet", source)
    assert out["node_id"] == "n1"
    assert out["node_key"] == "nk_keep"
    assert out["heartbeat_sec"] == 15
    assert out["enroll_secret"] == secret
    assert out["pending_request_id"] == "req1"
    assert out["pairing_code"] == "AB2345"
    assert out["controller_url"] == "https://ctl.test/fleet"
    assert out["instances"] == []
    saved = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    blob = json.dumps(saved)
    assert saved["instances"] == []
    assert "restart_cmd" not in blob
    assert "evil.example" not in blob
    assert "reenroll_not_before" not in saved
    assert "auth_token" not in blob
    assert "config_path" not in blob
    assert fleet.stat().st_mode & 0o777 == 0o700
    legacies = list(fleet.parent.glob("fleet.legacy-*"))
    assert len(legacies) == 1
    old_blob = (legacies[0] / "agent.json").read_text(encoding="utf-8")
    assert "restart_cmd" in old_blob
    assert not any("restart_cmd" in p.read_text(encoding="utf-8") for p in fleet.rglob("*.json"))


def test_migrated_heartbeat_keeps_only_an_int_in_range():
    kept = migrated_agent_data("https://ctl.test/fleet", {"node_id": "n1", "heartbeat_sec": 15})
    assert kept["heartbeat_sec"] == 15
    for bad in ("15", True, False, 1, 0, 3601, 10**9, None, 15.0):
        got = migrated_agent_data("https://ctl.test/fleet", {"node_id": "n1", "heartbeat_sec": bad})
        assert got["heartbeat_sec"] == DEFAULT_HEARTBEAT_SEC


def test_migrate_snapshot_is_removed_and_the_old_file_leaves_fleet(tmp_path, capsys):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    fleet.chmod(0o755)
    old = {
        "node_id": "n1",
        "node_key": "nk_keep",
        "heartbeat_sec": 15,
        "instances": [{"restart_cmd": "calc"}],
    }
    planted = fleet / "agent.json"
    planted.write_text(json.dumps(old), encoding="utf-8")
    planted.chmod(0o600)
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps(old), encoding="utf-8")
    rc = agent_mod.main([
        "--state-dir", str(fleet), "migrate-legacy",
        "--controller", "https://ctl.test/fleet", "--snapshot", str(snap),
    ])
    assert rc == 0
    assert not snap.exists()
    text = capsys.readouterr().out
    assert '"ok": true' in text and "nk_keep" not in text
    saved = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert saved["instances"] == [] and "restart_cmd" not in json.dumps(saved)
    legacies = list(tmp_path.glob("fleet.legacy-*"))
    assert len(legacies) == 1
    assert "restart_cmd" in (legacies[0] / "agent.json").read_text(encoding="utf-8")


def test_migrate_snapshot_is_removed_when_lock_fails(tmp_path, monkeypatch, capsys):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"node_id": "n1", "node_key": "nk_keep"}), encoding="utf-8")

    def boom(_path):
        raise StateDirLockError("denied")

    monkeypatch.setattr(agent_mod, "lock_state_dir", boom)
    rc = agent_mod.main([
        "--state-dir", str(tmp_path / "fleet"), "migrate-legacy",
        "--controller", "https://ctl.test/fleet", "--snapshot", str(snap),
    ])
    assert rc == 1
    assert not snap.exists()
    err = capsys.readouterr().err
    assert "nk_keep" not in err and "Traceback" not in err


def test_poll_restarts_enroll_on_unknown_and_room_key_is_file_only(tmp_path, capsys):
    secret = _es("keep")
    cfg = AgentConfig(tmp_path)
    cfg.data.update({
        "controller_url": "https://ctl.test/fleet",
        "pending_request_id": "req_keep",
        "enroll_secret": secret,
    })
    cfg.save()
    calls = []

    def http(method, url, body, headers, timeout):
        calls.append(url)
        assert body["enroll_secret"] == secret
        if url.rstrip("/").endswith("/api/fleet/enroll"):
            return 200, {"ok": True, "status": "pending", "request_id": "req_new", "pairing_code": "AB2345"}
        return 200, {"ok": False, "status": http.status}

    http.status = "unknown"
    agent = NodeAgent(cfg, http=http)
    assert agent.poll_enrollment()["status"] == "pending"
    assert cfg.data["pending_request_id"] == "req_new"
    assert "reenroll_not_before" not in cfg.data
    assert any(u.rstrip("/").endswith("/api/fleet/enroll") for u in calls)
    cfg.data["pending_request_id"] = "req_keep"
    cfg.data["reenroll_not_before"] = time.time() + 3600
    cfg.save()
    calls.clear()
    http.status = "already_claimed"
    assert agent.poll_enrollment()["status"] == "backoff"
    assert "pending_request_id" not in cfg.data
    assert calls and calls[0].endswith("/poll")
    assert not any(u.rstrip("/").endswith("/api/fleet/enroll") for u in calls)
    with pytest.raises(SystemExit) as exc:
        agent_mod.main(["enroll", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--room-key-file" in text
    import re
    assert re.search(r"--room-key(?!-file)", text) is None


def test_after_reject_no_further_enroll_attempts(tmp_path):
    secret = _es("rej")
    cfg = AgentConfig(tmp_path)
    cfg.data.update({
        "controller_url": "https://ctl.test/fleet",
        "pending_request_id": "req_rej",
        "enroll_secret": secret,
        "reenroll_not_before": 1,
    })
    cfg.save()
    calls = []

    def http(method, url, body, headers, timeout):
        calls.append(url)
        if url.rstrip("/").endswith("/api/fleet/enroll"):
            return 200, {"ok": True, "status": "pending", "request_id": "req_new", "pairing_code": "ABCD23"}
        return 200, {"ok": False, "status": "rejected"}

    agent = NodeAgent(cfg, http=http, clock=lambda: 1_000_000.0)
    assert agent.poll_enrollment()["status"] == "rejected"
    assert cfg.data.get("enroll_rejected") is True
    assert "reenroll_not_before" not in cfg.data
    assert "pending_request_id" not in cfg.data
    calls.clear()
    agent.clock = lambda: 1_000_000.0 + 10_000
    assert agent.poll_enrollment()["status"] == "idle"
    assert calls == []


def test_room_key_outside_state_dir_is_copied_before_the_owner_check(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "room.key"
    outside.write_text("room-key-from-operator\n", encoding="utf-8")
    cfg = AgentConfig(state)
    seen = []
    real = agent_mod.discard_untrusted_secret

    def wrapped(path, owner_sid=None):
        seen.append(Path(path).resolve())
        return real(path, owner_sid=owner_sid)

    monkeypatch.setattr(agent_mod, "discard_untrusted_secret", wrapped)
    assert agent_mod._load_room_key(cfg, outside) == "room-key-from-operator"
    assert seen == [(state / "room.key").resolve()]
    assert not outside.exists()
    assert not (state / "room.key").exists()


def test_status_lock_failure_is_a_friendly_message(tmp_path, monkeypatch, capsys):
    def boom(_path):
        raise StateDirLockError("access denied")

    monkeypatch.setattr("src.fleet.identity.lock_state_dir", boom)
    assert agent_mod.main(["--state-dir", str(tmp_path), "status"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Run as Administrator" in err


def test_unlocked_dir_drops_planted_secrets(tmp_path):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    fleet.chmod(0o755)
    planted = fleet / "agent.json"
    planted.write_text('{"restart_cmd":"calc"}', encoding="utf-8")
    lock_state_dir(fleet)
    assert not planted.exists()
    assert fleet.stat().st_mode & 0o777 == 0o700
    kept = fleet / "machine_id"
    kept.write_text("m-kept\n", encoding="utf-8")
    kept.chmod(0o600)
    lock_state_dir(fleet)
    assert kept.read_text(encoding="utf-8").startswith("m-")


def test_approve_route_returns_409_until_confirm_rotate(st):
    code = st.create_enroll_code(group_name="机房A", now=T0)["code"]
    node = st.enroll(code=code, machine_id="m-http", proto_version=1, host_name="BOX", now=T0)
    sec = _es("http")
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


def test_enroll_code_accepts_case_dashes_aliases_and_legacy_digits(st):
    minted = st.create_enroll_code(label="desk", group_name="机房A", now=T0)
    assert minted["expires_at"] == T0 + 15 * 60
    ok = st.enroll(code=minted["code"].lower(), machine_id="m-case", proto_version=1, now=T0)
    assert ok["status"] == "active" and ok["group_name"] == "机房A"
    st._conn.execute(
        "INSERT INTO enroll_codes(code, label, group_name, created_by, created_at, expires_at) "
        "VALUES ('0123456789AB','','','',?,?)", (T0, T0 + 900))
    st._conn.commit()
    alias = st.enroll(code="ol23-4567-89ab", machine_id="m-alias", proto_version=1, now=T0)
    assert alias["ok"], alias
    st._conn.execute(
        "INSERT INTO enroll_codes(code, label, group_name, created_by, created_at, expires_at) "
        "VALUES ('12345678','','','',?,?)", (T0, T0 + 3600))
    st._conn.commit()
    legacy = st.enroll(code="12345678", machine_id="m-legacy", proto_version=1, now=T0)
    assert legacy["ok"] and legacy["status"] == "active"


def test_code_for_revoked_machine_queues_pending_and_does_not_revive(st):
    code = st.create_enroll_code(group_name="机房A", now=T0)["code"]
    first = st.enroll(code=code, machine_id="m-dead", proto_version=1, now=T0)
    st.revoke(first["node_id"], now=T0)
    fresh = st.create_enroll_code(group_name="机房A", now=T0 + 1)
    missing = st.enroll(code=fresh["code"], machine_id="m-dead", proto_version=1, now=T0 + 1)
    assert missing["error"] == "enroll_secret_required"
    again = st.enroll(code=fresh["code"], machine_id="m-dead", proto_version=1, now=T0 + 1)
    assert again["error"] == "enroll_secret_required"
    sec = _es("revokedq")
    held = st.enroll(code=fresh["code"], machine_id="m-dead", proto_version=1, now=T0 + 2,
                     enroll_secret=sec)
    assert held["status"] == "pending" and held["revoked_note"] == "this machine was revoked"
    assert "node_key" not in held
    assert st.get_node(first["node_id"], now=T0 + 2)["status"] == "revoked"
    row = st.list_pending(now=T0 + 2)[0]
    assert row["was_revoked"] is True and row["revoked_note"] == "this machine was revoked"
    assert row["warning"].startswith("approving will rotate key of ")
    spent = st.enroll(code=fresh["code"], machine_id="m-other", proto_version=1, now=T0 + 3,
                      enroll_secret=sec)
    assert spent["error"] == "invalid_or_expired_code"


def test_enroll_failures_log_a_fail2ban_line(st, caplog):
    caplog.set_level(logging.WARNING)
    c = _client(st)
    bad = c.post("/api/fleet/enroll", json={"code": "00000000", "machine_id": "m-guess", "proto_version": 1},
                 headers={"Authorization": "Bearer 00000000"})
    assert bad.status_code == 403
    assert "fleet enroll_fail ip=testclient reason=bad_code" in caplog.text
    assert "00000000" not in caplog.text.split("fleet enroll_fail")[-1]
    st.code_fail_max = 1
    locked = c.post("/api/fleet/enroll", json={"code": "11111111", "machine_id": "m-guess", "proto_version": 1},
                    headers={"Authorization": "Bearer 11111111"})
    assert locked.status_code == 429
    assert "fleet enroll_fail ip=testclient reason=lockout" in caplog.text
    filt = (ENGINE / "deploy/fleet/fail2ban/filter.d/chatx-fleet-enroll.conf").read_text(encoding="utf-8")
    jail = (ENGINE / "deploy/fleet/fail2ban/jail.d/chatx-fleet-enroll.conf").read_text(encoding="utf-8")
    nginx = (ENGINE / "deploy/fleet/nginx-fleet-enroll-limit.conf").read_text(encoding="utf-8")
    assert "reason=(?:bad_code|bad_room_key|lockout)" in filt
    assert "exhausted" not in filt.split("failregex", 1)[-1].split("\n", 1)[0]
    assert "enabled = false" in jail and "chatx-fleet.service" in jail
    assert "location = /fleet/api/fleet/enroll" in nginx and "location = /fleet/api/fleet/enroll/poll" in nginx
    assert "limit_req zone=fleet_enroll" in nginx and "limit_req zone=fleet_enroll_poll" in nginx
    console = (ENGINE / "domains/fleet_control/web/templates/fleet_console.html").read_text(encoding="utf-8")
    assert "this machine was revoked" in console
    assert "duplicate machine_id" in console
    assert "if(typed===null)return" in console and "if(!g)return" in console
    assert "prompt('分组名称','机房')||''" not in console
    admin_src = (ENGINE / "src/fleet/admin.py").read_text(encoding="utf-8")
    assert "DUPLICATE machine_id" in admin_src
    caplog.clear()
    st.pending_ip_max = 1
    first = c.post("/api/fleet/enroll", json={
        "machine_id": "m-queue-a", "proto_version": 1, "enroll_secret": _es("queue-a")},
        headers={"Authorization": "Bearer pending"})
    assert first.status_code == 200
    queued = c.post("/api/fleet/enroll", json={
        "machine_id": "m-queue-b", "proto_version": 1, "enroll_secret": _es("queue-b")},
        headers={"Authorization": "Bearer pending"})
    assert queued.status_code == 429
    assert "reason=lockout" not in caplog.text
    room = st.create_room_key(group_name="机房A", max_uses=1, ttl_hours=2, now=T0)
    assert st.redeem_room_key(room["room_key"], machine_id="m-used", proto_version=1, now=T0)["ok"]
    caplog.clear()
    exhausted = c.post("/api/fleet/enroll", json={
        "room_key": room["room_key"], "machine_id": "m-used-2", "proto_version": 1,
        "enroll_secret": _es("used")}, headers={"Authorization": "Bearer " + room["room_key"]})
    assert exhausted.status_code == 403 and exhausted.json()["detail"] == "exhausted"
    assert "fleet enroll_fail" not in caplog.text


def test_room_key_requires_a_group_and_empty_group_node_is_not_rotated(st, monkeypatch, capsys):
    from src.fleet import admin as admin_mod

    c = _client(st)
    denied = c.post("/api/fleet/room-keys", json={"group_name": "  ", "max_uses": 5}, headers=OP)
    assert denied.status_code == 400 and denied.json()["detail"] == "group_required"
    assert st.create_room_key(group_name="", now=T0)["error"] == "group_required"
    assert st.list_room_keys(now=T0) == []
    monkeypatch.setenv(admin_mod.ENV_CONTROLLER, "https://ctl.test/fleet")
    monkeypatch.setenv(admin_mod.ENV_TOKEN, "tok")
    assert admin_mod.main(["room-key", "--max-uses", "4"]) == 2
    assert "分组" in capsys.readouterr().err

    code = st.create_enroll_code(group_name="", now=T0)["code"]
    node = st.enroll(code=code, machine_id="m-blank", proto_version=1, now=T0)
    assert node["group_name"] == ""
    minted = st.create_room_key(group_name="temp", max_uses=3, ttl_hours=5, now=T0)
    st._conn.execute("UPDATE room_keys SET group_name='' WHERE key_id=?", (minted["key_id"],))
    st._conn.commit()
    pending = st.redeem_room_key(
        minted["room_key"], machine_id="m-blank", proto_version=1, enroll_secret=_es("blank"), now=T0 + 1)
    assert pending["status"] == "pending" and "node_key" not in pending
    uses = st._conn.execute("SELECT uses FROM room_keys WHERE key_id=?", (minted["key_id"],)).fetchone()
    assert uses["uses"] == 0
    assert st.authenticate(node["node_key"])["status"] == "active"


def test_untrusted_state_file_is_deleted_before_it_can_be_read(tmp_path):
    planted = tmp_path / "agent.json"
    planted.write_text('{"restart_cmd":"calc"}', encoding="utf-8")
    assert discard_untrusted_secret(planted, owner_sid="S-1-5-32-545", dir_locked=True) is True
    assert not planted.exists()
    kept = tmp_path / "room.key"
    kept.write_text("rk-not-used\n", encoding="utf-8")
    assert discard_untrusted_secret(kept, owner_sid="S-1-5-18", dir_locked=True) is False
    assert kept.read_text(encoding="utf-8").startswith("rk-")
    machine = tmp_path / "machine_id"
    machine.write_text("m-planted\n", encoding="utf-8")
    assert discard_untrusted_secret(machine, owner_sid="S-1-5-32-544", dir_locked=True) is False
    assert machine.exists()


def test_read_refused_when_state_dir_is_not_locked(tmp_path):
    planted = tmp_path / "agent.json"
    planted.write_text(
        '{"restart_cmd":"calc","controller_url":"https://evil.example/fleet","node_key":"nk_planted"}',
        encoding="utf-8")
    assert discard_untrusted_secret(planted, owner_sid="S-1-5-32-544", dir_locked=False) is True
    assert not planted.exists()
    cfg = AgentConfig(tmp_path)
    assert cfg.node_key == ""
    assert cfg.data.get("restart_cmd") in (None, "")
    assert "evil.example" not in cfg.controller_url
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(StateDirLockError):
        lock_state_dir(link / "fleet")
