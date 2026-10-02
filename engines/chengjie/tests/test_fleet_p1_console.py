"""P1-7：控制台改版第一期 + 节点长时间离线告警。"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.fleet import offline_alert as oa
from src.fleet.store import FleetStore, resolve_fleet_cfg, set_store

ENGINE = Path(__file__).resolve().parents[1]
CONSOLE = ENGINE / "domains/fleet_control/web/templates/fleet_console.html"
NOW = 1_800_000_000.0


def _n(nid, state, last_seen, status="active", **kw):
    d = {"node_id": nid, "state": state, "status": status, "last_seen": last_seen, "label": nid.upper(),
         "host_name": "PC-" + nid, "group_name": "机房A"}
    d.update(kw)
    return d


# ── 纯函数 / 告警器 ─────────────────────────────────────────────────────────
def test_resolve_alert_min_defaults_and_disable():
    assert oa.resolve_alert_min(None) == 10 and oa.resolve_alert_min("") == 10
    assert oa.resolve_alert_min("x") == 10 and oa.resolve_alert_min(25) == 25
    assert oa.resolve_alert_min(0) == 0 and oa.resolve_alert_min(-3) == 0
    assert resolve_fleet_cfg({})["offline_alert_min"] is None
    assert resolve_fleet_cfg({"fleet_control": {"offline_alert_min": 5}})["offline_alert_min"] == 5


def test_long_offline_picks_only_offline_past_threshold():
    nodes = [_n("a", "online", NOW - 5), _n("b", "offline", NOW - 9 * 60), _n("c", "offline", NOW - 46 * 60),
             _n("d", "revoked", NOW - 999 * 60, status="revoked"), _n("e", "offline", None)]
    out = oa.long_offline(nodes, now=NOW, after_min=10)
    assert [x["node_id"] for x in out] == ["c"] and out[0]["offline_min"] == 46


def test_alerter_fires_once_per_offline_and_rearms_after_recovery(caplog):
    sent = []
    al = oa.OfflineAlerter(10, notify=lambda text, nid, deb: sent.append((nid, text, deb)))
    caplog.set_level(logging.INFO)
    assert al.check([_n("c", "offline", NOW - 11 * 60)], now=NOW) and len(sent) == 1
    assert sent[0][0] == "c" and "已离线 11 分钟" in sent[0][1] and sent[0][2] == 600
    assert "fleet node_offline_alert node=c" in caplog.text
    assert al.check([_n("c", "offline", NOW - 30 * 60)], now=NOW) == [] and len(sent) == 1   # 不重复刷屏
    al.check([_n("c", "online", NOW)], now=NOW)                                             # 恢复
    assert "fleet node_offline_recovered node=c" in caplog.text
    al.check([_n("c", "offline", NOW - 12 * 60)], now=NOW)
    assert len(sent) == 2                                                                   # 再次离线再告警


def test_alerter_disabled_and_notify_failure_is_logged(caplog):
    assert oa.OfflineAlerter(0, notify=lambda *a: 1 / 0).check([_n("c", "offline", NOW - 99 * 60)], now=NOW) == []

    def boom(*a):
        raise RuntimeError("tg down")
    caplog.set_level(logging.WARNING)
    assert oa.OfflineAlerter(10, notify=boom).check([_n("c", "offline", NOW - 99 * 60)], now=NOW)
    assert "notify failed node=c" in caplog.text


def test_default_notify_uses_existing_ops_alert_channel(monkeypatch):
    calls = []
    import src.ops.ops_alert as ops
    monkeypatch.setattr(ops, "notify", lambda kind, text, **kw: calls.append((kind, text, kw)) or True)
    oa.OfflineAlerter(10).check([_n("c", "offline", NOW - 20 * 60)], now=NOW)
    assert calls and calls[0][0] == "fleet_node_offline" and calls[0][2]["account_id"] == "c"
    assert calls[0][2]["source"] == "fleet-controller"


def test_start_watch_runs_in_background():
    hit = []
    al = oa.OfflineAlerter(10, notify=lambda text, nid, deb: hit.append(nid))
    th = oa.start_watch(lambda: [_n("z", "offline", time.time() - 3600)], al, interval_sec=0.01)
    deadline = time.time() + 5
    while not hit and time.time() < deadline:
        time.sleep(0.02)
    assert th is not None and th.daemon and hit == ["z"]
    assert oa.start_watch(lambda: [], oa.OfflineAlerter(0)) is None


# ── 路由：overview 带 offline_alert；startup 挂了巡检 ────────────────────────
def _client(st, cfg):
    from domains.fleet_control.web.routes import register_routes
    app = FastAPI()
    set_store(st)

    def _auth(request: Request):
        if request.headers.get("authorization") != "Bearer op":
            raise HTTPException(status_code=401)

    ctx = SimpleNamespace(config_manager=SimpleNamespace(config=cfg), api_auth=_auth,
                          api_write_factory=lambda perm: _auth, page_auth=_auth, templates=None)
    register_routes(app, ctx)
    return app, TestClient(app)


def test_overview_reports_long_offline_nodes(tmp_path, monkeypatch):
    monkeypatch.delenv("EVENT_INGEST_KEY", raising=False)
    st = FleetStore(str(tmp_path / "f.db"))
    try:
        from src.fleet.protocol import PROTO_VERSION
        code = st.create_enroll_code(label="B-09", now=time.time() - 3600)["code"]
        res = st.enroll(code=code, machine_id="m-1", host_name="PC-9", proto_version=PROTO_VERSION, now=time.time() - 3600)
        assert res["ok"], res
        app, c = _client(st, {"fleet_control": {"offline_alert_min": 15}})
        assert any(getattr(h, "__name__", "") == "_start_offline_watch" for h in app.router.on_startup)
        d = c.get("/api/fleet/overview", headers={"Authorization": "Bearer op"}).json()
        a = d["offline_alert"]
        assert a["after_min"] == 15 and a["push"] is True and a["channel"] == "log"
        assert len(a["nodes"]) == 1 and a["nodes"][0]["offline_min"] >= 59
        monkeypatch.setenv("EVENT_INGEST_KEY", "x")
        assert c.get("/api/fleet/overview", headers={"Authorization": "Bearer op"}).json()["offline_alert"]["channel"] == "ops_alert"
    finally:
        set_store(None)
        st.close()


# ── 控制台模板 ───────────────────────────────────────────────────────────────
def _js():
    return re.search(r"<script>([\s\S]*?)</script>", CONSOLE.read_text(encoding="utf-8")).group(1)


def test_console_has_no_browser_popups_and_chinese_buttons():
    js = _js()
    for bad in ("prompt(", "alert(", "confirm("):
        assert bad not in js, bad
    kinds = re.search(r"var KINDS=\[([^\]]*)\]", js).group(1)
    labels = re.search(r"var KIND_LABEL=\{([^}]*)\}", js).group(1)
    for k in re.findall(r"'([a-z_]+)'", kinds):
        assert re.search(k + r":'[^a-z']+'", labels), k               # 每个按钮都有中文名
    assert "'+k+'</button>" not in js                                 # 不再直接显示英文 kind
    html = CONSOLE.read_text(encoding="utf-8")
    assert 'id="fc-dlg"' in html and 'id="fc-toast"' in html and 'id="fc-offline"' in html
    assert "product_name|default('智拓群控')" in html


def test_console_stop_account_is_picked_from_lists():
    js = _js()
    stop = js.split("function stopOpen")[1].split("\n  }")[0]
    assert "fc-dlg-acc" in stop and "fc-dlg-phone" in stop and "account_health" in stop
    assert "PHONE_RE.test(p)" in stop


_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {}, calls = [];
function el(id) {
  if (!els[id]) els[id] = {id, textContent: '', innerHTML: '', value: '', className: '', style: {}, attrs: {}, handlers: {},
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; }, addEventListener(ev, fn) { this.handlers[ev] = fn; },
    scrollIntoView() {}, focus() {}, select() {}};
  return els[id];
}
global.document = {getElementById: el};
global.alert = () => { throw new Error('alert used'); }; global.confirm = global.alert; global.prompt = global.alert;
global.setInterval = () => 0; global.clearTimeout = () => {}; global.setTimeout = () => 1;
const now = Date.now() / 1000;
function reply(d) { return Promise.resolve({ok: true, json: () => Promise.resolve(d)}); }
global.apiFetch = (url, opt) => {
  calls.push([(opt && opt.method) || 'GET', url, opt && opt.body ? JSON.parse(opt.body) : null]);
  if (url.startsWith('/api/fleet/overview')) return reply({nodes: {total: 2, online: 1, offline: 1},
    offline_alert: {after_min: 10, channel: 'log', nodes: [{node_id: 'n2', label: 'B-09', offline_min: 46}]}});
  if (url.startsWith('/api/fleet/nodes?')) return reply({nodes: [
    {node_id: 'n1', status: 'active', state: 'online', label: 'A-01', last_seen: now, last_heartbeat: {instances: [{name: 'player', up: true}]}},
    {node_id: 'n2', status: 'active', state: 'offline', label: 'B-09', last_seen: now - 2760}]});
  if (url.startsWith('/api/fleet/tasks?node_id=n1')) return reply({tasks: [
    {kind: 'account_health', status: 'done', result: {accounts: [{account_id: 'wa-1', platform: 'whatsapp'}]}},
    {kind: 'stop_account', status: 'done', target: {phone: '639170000123', account: 'wa-1'}, created_at: now}]});
  if (url.startsWith('/api/fleet/tasks?') || url === '/api/fleet/pending' || url === '/api/fleet/room-keys')
    return reply({tasks: [], pending: [], room_keys: []});
  return reply({ok: true});
};
eval(js);
const step = JSON.parse(process.argv[3]);
const btn = {getAttribute: (k) => ({'data-n': 'n1', 'data-k': step.kind})[k]};
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  for (let i = 0; i < 5; i++) await tick();
  el('fc-list').handlers.click({target: {closest: () => btn}});
  for (let i = 0; i < 5; i++) await tick();
  const dlgBody = el('fc-dlg-body').innerHTML, shown = el('fc-dlg').style.display;
  for (const [k, v] of Object.entries(step.set || {})) el(k).value = v;
  el('fc-dlg-ok').handlers.click();
  const err = el('fc-dlg-err').textContent;
  for (let i = 0; i < 5; i++) await tick();
  console.log(JSON.stringify({dlgBody, shown, err, calls, banner: el('fc-offline').innerHTML,
    list: el('fc-list').innerHTML, toast: el('fc-toast').textContent}));
})();
"""


def _run(tmp_path, step):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    h = tmp_path / "h.js"
    h.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run([node, str(h), str(CONSOLE), json.dumps(step)], capture_output=True, text=True,
                         timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_console_stop_flow_picks_phone_and_account_in_page(tmp_path):
    res = _run(tmp_path, {"kind": "stop_account", "set": {"fc-dlg-phone": "639170000123", "fc-dlg-acc": "wa-1"}})
    assert res["shown"] == "" and "639170000123" in res["dlgBody"] and "wa-1" in res["dlgBody"]
    posts = [c for c in res["calls"] if c[0] == "POST"]
    assert posts == [["POST", "/api/fleet/nodes/n1/tasks",
                      {"kind": "stop_account", "target": {"phone": "639170000123", "account": "wa-1"}}]]
    assert "停止触达" in res["toast"]
    assert "B-09" in res["banner"] and "46 分钟" in res["banner"] and "fc-row-alert" in res["list"]
    assert "已离线" in res["list"] and "停止触达" in res["list"] and ">stop_account<" not in res["list"]


def test_console_stop_flow_rejects_bad_other_number(tmp_path):
    res = _run(tmp_path, {"kind": "stop_account", "set": {"fc-dlg-phone": "__other", "fc-dlg-other": "12ab"}})
    assert "号码格式不对" in res["err"] and not [c for c in res["calls"] if c[0] == "POST"]


def test_console_restart_uses_in_page_dialog_with_instance(tmp_path):
    res = _run(tmp_path, {"kind": "restart_instance", "set": {"fc-dlg-inst": "player"}})
    assert "player" in res["dlgBody"]
    assert ["POST", "/api/fleet/nodes/n1/tasks", {"kind": "restart_instance", "target": {"instance": "player"}}] in res["calls"]


def test_offline_watch_registers_via_on_event():
    # Starlette 1.x (VPS venv) has no add_event_handler; on_event is what admin.py uses too
    src = (ENGINE / "domains/fleet_control/web/routes.py").read_text(encoding="utf-8")
    assert 'app.on_event("startup")(_start_offline_watch)' in src
