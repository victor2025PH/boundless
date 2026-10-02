"""Console QR-code login (集中扫码): controller routes, agent checks, console page.

Design: docs/FLEET_CONSOLE_QR_LOGIN.md
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.fleet.login_qr import (
    LOGIN_QR_TTL_SEC, LOGIN_STATUS_TTL_SEC, safe_qr_data_url, sanitize_login_result, start_payload, strip_qr,
    valid_login_id, valid_platform,
)
from src.fleet.protocol import STATUS_DONE, STATUS_REJECTED, TASK_LOGIN_QR, TASK_LOGIN_STATUS
from tests.test_fleet_control import OP, _clean, _op_code, rig, st  # noqa: F401  (fixtures)

ENGINE = Path(__file__).resolve().parents[1]
CONSOLE = ENGINE / "domains/fleet_control/web/templates/fleet_console.html"
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def _enrolled(rig):
    rig.agent.enroll(_op_code(rig.client), controller_url="https://ctl.test/fleet")
    return rig.cfg.node_id


# ── helpers ─────────────────────────────────────────────────────────────────
def test_qr_data_url_only_base64_raster():
    assert safe_qr_data_url(PNG) == PNG
    assert safe_qr_data_url("data:image/jpeg;base64,/9j/4AAQ") == "data:image/jpeg;base64,/9j/4AAQ"
    for bad in ("javascript:alert(1)", "https://evil.example/qr.png", "data:image/svg+xml;base64,PHN2Zz4=",
                "data:text/html;base64,PGgxPg==", 'data:image/png;base64,AA" onerror="x', "",
                "data:image/png;base64," + "A" * (256 * 1024)):
        assert safe_qr_data_url(bad) == "", bad


def test_platform_and_login_id_shapes():
    assert valid_platform("WhatsApp") == "whatsapp" and valid_platform("line") == "line"
    for bad in ("../x", "whatsapp/login", "a", "", "x" * 30, "wa?x=1", "1abc"):
        assert valid_platform(bad) == "", bad
    assert valid_login_id("L1_ab-9.x:2") == "L1_ab-9.x:2"
    for bad in ("../../api/x", "a/b", "", "a b", "x" * 97, "id?x=1"):
        assert valid_login_id(bad) == "", bad


def test_start_payload_whitelist_and_result_sanitize():
    assert start_payload({"account_id": "a1", "use_fingerprint": 1, "restart_cmd": "calc", "proxy_id": ""}) == {
        "account_id": "a1", "use_fingerprint": True}
    out = sanitize_login_result({"qr_data_url": "javascript:x", "login_id": "../x", "status": "pending", "n": 1})
    assert out == {"qr_data_url": "", "login_id": "", "status": "pending", "n": 1}
    assert strip_qr({"qr_data_url": PNG, "login_id": "L1"}) == {"qr_data_url": "", "has_qr": True, "login_id": "L1"}
    assert strip_qr({"pong": True}) == {"pong": True}


# ── controller routes ───────────────────────────────────────────────────────
def test_login_qr_route_enqueues_whitelisted_task(rig):
    nid = _enrolled(rig)
    r = rig.client.post(f"/api/fleet/nodes/{nid}/login-qr", headers=OP, json={
        "platform": "Telegram", "instance": "player", "proxy_id": "px1", "restart_cmd": "calc"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["kind"] == TASK_LOGIN_QR
    assert task["target"] == {"platform": "telegram", "instance": "player"}
    assert task["payload"] == {"platform": "telegram", "proxy_id": "px1"}
    assert task["expires_at"] - task["created_at"] == pytest.approx(LOGIN_QR_TTL_SEC)


@pytest.mark.parametrize("body", [{"platform": "../etc"}, {}, {"platform": "whatsapp", "instance": "a/b"}])
def test_login_qr_route_rejects_bad_input(rig, body):
    nid = _enrolled(rig)
    assert rig.client.post(f"/api/fleet/nodes/{nid}/login-qr", headers=OP, json=body).status_code == 400
    assert rig.st.list_tasks(node_id=nid) == []


def test_login_qr_routes_need_operator_and_live_node(rig):
    nid = _enrolled(rig)
    assert rig.client.post(f"/api/fleet/nodes/{nid}/login-qr", json={"platform": "whatsapp"}).status_code == 401
    assert rig.client.post(f"/api/fleet/nodes/{nid}/login-qr/L1/status",
                           json={"platform": "whatsapp"}).status_code == 401
    assert rig.client.post("/api/fleet/nodes/n_nope/login-qr", headers=OP,
                           json={"platform": "whatsapp"}).status_code == 409


def test_status_route_enqueues_login_status(rig):
    nid = _enrolled(rig)
    r = rig.client.post(f"/api/fleet/nodes/{nid}/login-qr/L1/status", headers=OP, json={"platform": "whatsapp"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["kind"] == TASK_LOGIN_STATUS and task["payload"] == {"platform": "whatsapp", "login_id": "L1"}
    assert task["expires_at"] - task["created_at"] == pytest.approx(LOGIN_STATUS_TTL_SEC)
    assert rig.client.post(f"/api/fleet/nodes/{nid}/login-qr/a%20b/status", headers=OP,
                           json={"platform": "whatsapp"}).status_code == 400



def test_status_route_dedupes_waiting_probe(rig):
    nid = _enrolled(rig)
    url = f"/api/fleet/nodes/{nid}/login-qr/L1/status"
    first = rig.client.post(url, headers=OP, json={"platform": "whatsapp"}).json()
    again = rig.client.post(url, headers=OP, json={"platform": "whatsapp"}).json()
    assert again["task"]["task_id"] == first["task"]["task_id"] and again["deduped"] is True
    other = rig.client.post(f"/api/fleet/nodes/{nid}/login-qr/L2/status", headers=OP, json={"platform": "whatsapp"}).json()
    assert other["task"]["task_id"] != first["task"]["task_id"] and "deduped" not in other
    tg = rig.client.post(url, headers=OP, json={"platform": "telegram"}).json()
    assert tg["task"]["task_id"] != first["task"]["task_id"]
    rig.client.get("/api/fleet/tasks/pull", headers={"Authorization": f"Bearer {rig.cfg.node_key}"})
    pulled = rig.client.post(url, headers=OP, json={"platform": "whatsapp"}).json()     # pulled, not acked yet
    assert pulled["task"]["task_id"] == first["task"]["task_id"]
    rig.client.post("/api/fleet/tasks/ack", headers={"Authorization": f"Bearer {rig.cfg.node_key}"},
                    json={"task_id": first["task"]["task_id"], "status": "done", "result": {"status": "pending"}})
    fresh = rig.client.post(url, headers=OP, json={"platform": "whatsapp"}).json()     # finished → new probe
    assert fresh["task"]["task_id"] != first["task"]["task_id"] and "deduped" not in fresh
    kinds = [t["kind"] for t in rig.st.list_tasks(node_id=nid)]
    assert kinds.count(TASK_LOGIN_STATUS) == 4

# ── end to end through the agent ────────────────────────────────────────────
def test_full_qr_login_loop_and_qr_only_on_task_detail(rig):
    nid = _enrolled(rig)
    starts = []
    rig.net.local[("POST", "/api/platforms/whatsapp/login/start")] = lambda body: (
        starts.append(body) or {"ok": True, "login_id": "L1", "status": "pending", "qr_image": PNG, "secret": "no"})
    tid = rig.client.post(f"/api/fleet/nodes/{nid}/login-qr", headers=OP,
                          json={"platform": "whatsapp", "account_id": "wa-9"}).json()["task"]["task_id"]
    rig.agent.run_once()
    assert starts == [{"account_id": "wa-9"}]          # whitelisted options only
    got = rig.client.get(f"/api/fleet/tasks/{tid}", headers=OP).json()["task"]
    assert got["status"] == STATUS_DONE and got["detail"] == "qr_ready"
    assert got["result"]["login_id"] == "L1" and got["result"]["qr_data_url"] == PNG
    assert "secret" not in got["result"]
    listed = [t for t in rig.client.get("/api/fleet/tasks", headers=OP).json()["tasks"] if t["task_id"] == tid][0]
    assert listed["result"]["qr_data_url"] == "" and listed["result"]["has_qr"] is True

    rig.net.local[("GET", "/api/platforms/whatsapp/login/L1/status")] = {
        "ok": True, "status": "authorized", "account_id": "wa-9", "reason_code": ""}
    sid = rig.client.post(f"/api/fleet/nodes/{nid}/login-qr/L1/status", headers=OP,
                          json={"platform": "whatsapp"}).json()["task"]["task_id"]
    rig.agent.run_once()
    res = rig.client.get(f"/api/fleet/tasks/{sid}", headers=OP).json()["task"]
    assert res["status"] == STATUS_DONE and res["result"]["status"] == "authorized"
    assert res["result"]["account_id"] == "wa-9" and res["result"]["login_id"] == "L1"


def test_controller_drops_unsafe_qr_from_node_ack(rig):
    """Even an old / tampered agent cannot get a non-image URL into the console <img>."""
    nid = _enrolled(rig)
    tid = rig.client.post(f"/api/fleet/nodes/{nid}/login-qr", headers=OP,
                          json={"platform": "whatsapp"}).json()["task"]["task_id"]
    pulled = rig.client.get("/api/fleet/tasks/pull", headers={"Authorization": f"Bearer {rig.cfg.node_key}"})
    assert [t["task_id"] for t in pulled.json()["tasks"]] == [tid]
    ack = rig.client.post("/api/fleet/tasks/ack", headers={"Authorization": f"Bearer {rig.cfg.node_key}"}, json={
        "task_id": tid, "status": "done", "result": {"login_id": "L1", "qr_data_url": "javascript:alert(1)"}})
    assert ack.status_code == 200
    stored = rig.st.get_task(tid)["result"]
    assert stored == {"login_id": "L1", "qr_data_url": ""}


def test_agent_refuses_path_tricks_without_local_call(rig):
    _enrolled(rig)
    before = len(rig.net.calls)
    st1, _, d1 = rig.agent.execute({"task_id": "t1", "kind": TASK_LOGIN_QR, "payload": {"platform": "../../api/x"}})
    st2, _, d2 = rig.agent.execute({"task_id": "t2", "kind": TASK_LOGIN_STATUS,
                                    "payload": {"platform": "whatsapp", "login_id": "../../admin"}})
    assert (st1, d1) == (STATUS_REJECTED, "bad_platform")
    assert (st2, d2) == (STATUS_REJECTED, "no_instance_or_login_id")
    assert len(rig.net.calls) == before


def test_agent_drops_non_image_qr_and_keeps_reason_code(rig):
    _enrolled(rig)
    rig.net.local[("POST", "/api/platforms/telegram/login/start")] = {
        "ok": True, "login_id": "T1", "status": "pending", "qr_image": "https://evil.example/q.png"}
    st1, res1, d1 = rig.agent.execute({"task_id": "t1", "kind": TASK_LOGIN_QR,
                                       "target": {"platform": "telegram"}, "payload": {}})
    assert st1 == STATUS_DONE and res1["qr_data_url"] == "" and d1 == "started" and res1["platform"] == "telegram"
    rig.net.local[("GET", "/api/platforms/telegram/login/T1/status")] = {
        "ok": True, "status": "failed", "reason_code": "flood_wait", "retry_after_sec": 30}
    st2, res2, d2 = rig.agent.execute({"task_id": "t2", "kind": TASK_LOGIN_STATUS,
                                       "payload": {"platform": "telegram", "login_id": "T1"}})
    assert st2 == STATUS_DONE and d2 == "failed"
    assert res2["reason_code"] == "flood_wait" and res2["retry_after_sec"] == 30


# ── console page ────────────────────────────────────────────────────────────
def test_console_has_qr_login_panel_and_guards():
    html = CONSOLE.read_text(encoding="utf-8")
    assert 'id="fc-login"' in html and 'id="fc-login-qr"' in html and "扫码登录" in html
    assert "/login-qr'" in html and "/login-qr/'+encodeURIComponent(s.loginId)+'/status'" in html
    # the raw login_qr button (prompt only, no polling) is replaced by the panel
    assert "'login_qr'" not in html.split("var KINDS=")[1].split(";")[0]
    # every <img src> assignment goes through the same data-URL check as the server
    assert "QR_RE.test(u)" in html and "r.qr_data_url&&QR_RE.test(r.qr_data_url)" in html
    js_re = re.search(r"var QR_RE=/(.+?)/;", html).group(1).replace("\\/", "/")
    assert js_re == r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$"
    assert "LOGIN_MAX_MS=300000" in html and "authorized:1" in html
    assert "/cancel'" in html                  # stopping cancels still-queued tasks


# ── console flow, executed for real in node (skipped when node is missing) ──
_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==';
const els = {}, srcs = [], calls = [];
function el(id) {
  if (!els[id]) {
    const e = {id, textContent: '', innerHTML: '', value: '', disabled: false, style: {}, attrs: {}, handlers: {},
      setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] ?? null; },
      removeAttribute(k) { delete this.attrs[k]; if (k === 'src') this._src = undefined; },
      addEventListener(ev, fn) { this.handlers[ev] = fn; }, scrollIntoView() {}, focus() {}, select() {}};
    Object.defineProperty(e, 'src', {get() { return this._src; }, set(v) { this._src = v; srcs.push(v); }});
    els[id] = e;
  }
  return els[id];
}
global.document = {getElementById: el};
global.alert = () => {}; global.confirm = () => true; global.prompt = () => '';
global.setInterval = () => 0; global.clearTimeout = () => {};
global.setTimeout = (fn) => { setImmediate(fn); return 1; };
const script = JSON.parse(process.argv[3]);
const seen = {};
function reply(data) { return Promise.resolve({ok: true, json: () => Promise.resolve(data)}); }
global.apiFetch = (url, opt) => {
  calls.push((opt && opt.method || 'GET') + ' ' + url);
  if (url.startsWith('/api/fleet/overview')) return reply({});
  if (url.startsWith('/api/fleet/nodes?')) return reply({nodes: [{node_id: 'n1', status: 'active', state: 'online',
    label: 'PC-1', last_heartbeat: {instances: [{name: 'player', up: true}, {name: 'hub', role: 'health', up: true}]}}]});
  if (url.startsWith('/api/fleet/tasks?') || url === '/api/fleet/pending' || url === '/api/fleet/room-keys')
    return reply({tasks: [], pending: [], room_keys: []});
  if (url === '/api/fleet/nodes/n1/login-qr') return reply({task: {task_id: 'q1'}});
  let m = url.match(/^\/api\/fleet\/nodes\/n1\/login-qr\/([^/]+)\/status$/);
  if (m) { seen.st = (seen.st || 0) + 1; return reply({task: {task_id: 's' + seen.st, login: m[1]}}); }
  m = url.match(/^\/api\/fleet\/tasks\/([^/]+)$/);
  if (m) {
    const id = m[1]; seen[id] = (seen[id] || 0) + 1;
    const steps = script[id] || script['s*'];
    const x = steps[Math.min(seen[id], steps.length) - 1];
    return reply({task: x});
  }
  if (url.endsWith('/cancel')) return reply({ok: true});
  return Promise.resolve({ok: false, status: 404, json: () => Promise.resolve({detail: 'nf ' + url})});
};
eval(js);
const btn = {getAttribute: (k) => ({'data-n': 'n1', 'data-k': '__login'})[k]};
setImmediate(() => {
  el('fc-list').handlers.click({target: {closest: () => btn}});
  el('fc-login-inst').value = 'player';
  el('fc-login-platform').value = 'WhatsApp';
  el('fc-login-go').handlers.click();
});
let printed = false;
process.on('beforeExit', () => {      // fires once the flow has no timer / promise left
  if (printed) return; printed = true;
  console.log(JSON.stringify({msg: el('fc-login-msg').textContent, srcs, calls,
    opts: el('fc-login-inst').innerHTML, qrShown: el('fc-login-qr').style.display, go: el('fc-login-go').disabled}));
});
"""


def _run_console(tmp_path, script):
    import json as _json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    h = tmp_path / "harness.js"
    h.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run([node, str(h), str(CONSOLE), _json.dumps(script)], capture_output=True, text=True,
                         timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return _json.loads(out.stdout.strip().splitlines()[-1])


def test_console_flow_runs_to_authorized_in_node(tmp_path):
    res = _run_console(tmp_path, {
        "q1": [{"status": "queued"}, {"status": "done", "result": {"login_id": "L1", "qr_data_url": PNG}}],
        "s*": [{"status": "done", "result": {"status": "authorized", "account_id": "wa-9"}}],
    })
    assert "POST /api/fleet/nodes/n1/login-qr" in res["calls"]
    assert "POST /api/fleet/nodes/n1/login-qr/L1/status" in res["calls"]
    assert res["srcs"] == [PNG]
    assert res["msg"].startswith("登录成功") and "wa-9" in res["msg"]
    assert res["qrShown"] == "none" and res["go"] is False
    assert "player" in res["opts"] and "hub" not in res["opts"]      # health-only instances are not offered


def test_console_never_puts_unsafe_qr_into_img(tmp_path):
    res = _run_console(tmp_path, {
        "q1": [{"status": "done", "result": {"login_id": "L1", "qr_data_url": "javascript:alert(1)"}}],
        "s*": [{"status": "done", "result": {"status": "failed", "reason_code": "x",
                                             "qr_data_url": "data:image/svg+xml;base64,PHN2Zz4="}}],
    })
    assert res["srcs"] == []
    assert res["msg"].startswith("已结束：failed")


def test_console_node_rejection_stops_the_flow(tmp_path):
    res = _run_console(tmp_path, {"q1": [{"status": "rejected", "detail": "bad_platform", "result": {}}], "s*": []})
    assert res["msg"].startswith("发起失败：rejected") and not any("/status" in c for c in res["calls"])
