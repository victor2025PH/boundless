"""Site todos for the room PC (agent 0.3.22)."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.fleet import agent as agent_mod
from src.fleet import operator_alert as oa
from src.fleet.phones import PROTECTED_SERIALS
from src.fleet.protocol import (
    LEGACY_ALLOWED_KINDS, PROTO_VERSION, REMOTE_PHONE_KINDS, STATUS_DONE, STATUS_QUEUED, STATUS_REJECTED,
    TASK_KINDS, TASK_SITE_TODO,
)
from src.fleet.site_todo import (
    CONSOLE_REINSTALL, build_site_todos, console_site_todos, dispatch_site_todo, sanitize_site_todo_payload,
    should_dispatch, site_skip_reason,
)
from src.fleet.store import FleetStore, set_store

SERIAL = "B03AUTH01"
OTHER = "B03DENY01"
NINE_A = "B09AUTH01"
NINE_B = "B09DENY01"
GONE = "B04GONE01"
PROTECTED = PROTECTED_SERIALS[0]
LEDGER = {SERIAL: "03", OTHER: "03", NINE_A: "09", NINE_B: "09", GONE: "04"}


def _phones():
    return [
        {"serial": SERIAL, "state": "device"},
        {"serial": OTHER, "state": "unauthorized"},
        {"serial": NINE_A, "state": "device"},
        {"serial": NINE_B, "state": "unauthorized"},
        {"serial": PROTECTED, "state": "unauthorized"},
    ]


def test_ledger_conflict_is_its_own_row_without_serials():
    todos = build_site_todos(phones=_phones(), net_rows=[], wallpaper_map=LEDGER)
    pairs = {(item["category"], item["wallpaper_no"]) for item in todos}
    assert ("ledger_conflict", "03") in pairs
    assert ("ledger_conflict", "09") in pairs
    assert ("unauthorized", "03") in pairs
    assert ("unauthorized", "09") in pairs
    assert all(item["category"] != "ledger_conflict" or item["wallpaper_no"] in {"03", "09"} for item in todos)
    blob = json.dumps(todos, ensure_ascii=False)
    for secret in (SERIAL, OTHER, NINE_A, NINE_B, PROTECTED):
        assert secret not in blob
    assert "serial" not in blob
    conflict = next(item for item in todos if item["category"] == "ledger_conflict" and item["wallpaper_no"] == "03")
    assert set(conflict) <= {"category", "wallpaper_no", "unnumbered", "slot"}


def test_categories_and_second_facebook_signal():
    phones = [
        {"serial": "DEV0001", "state": "device"},
        {"serial": "OFF0001", "state": "offline"},
        {"serial": "UNK0001", "state": "unauthorized"},
    ]
    wall = {"OFF0001": "05"}
    net = [
        {"wallpaper_no": "11", "key": "11", "reachable": False, "transport": "none", "sim": "present",
         "signal": "ok", "airplane": False, "fb_screen": "app", "fb_confirm": ""},
        {"wallpaper_no": "12", "key": "12", "reachable": True, "transport": "mobile", "sim": "present",
         "signal": "none", "airplane": False, "fb_screen": "login", "fb_confirm": ""},
        {"wallpaper_no": "13", "key": "13", "reachable": True, "transport": "wifi", "sim": "present",
         "signal": "none", "airplane": False, "fb_screen": "login", "fb_confirm": "login"},
        {"wallpaper_no": "14", "key": "14", "reachable": True, "transport": "mobile", "sim": "present",
         "signal": "none", "airplane": True, "fb_screen": "app", "fb_confirm": ""},
        {"wallpaper_no": "15", "key": "15", "reachable": True, "transport": "mobile", "sim": "present",
         "signal": "ok", "airplane": False, "fb_screen": "login", "fb_confirm": "login"},
    ]
    todos = build_site_todos(phones=phones, net_rows=net, wallpaper_map=wall)
    pairs = {(item["category"], item["wallpaper_no"]) for item in todos}
    assert ("no_network", "11") in pairs
    assert ("no_signal", "12") in pairs
    assert ("fb_logged_out", "12") not in pairs
    assert ("no_signal", "13") not in pairs
    assert ("fb_logged_out", "13") in pairs
    assert ("no_network", "14") in pairs
    assert ("no_signal", "14") not in pairs
    assert ("fb_logged_out", "15") in pairs
    assert ("usb_unplugged", "05") in pairs
    assert ("unauthorized", "") in pairs
    assert ("missing_wallpaper", "") in pairs
    assert any(item["category"] == "missing_wallpaper" and item.get("slot") == 1 for item in todos)
    blob = json.dumps(todos)
    assert "DEV0001" not in blob and "note" not in blob


def test_missing_ledger_phone_is_usb_only_when_inventory_succeeded():
    todos = build_site_todos(
        phones=[{"serial": SERIAL, "state": "device"}], net_rows=[], wallpaper_map=LEDGER, phones_error="")
    assert ("usb_unplugged", "04") in {(item["category"], item["wallpaper_no"]) for item in todos}
    quiet = build_site_todos(
        phones=[], net_rows=[], wallpaper_map={GONE: "04"}, phones_error="adb_server_not_running")
    assert quiet == []


def test_protected_phone_does_not_become_a_conflict():
    todos = build_site_todos(
        phones=[{"serial": PROTECTED, "state": "unauthorized"}, {"serial": SERIAL, "state": "unauthorized"}],
        net_rows=[], wallpaper_map={PROTECTED: "03", SERIAL: "03"})
    assert [item["category"] for item in todos] == ["unauthorized"]
    assert todos[0]["wallpaper_no"] == "03"
    assert PROTECTED not in json.dumps(todos)


def test_sanitize_drops_free_text_and_serials():
    clean = sanitize_site_todo_payload({"todos": [{
        "category": "usb_unplugged", "wallpaper_no": "07", "serial": SERIAL, "note": "plug it back " + SERIAL,
    }, {"category": "made_up", "wallpaper_no": "1"}]})
    assert clean == {"todos": [{"category": "usb_unplugged", "wallpaper_no": "07", "unnumbered": False}]}
    assert SERIAL not in json.dumps(clean)


def test_skip_live_stream_and_173_only():
    assert site_skip_reason({"host_name": "173"}) == "node_173"
    assert site_skip_reason({"label": "seat-173"}) == "node_173"
    assert site_skip_reason({"node_id": "173"}) == "node_173"
    assert site_skip_reason({"node_id": "n_aa173bb0cc1"}) == ""
    assert site_skip_reason({"host_name": "MNLWIN"}) == "live_stream"
    assert site_skip_reason({"label": "AORY"}) == ""
    assert site_skip_reason({"host_name": "AORY-A", "label": "AORY-A", "node_id": "n_aory_a"}) == ""
    assert site_skip_reason({"host_name": "AORY-B", "node_id": "n_aory_b"}) == ""
    assert site_skip_reason({"meta": {"live_stream": True}, "host_name": "PC", "node_id": "n_pc"}) == "live_stream"
    assert site_skip_reason({"host_name": "PC-20240123AORY"}) == ""
    assert site_skip_reason({"host_name": "CHINAMI-B", "label": "机房"}) == ""


def test_should_dispatch_debounces_identical_and_rapid_changes():
    assert should_dispatch(None, "[]", None, 100) is True
    assert should_dispatch("[]", "[]", 100, 200) is False
    assert should_dispatch("a", "b", 100, 110, debounce_sec=25) is False
    assert should_dispatch("a", "b", 100, 130, debounce_sec=25) is True


def _enroll(st, host="PC-1", label="room", machine="m-1", now=None):
    code = st.create_enroll_code(label=label, now=now)["code"]
    res = st.enroll(code=code, machine_id=machine, host_name=host, proto_version=PROTO_VERSION, now=now)
    assert res["ok"], res
    return res


def test_dispatch_queues_one_redacted_task_and_skips_173(tmp_path):
    st = FleetStore(str(tmp_path / "f.db"))
    try:
        room = _enroll(st, host="CHINAMI-B", label="机房B")
        skipped = _enroll(st, host="173", label="seat", machine="m-173")
        st.enqueue(room["node_id"], "push_config", payload={"patch": {"wallpaper_map": LEDGER}})
        st.heartbeat(room["node_id"], {"phones": _phones()})
        st.heartbeat(skipped["node_id"], {"phones": [{"serial": SERIAL, "state": "unauthorized"}]})
        state = {}
        first = dispatch_site_todo(st, room["node_id"], state)
        assert first
        assert dispatch_site_todo(st, skipped["node_id"], state) is None
        queued = st.list_tasks(node_id=room["node_id"], status=STATUS_QUEUED, kind=TASK_SITE_TODO)
        assert len(queued) == 1
        blob = json.dumps(queued[0]["payload"], ensure_ascii=False)
        assert SERIAL not in blob and PROTECTED not in blob
        assert any(item["category"] == "ledger_conflict" for item in queued[0]["payload"]["todos"])
        again = dispatch_site_todo(st, room["node_id"], state)
        assert again is None
        assert len(st.list_tasks(node_id=room["node_id"], status=STATUS_QUEUED, kind=TASK_SITE_TODO)) == 1
    finally:
        st.close()


def test_panel_groups_categories_and_restores_network(tmp_path):
    assert oa.PANEL_SCRIPT.isascii()
    assert "todos" in oa.PANEL_SCRIPT and "cat_" in oa.PANEL_SCRIPT
    assert "title_todo" in oa.PANEL_SCRIPT
    assert oa.STRINGS["zh"]["act_no_network"] == "开流量或连WiFi"
    assert oa.STRINGS["zh"]["act_ledger_conflict"] == "需给其中一部重新编号"
    assert oa.STRINGS["en"]["act_usb_unplugged"]
    calls = []
    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0, present=lambda _s, payload: calls.append(payload))
    cfg = {"operator_alert_enabled": True, "wallpaper_map": {SERIAL: "07"}}
    alert.apply_todos([
        {"category": "no_network", "wallpaper_no": "07", "serial": SERIAL},
        {"category": "ledger_conflict", "wallpaper_no": "03"},
    ], cfg)
    assert calls[-1]["raised"] == ["todos"]
    assert calls[-1]["todos"][0]["category"] == "no_network"
    assert SERIAL not in json.dumps(calls[-1])
    alert.apply_todos(calls[-1]["todos"], cfg)
    assert len(calls) == 1
    snap = {
        "enabled": True, "seq": 4, "updated_at": 1000.0,
        "phones": [{"key": "07", "wallpaper_no": "07", "reason": "offline", "since": 1000.0}],
        "network": [{"key": "07", "wallpaper_no": "07", "status_zh": "网络✓ wifi", "status_en": "net ok wifi",
                     "alert": False, "reachable": True, "transport": "wifi"}],
        "todos": [{"category": "unauthorized", "wallpaper_no": "07", "unnumbered": False}],
        "pc": None,
    }
    (tmp_path / "restored").mkdir()
    (tmp_path / "restored" / oa.SNAP_NAME).write_text(json.dumps(snap), encoding="utf-8")
    restored_calls = []
    restored = oa.OperatorAlert(
        tmp_path / "restored", clock=lambda: 5000.0,
        present=lambda _s, payload: restored_calls.append(payload))
    assert restored._network[0]["status_zh"] == "网络✓ wifi"
    assert restored._todos[0]["category"] == "unauthorized"
    restored.observe([{"serial": SERIAL, "state": "offline"}], "", cfg)
    assert restored_calls == []
    written = json.loads((tmp_path / "restored" / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert written["network"][0]["status_zh"] == "网络✓ wifi"
    assert written["todos"][0]["category"] == "unauthorized"
    assert SERIAL not in json.dumps(written)


def test_agent_site_todo_rejects_live_stream_and_diag_reports_panel(tmp_path, monkeypatch):
    assert TASK_SITE_TODO in TASK_KINDS
    assert TASK_SITE_TODO not in REMOTE_PHONE_KINDS
    assert TASK_SITE_TODO not in LEGACY_ALLOWED_KINDS
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.save()
    agent = agent_mod.NodeAgent(cfg, http=lambda *_a, **_k: (200, {}), app_version="t")
    agent.cfg.data["operator_alert_enabled"] = True
    status, result, detail = agent.execute({
        "kind": TASK_SITE_TODO,
        "payload": {"todos": [{"category": "unauthorized", "wallpaper_no": "09", "serial": SERIAL, "note": SERIAL}]},
    })
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result == {"ok": True, "count": 1}
    snap = json.loads((tmp_path / "fleet" / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert snap["todos"][0]["category"] == "unauthorized"
    assert "strings" in snap and snap["strings"]["zh"]["cat_unauthorized"] == "未授权"
    assert SERIAL not in json.dumps(snap)
    status, diag, detail = agent.execute({"kind": "operator_alert_diag", "payload": {}})
    assert (status, detail) == (STATUS_DONE, "ok")
    assert diag["panel_running"] is False
    assert diag["last_present"]["mode"] == "headless"
    assert diag["last_present"]["shown"] is False
    assert isinstance(diag["session_id"], int)
    assert diag["todos"][0]["wallpaper_no"] == "09"
    assert SERIAL not in json.dumps(diag)

    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: True)
    live_cfg = agent_mod.AgentConfig(tmp_path / "live")
    live_cfg.save()
    live = agent_mod.NodeAgent(live_cfg, http=lambda *_a, **_k: (200, {}), app_version="t")
    status, result, detail = live.execute({
        "kind": TASK_SITE_TODO,
        "payload": {"todos": [{"category": "no_network", "wallpaper_no": "1"}]},
    })
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert result == {}
    assert not (tmp_path / "live" / oa.SNAP_NAME).exists()


def test_ack_strips_serials_and_net_health_enqueues(tmp_path):
    st = FleetStore(str(tmp_path / "ack.db"))
    try:
        room = _enroll(st, host="CHINAMI-B", label="机房B", machine="m-ack")
        st.enqueue(room["node_id"], "push_config", payload={"patch": {"wallpaper_map": {SERIAL: "07"}}})
        st.heartbeat(room["node_id"], {"phones": [{"serial": SERIAL, "state": "device"}]})
        state = {}
        assert dispatch_site_todo(st, room["node_id"], state) is None
        c = _client(st)
        nk = {"Authorization": f"Bearer {room['node_key']}"}
        op = {"Authorization": "Bearer op"}
        net = c.post(f"/api/fleet/nodes/{room['node_id']}/tasks", json={"kind": "net_health"}, headers=op).json()["task"]
        assert c.get("/api/fleet/tasks/pull", headers=nk).json()["tasks"]
        poisoned = {
            "phones": [{
                "serial": SERIAL, "wallpaper_no": "07", "key": "07", "reachable": False, "transport": "none",
                "sim": "present", "signal": "none", "airplane": False, "fb_screen": "login", "fb_confirm": "login",
                "status_zh": "网络✗ 无流量",
            }],
        }
        ack = c.post("/api/fleet/tasks/ack", json={
            "task_id": net["task_id"], "status": "done", "result": poisoned,
            "detail": "device '" + SERIAL + "' not found",
        }, headers=nk).json()
        assert ack["status"] == STATUS_DONE
        stored = st.get_task(net["task_id"])["result"]
        assert SERIAL not in json.dumps(stored)
        queued = st.list_tasks(node_id=room["node_id"], status=STATUS_QUEUED, kind=TASK_SITE_TODO)
        assert len(queued) == 1
        cats = {item["category"] for item in queued[0]["payload"]["todos"]}
        assert "no_network" in cats and "fb_logged_out" in cats
        assert SERIAL not in json.dumps(queued[0]["payload"])
        pulled = c.get("/api/fleet/tasks/pull", headers=nk).json()["tasks"]
        assert pulled and pulled[0]["kind"] == TASK_SITE_TODO
        done = c.post("/api/fleet/tasks/ack", json={
            "task_id": pulled[0]["task_id"], "status": "done",
            "result": {"ok": True, "count": 2, "serial": SERIAL, "note": SERIAL},
            "detail": "device '" + SERIAL + "' offline",
        }, headers=nk).json()
        assert done["status"] == STATUS_DONE
        saved = st.get_task(pulled[0]["task_id"])
        assert saved["result"] == {"ok": True, "count": 2}
        assert SERIAL not in json.dumps(saved["result"])
        assert SERIAL not in saved["detail"]
        assert "[redacted]" in saved["detail"]
    finally:
        set_store(None)
        st.close()


def _client(st):
    from domains.fleet_control.web.routes import register_routes

    app = FastAPI()
    set_store(st)

    def _auth(request: Request):
        if request.headers.get("authorization") != "Bearer op":
            raise HTTPException(status_code=401)

    ctx = SimpleNamespace(
        config_manager=SimpleNamespace(config={"fleet_control": {}}),
        api_auth=_auth, api_write_factory=lambda _perm: _auth, page_auth=_auth, templates=None)
    register_routes(app, ctx)
    return TestClient(app)


def test_routes_dispatch_on_heartbeat_and_console_for_offline(tmp_path, monkeypatch):
    clock = {"t": time.time()}
    monkeypatch.setattr("src.fleet.site_todo.time.time", lambda: clock["t"])
    st = FleetStore(str(tmp_path / "f.db"))
    try:
        room = _enroll(st, host="CHINAMI-B", label="机房B", machine="m-room")
        live = _enroll(st, host="MNLWIN", label="MNLWIN", machine="m-live")
        old = _enroll(st, host="PC-OLD", label="old", machine="m-old", now=clock["t"] - 1000)
        c = _client(st)
        op = {"Authorization": "Bearer op"}
        nk = {"Authorization": f"Bearer {room['node_key']}"}
        st.enqueue(room["node_id"], "push_config", payload={"patch": {"wallpaper_map": {SERIAL: "03", OTHER: "03"}}})
        body = {"phones": [
            {"serial": SERIAL, "state": "device"},
            {"serial": OTHER, "state": "unauthorized"},
            {"serial": PROTECTED, "state": "unauthorized"},
        ]}
        assert c.post("/api/fleet/heartbeat", json=body, headers=nk).status_code == 200
        queued = st.list_tasks(node_id=room["node_id"], status=STATUS_QUEUED, kind=TASK_SITE_TODO)
        assert len(queued) == 1
        blob = json.dumps(queued[0]["payload"])
        assert PROTECTED not in blob and SERIAL not in blob
        assert {item["category"] for item in queued[0]["payload"]["todos"]} >= {"ledger_conflict", "unauthorized"}
        clock["t"] += 5
        body["phones"][1]["state"] = "offline"
        assert c.post("/api/fleet/heartbeat", json=body, headers=nk).status_code == 200
        still = st.list_tasks(node_id=room["node_id"], status=STATUS_QUEUED, kind=TASK_SITE_TODO)
        assert len(still) == 1
        assert any(item["category"] == "unauthorized" for item in still[0]["payload"]["todos"])
        clock["t"] += 30
        body["phones"][1]["state"] = "device"
        assert c.post("/api/fleet/heartbeat", json=body, headers=nk).status_code == 200
        latest = st.list_tasks(node_id=room["node_id"], status=STATUS_QUEUED, kind=TASK_SITE_TODO)
        assert len(latest) == 1
        latest_cats = [item["category"] for item in latest[0]["payload"]["todos"]]
        assert "unauthorized" not in latest_cats
        assert "ledger_conflict" in latest_cats

        live_nk = {"Authorization": f"Bearer {live['node_key']}"}
        assert c.post("/api/fleet/heartbeat", json={"phones": [{"serial": SERIAL, "state": "offline"}]},
                      headers=live_nk).status_code == 200
        assert st.list_tasks(node_id=live["node_id"], kind=TASK_SITE_TODO) == []
        refused = c.post(f"/api/fleet/nodes/{live['node_id']}/tasks",
                         json={"kind": "site_todo", "payload": {"todos": [{"category": "no_network", "wallpaper_no": "1",
                                                                            "serial": SERIAL}]}},
                         headers=op)
        assert refused.status_code == 409

        rows = c.get("/api/fleet/site-todos", headers=op).json()["nodes"]
        by_host = {row["host_name"]: row for row in rows}
        assert by_host["MNLWIN"]["skipped"] == "live_stream"
        assert by_host["MNLWIN"]["todos"] == []
        assert by_host["PC-OLD"]["console"] == CONSOLE_REINSTALL
        assert by_host["CHINAMI-B"]["console"] == ""
        assert SERIAL not in json.dumps(rows)
        viewed = console_site_todos(st)
        assert any(row["console"] == CONSOLE_REINSTALL for row in viewed)
        assert old["node_id"]
    finally:
        set_store(None)
        st.close()
