"""养号 / 私信 / 看短视频。mock adb，不碰真机，也不碰受保护的直播机。"""
from __future__ import annotations

import json

import pytest

from src.fleet.phone_flow_robust import MAX_DWELL_SEC, dwell_seconds
from src.fleet.phone_flow_rules import kind_for_flow, validate_flow_payload
from src.fleet.phone_flows import PhoneFlows, bundled_ui_map, compile_flow, compile_flow_checked
from src.fleet.phone_ops import MIN_INTERVAL_SEC
from src.fleet.phone_rules import PhoneOpError, sanitize_phone_result
from src.fleet.protocol import (
    CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2, CAP_PHONE_OPS_V1, FLOW_NAMES, PHONE_FLOW_KINDS, PHONE_FLOW_TTL_SEC,
    PHONE_SESSION_KINDS, SESSION_FLOW_NAMES, SOCIAL_APPS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_KINDS, TASK_PHONE_DM, TASK_PHONE_KEY, TASK_PHONE_SWIPE, TASK_PHONE_TAP, TASK_PHONE_TEXT, TASK_PHONE_WARMUP,
    TASK_PHONE_WATCH, TASK_PRIORITY, missing_cap,
)
from tests.test_fleet_control import OP, _client
from tests.test_fleet_phone_flow_robust import IndexAdb, _paint, _px
from tests.test_fleet_phone_flows import (
    W, H, _cnode, _console, _flow_node, _frame, _ops, st,  # noqa: F401
)

APPS = SOCIAL_APPS
_LOGGED = (24, 119, 242)
_THREAD = (20, 180, 80)


def _anchor(app, name, w=W, h=H):
    ax, ay = bundled_ui_map()["apps"][app]["anchors"][name]
    return (ax * w // 1000, ay * h // 1000)


def _rng_zero():
    return 0.0


def _body(app, flow, **kw):
    p = {"app": app}
    if flow == "warmup":
        p["scrolls"] = kw.pop("scrolls", 4)
        p["likes"] = kw.pop("likes", 0)
    elif flow == "watch":
        p["watches"] = kw.pop("watches", 3)
        p["likes"] = kw.pop("likes", 0)
    elif flow == "dm":
        p["handle"] = kw.pop("handle", "some.user")
        p["text"] = kw.pop("text", "hello fleet")
    p.update(kw)
    return p


def _texts(actions):
    return [a for a in actions if len(a) >= 5 and a[4] == "text"]


def _taps_at(compiled, xy):
    return [p for k, p in compiled if k == TASK_PHONE_TAP and (p["x"], p["y"]) == xy]


# ── 协议 / 参数 ───────────────────────────────────────────────────────────────
def test_session_kinds_need_v2_and_leave_v1_kinds_alone():
    assert list(PHONE_FLOW_KINDS) == ["phone_post", "phone_like", "phone_comment", "phone_follow"]
    assert FLOW_NAMES == ("post", "like", "comment", "follow")
    assert PHONE_SESSION_KINDS == (TASK_PHONE_WARMUP, TASK_PHONE_DM, TASK_PHONE_WATCH)
    assert SESSION_FLOW_NAMES == ("warmup", "dm", "watch")
    for k in PHONE_SESSION_KINDS:
        assert k in TASK_KINDS and TASK_PRIORITY[k] == 6
        assert missing_cap(k, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1]) == CAP_PHONE_FLOWS_V2
        assert missing_cap(k, [CAP_PHONE_FLOWS_V2]) == ""
    assert kind_for_flow("Warmup") == TASK_PHONE_WARMUP
    assert kind_for_flow("DM") == TASK_PHONE_DM
    assert kind_for_flow("Watch") == TASK_PHONE_WATCH
    assert kind_for_flow("nope") == ""


def test_session_payload_defaults_and_rejects():
    assert validate_flow_payload(TASK_PHONE_WARMUP, {"app": "TikTok", "note": "x"}) == {
        "app": "tiktok", "scrolls": 4, "likes": 0,
    }
    assert validate_flow_payload(TASK_PHONE_WATCH, {"app": "facebook"}) == {
        "app": "facebook", "watches": 3, "likes": 0,
    }
    dm = validate_flow_payload(TASK_PHONE_DM, {"app": "instagram", "handle": "some.user", "text": "hello fleet", "evil": "1"})
    assert dm == {"app": "instagram", "handle": "some.user", "text": "hello fleet"}
    cases = [
        (TASK_PHONE_WARMUP, {"app": "facebook", "scrolls": 0}, "bad_scrolls"),
        (TASK_PHONE_WARMUP, {"app": "facebook", "scrolls": 9}, "bad_scrolls"),
        (TASK_PHONE_WARMUP, {"app": "facebook", "scrolls": 2, "likes": 3}, "bad_likes"),
        (TASK_PHONE_WATCH, {"app": "facebook", "watches": 0}, "bad_watches"),
        (TASK_PHONE_WATCH, {"app": "facebook", "watches": 3, "likes": 4}, "bad_likes"),
        (TASK_PHONE_DM, {"app": "facebook", "handle": "bad name", "text": "hi"}, "bad_handle"),
        (TASK_PHONE_DM, {"app": "facebook", "handle": "ada", "text": "你好"}, "text_non_ascii_unsupported"),
        (TASK_PHONE_DM, {"app": "facebook", "handle": "ada"}, "bad_text"),
        (TASK_PHONE_WARMUP, {"app": "myspace"}, "bad_app"),
    ]
    for kind, payload, code in cases:
        with pytest.raises(PhoneOpError) as ei:
            validate_flow_payload(kind, payload)
        assert ei.value.code == code


def test_dwell_seconds_skips_rng_when_fixed_and_caps():
    def boom():
        raise AssertionError("rng called")

    assert dwell_seconds((800, 800), boom) == 0.8
    assert dwell_seconds((0, 0), boom) == 0.0
    assert dwell_seconds((100, 500), _rng_zero) == 0.1
    assert dwell_seconds((0, 999999), lambda: 1) == MAX_DWELL_SEC


# ── 坐标：三个应用各自一份 ────────────────────────────────────────────────────
def test_map_sessions_differ_per_app_and_do_not_post():
    ui = bundled_ui_map()
    fb, ig, tt = (ui["apps"][a] for a in APPS)
    assert fb["anchors"]["reels_tab"] == (500, 920)
    assert ig["anchors"]["reels_tab"] == (700, 940)
    assert "reels_tab" not in tt["anchors"]
    assert fb["anchors"]["dm_open"] != ig["anchors"]["dm_open"] != tt["anchors"]["dm_open"]
    assert fb["preflight"]["thread"]["after_anchor"] == "dm_open"
    assert fb["preflight"]["thread"]["require"] == ["thread_open"]
    for app in APPS:
        flows = ui["apps"][app]["flows"]
        for name in SESSION_FLOW_NAMES:
            steps = flows[name]
            assert steps[0] == {"op": "key", "key": "home"}
            assert steps[1]["op"] == "tap" and steps[1]["anchor"] == "app_icon"
        blob = json.dumps(flows["warmup"])
        assert "composer" not in blob and "post_submit" not in blob and '"text"' not in blob
    assert ui["apps"]["tiktok"]["flows"]["watch"][2]["anchor"] == "home_tab"
    assert ui["apps"]["facebook"]["flows"]["watch"][2]["anchor"] == "reels_tab"
    assert ui["apps"]["instagram"]["flows"]["watch"][2]["anchor"] == "reels_tab"
    tt_dwell = ui["apps"]["tiktok"]["flows"]["watch"][3]["steps"][0]
    fb_dwell = ui["apps"]["facebook"]["flows"]["watch"][3]["steps"][0]
    assert tt_dwell["lo_ms"] == 2800 and tt_dwell["hi_ms"] == 8900
    assert fb_dwell["lo_ms"] == 2500 and fb_dwell["hi_ms"] == 7000


def test_warmup_likes_are_spread_and_long_dwells_are_occasional():
    ui = bundled_ui_map()
    steps, checks = compile_flow_checked("facebook", "warmup", {"scrolls": 6, "likes": 2}, W, H, ui)
    like_xy = _anchor("facebook", "like_button")
    assert len(_taps_at(steps, like_xy)) == 2
    assert sum(1 for k, _ in steps if k == "dwell") == 8
    assert sum(1 for k, _ in steps if k == TASK_PHONE_SWIPE) == 6
    assert not any(k == TASK_PHONE_TEXT for k, _ in steps)
    none, _ = compile_flow_checked("instagram", "warmup", {"scrolls": 4, "likes": 0}, W, H, ui)
    assert _taps_at(none, _anchor("instagram", "like_button")) == []
    assert sum(1 for k, _ in none if k == "dwell") == 5  # 4 short + 1 long (the 3rd)
    watched, wchecks = compile_flow_checked("tiktok", "watch", {"watches": 3, "likes": 1}, W, H, ui)
    assert len(_taps_at(watched, _anchor("tiktok", "like_button"))) == 1
    assert sum(1 for k, _ in watched if k == "dwell") == 3
    assert watched[2] == (TASK_PHONE_TAP, {"x": _anchor("tiktok", "home_tab")[0], "y": _anchor("tiktok", "home_tab")[1]})
    assert any(c and c.get("preflight") for c in checks)
    dm_steps, dm_checks = compile_flow_checked(
        "facebook", "dm", {"handle": "some.user", "text": "hello fleet"}, W, H, ui)
    thread_checks = [c for c in dm_checks if c and c.get("thread")]
    assert len(thread_checks) == 1
    assert thread_checks[0]["thread_require"] == ("thread_open",)
    assert thread_checks[0]["change"] is True
    assert [p["text"] for k, p in dm_steps if k == TASK_PHONE_TEXT] == ["some.user", "hello fleet"]
    assert wchecks[1]["preflight"] is True


# ── 执行 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("flow", SESSION_FLOW_NAMES)
def test_execute_each_session_flow(app, flow):
    payload = validate_flow_payload(
        {"warmup": TASK_PHONE_WARMUP, "dm": TASK_PHONE_DM, "watch": TASK_PHONE_WATCH}[flow],
        _body(app, flow, scrolls=2, watches=2, likes=0),
    )
    compiled = compile_flow(app, flow, payload, W, H, bundled_ui_map())
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True, rng=_rng_zero)
    status, result, detail = flows.execute(
        {"warmup": TASK_PHONE_WARMUP, "dm": TASK_PHONE_DM, "watch": TASK_PHONE_WATCH}[flow],
        payload, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    actions = fake.actions()
    assert actions[0][2:] == ("exec-out", "screencap")
    adb_steps = [(k, p) for k, p in compiled if k != "dwell"]
    assert len(actions) == 1 + len(adb_steps)
    if flow == "warmup":
        assert result["scrolls"] == 2 and result["likes"] == 0 and result["dwells"] >= 2
        assert "watches" not in result
        assert not any(k == TASK_PHONE_TEXT for k, _ in compiled)
    elif flow == "watch":
        assert result["watches"] == 2 and result["likes"] == 0 and result["dwells"] == 2
    else:
        assert result["chars"] == len(payload["handle"]) + len(payload["text"])
        blob = json.dumps(result)
        assert payload["text"] not in blob and payload["handle"] not in blob
        assert "text" not in result and "handle" not in result
    assert all(s >= MIN_INTERVAL_SEC or s > 0 for s in slept)
    assert fake.calls[0] == ("version",)


def test_warmup_likes_and_dwells_do_not_call_adb_for_the_pause():
    payload = {"app": "facebook", "scrolls": 1, "likes": 0}
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True, rng=_rng_zero)
    status, result, detail = flows.execute(TASK_PHONE_WARMUP, payload, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["dwells"] == 1 and result["likes"] == 0 and result["scrolls"] == 1
    assert not any(len(a) >= 5 and a[4] == "text" for a in fake.actions())
    assert 0.6 in slept
    assert slept.count(MIN_INTERVAL_SEC) == 4


def test_disabled_and_protected_never_touch_adb():
    ops, fake, _ = _ops()
    off = PhoneFlows(enabled=False, rng=_rng_zero)
    status, _res, detail = off.execute(TASK_PHONE_WARMUP, {"app": "tiktok"}, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "phone_flows_disabled")
    on = PhoneFlows(enabled=True, rng=_rng_zero)
    for serial in ("3B1F4KE5MS140P4X", "192.168.0.148:5555"):
        status, _res, detail = on.execute(TASK_PHONE_DM, _body("instagram", "dm"), {"serial": serial}, ops=ops)
        assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    assert fake.calls == []


def test_verify_login_stops_before_any_text_and_thread_stops_before_the_message():
    def logged(n):
        return _paint(n, [(*_px("feed_tab"), _LOGGED)])

    def both(n):
        return _paint(n, [(*_px("feed_tab"), _LOGGED), (*_px("thread_mark"), _THREAD)])

    ops, fake, _ = _ops(adb=IndexAdb(lambda n: _paint(n, [])))
    flows = PhoneFlows(enabled=True, verify=True, rng=_rng_zero)
    status, result, detail = flows.execute(TASK_PHONE_DM, _body("facebook", "dm"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "app_not_ready")
    assert _texts(fake.actions()) == []
    assert "hello fleet" not in json.dumps(result)

    ops2, fake2, _ = _ops(adb=IndexAdb(logged))
    flows2 = PhoneFlows(enabled=True, verify=True, rng=_rng_zero)
    status, result, detail = flows2.execute(TASK_PHONE_DM, _body("facebook", "dm"), {"serial": "S1"}, ops=ops2)
    assert (status, detail) == (STATUS_FAILED, "thread_not_open")
    joined = " ".join(" ".join(a) for a in fake2.actions())
    assert joined.count("text") == 1
    assert "hello" not in joined and "some.user" in joined

    ops3, fake3, _ = _ops(adb=IndexAdb(both))
    flows3 = PhoneFlows(enabled=True, verify=True, rng=_rng_zero)
    status, result, detail = flows3.execute(
        TASK_PHONE_DM, _body("facebook", "dm", text="hello fleet"), {"serial": "S1"}, ops=ops3)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["chars"] == len("some.user") + len("hello fleet")
    assert "hello fleet" not in json.dumps(result) and "some.user" not in json.dumps(result)


def test_ack_keeps_counts_and_drops_message_and_handle(st):
    from tests.test_fleet_control import _enroll

    res = _enroll(st, mid="m-ack-session")
    nid, key = res["node_id"], res["node_key"]
    st.heartbeat(nid, {"caps": [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2]}, now=__import__("time").time())
    st.set_remote_ops(nid, True)
    rec = st.enqueue(nid, TASK_PHONE_DM, payload={"app": "instagram", "handle": "some.user", "text": "hello fleet"},
                     target={"serial": "S1"})
    c = _client(st)
    r = c.post("/api/fleet/tasks/ack", headers={"Authorization": f"Bearer {key}"}, json={
        "task_id": rec["task_id"], "status": "done",
        "result": {"serial": "S1", "app": "instagram", "flow": "dm", "steps": 11, "chars": 20,
                   "text": "hello fleet", "handle": "some.user", "scrolls": 0, "likes": 1,
                   "watches": 0, "dwells": 0, "elapsed_ms": 12},
    })
    assert r.status_code == 200
    stored = st.get_task(rec["task_id"])["result"]
    assert stored["flow"] == "dm" and stored["chars"] == 20 and stored["dwells"] == 0 and stored["likes"] == 1
    blob = json.dumps(stored)
    assert "hello fleet" not in blob and "some.user" not in blob
    assert "text" not in stored and "handle" not in stored
    clean = sanitize_phone_result(TASK_PHONE_WARMUP, {"app": "tiktok", "flow": "warmup", "scrolls": 4, "likes": 1,
                                                      "dwells": 5, "text": "nope", "handle": "ada"})
    assert clean == {"app": "tiktok", "flow": "warmup", "scrolls": 4, "likes": 1, "dwells": 5}


def test_social_endpoint_queues_sessions_and_rejects_the_wrong_route(st):
    c = _client(st)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2], mid="m-session")
    r = c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/warmup", headers=OP,
               json={"app": "TikTok", "scrolls": 4, "likes": 1, "evil": "rm"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["kind"] == TASK_PHONE_WARMUP and task["ttl_sec"] == PHONE_FLOW_TTL_SEC
    assert task["payload"] == {"app": "tiktok", "scrolls": 4, "likes": 1}
    assert "evil" not in json.dumps(task["payload"])
    watch = c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/watch", headers=OP, json={"app": "facebook", "watches": 3})
    assert watch.status_code == 200
    assert watch.json()["task"]["payload"] == {"app": "facebook", "watches": 3, "likes": 0}
    dm = c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/dm", headers=OP,
                json={"app": "instagram", "handle": "some.user", "text": "hello fleet"})
    assert dm.status_code == 200 and dm.json()["task"]["kind"] == TASK_PHONE_DM
    assert c.post(f"/api/fleet/nodes/{nid}/phones/3B1F4KE5MS140P4X/social/warmup", headers=OP,
                  json={"app": "facebook"}).status_code == 403
    assert c.post(f"/api/fleet/nodes/{nid}/phones/192.168.0.148:5555/social/dm", headers=OP,
                  json={"app": "facebook", "handle": "a", "text": "hi"}).json()["detail"] == "protected_phone"
    assert c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/warmup", headers=OP,
                  json={"app": "facebook", "scrolls": 2, "likes": 9}).json()["detail"] == "bad_likes"
    r = c.post(f"/api/fleet/nodes/{nid}/tasks", headers=OP, json={"kind": "phone_warmup", "payload": {"app": "tiktok"}})
    assert (r.status_code, r.json()["detail"]) == (400, "phone_flows_use_social_endpoint")
    only = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], mid="m-v1-only")
    r = c.post(f"/api/fleet/nodes/{only}/phones/S1/social/watch", headers=OP, json={"app": "tiktok"})
    assert (r.status_code, r.json()["detail"]) == (409, "node_lacks_cap:phone_flows_v2")
    off = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V2], remote=False, mid="m-session-off")
    r = c.post(f"/api/fleet/nodes/{off}/phones/S1/social/warmup", headers=OP, json={"app": "facebook"})
    assert (r.status_code, r.json()["detail"]) == (409, "remote_ops_disabled")


def test_agent_advertises_v2_and_runs_warmup(st, tmp_path, monkeypatch):
    from src.fleet.agent import AGENT_VERSION, AgentConfig, NodeAgent
    from src.fleet.phone_ops import PhoneOps
    from tests.test_fleet_control import _FakeNet
    from tests.test_fleet_phone_flows import FakeAdb

    assert AGENT_VERSION == "0.3.14"
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    client = _client(st)
    cfg = AgentConfig(tmp_path / "state")
    cfg.data.update({"controller_url": "http://127.0.0.1:1", "instances": [], "phone_flows_enabled": True})
    agent = NodeAgent(cfg, http=_FakeNet(client), app_version="t")
    fake = FakeAdb(frame=_frame())
    agent.phone_ops = PhoneOps(run=fake, locate=lambda _p: "adb", server_version=lambda _port: 41)
    agent.phones.collect = lambda: ([{"serial": "S1", "state": "device", "model": "Pixel", "transport": "usb"}], "")
    code = client.post("/api/fleet/enroll-codes", json={}, headers=OP).json()["code"]
    agent.enroll(code, controller_url="https://ctl.test/fleet")
    hb = agent.build_heartbeat()
    assert hb["caps"] == [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2]
    agent.heartbeat()
    nid = cfg.node_id
    st.set_remote_ops(nid, True)
    task = client.post(f"/api/fleet/nodes/{nid}/phones/S1/social/warmup", headers=OP,
                       json={"app": "facebook", "scrolls": 1, "likes": 0}).json()["task"]
    out = agent.run_once(wait=0)
    assert out["tasks"][0]["status"] == STATUS_DONE
    rec = st.get_task(task["task_id"])
    assert rec["status"] == STATUS_DONE
    assert rec["result"]["flow"] == "warmup" and rec["result"]["scrolls"] == 1 and rec["result"]["likes"] == 0
    assert not any(len(a) >= 5 and a[4] == "text" for a in fake.actions())


def test_console_offers_sessions_only_with_v2(tmp_path):
    nodes = [
        _cnode("n1", caps=["phone_ops_v1", "phone_flows_v1", "phone_flows_v2"], remote_ops_enabled=True),
        _cnode("n2", caps=["phone_ops_v1", "phone_flows_v1"], remote_ops_enabled=True),
    ]
    tasks = [
        {"task_id": "t1", "node_id": "n1", "kind": "phone_warmup", "status": "rejected",
         "detail": "node_lacks_cap:phone_flows_v2", "created_at": 1.9e9, "result": {}},
        {"task_id": "t2", "node_id": "n1", "kind": "phone_post", "status": "rejected",
         "detail": "node_lacks_cap:phone_flows_v1", "created_at": 1.9e9, "result": {}},
    ]
    res = _console(tmp_path, {"nodes": nodes, "tasks": tasks, "social": {
        "node": "n1",
        "fields": {"fc-soc-serial": "S1", "fc-soc-app": "tiktok", "fc-soc-flow": "warmup",
                   "fc-soc-warm": "4", "fc-soc-likes": "1"},
    }})
    assert res["list"].count("社交动作") == 2
    assert 'value="warmup"' in res["dlg"] and "看视频" in res["dlg"] and "私信文字" in res["dlg"]
    assert "养号 / 私信 / 看视频" in res["tasks"]
    assert "0.3.8" in res["tasks"]
    posts = [c for c in res["calls"] if c[0] == "POST"]
    assert posts == [["POST", "/api/fleet/nodes/n1/phones/S1/social/warmup",
                      {"app": "tiktok", "scrolls": 4, "likes": 1}]]
    plain = _console(tmp_path, {"nodes": nodes, "tasks": [], "social": {"node": "n2", "fields": {
        "fc-soc-serial": "S1", "fc-soc-app": "tiktok", "fc-soc-flow": "post", "fc-soc-text": "hello fleet",
    }}})
    assert 'value="warmup"' not in plain["dlg"]
    posts = [c for c in plain["calls"] if c[0] == "POST"]
    assert posts == [["POST", "/api/fleet/nodes/n2/phones/S1/social/post",
                      {"app": "tiktok", "text": "hello fleet", "media": 0}]]


def test_add_dwell_caps_and_keeps_the_pacing_floor():
    ops, _fake, slept = _ops()
    ops.add_dwell("S1", 100)
    assert slept == [MAX_DWELL_SEC]
    ops.add_dwell("S1", 0)
    ops.add_dwell("S1", -1)
    assert slept == [MAX_DWELL_SEC]
