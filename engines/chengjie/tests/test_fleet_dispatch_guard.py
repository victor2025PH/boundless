"""0.3.25: one fail-closed gate for live machines, seat 173, and protected phones."""

from __future__ import annotations

import json

from src.fleet.dispatch_guard import auto_dispatch_block
from src.fleet.protocol import CAP_PHONE_FLOWS_V1, CAP_PHONE_OPS_V1, TASK_PHONE_APP_RESTART, TASK_PHONE_LIKE, TASK_PING
from src.fleet.store import FleetStore


def _node(**kw):
    base = {"node_id": "n_room", "host_name": "CHINAMI", "label": "机房", "group_name": "room"}
    base.update(kw)
    return base


def test_live_173_and_protected_combinations_are_excluded():
    assert auto_dispatch_block(_node(label="直播机-01")) == "live_stream"
    assert auto_dispatch_block(_node(host_name="GANZHI-176", label="机房")) == "live_stream"
    assert auto_dispatch_block(_node(group_name="live")) == "live_stream"
    assert auto_dispatch_block(_node(meta={"tags": ["Live"]})) == "live_stream"
    assert auto_dispatch_block(_node(
        host_name="GANZHI-176", label="直播机-01", group_name="live",
    )) == "live_stream"
    assert auto_dispatch_block(_node(host_name="YUYAN-173")) == "node_173"
    assert auto_dispatch_block(_node(host_name="173", label="seat")) == "node_173"
    assert auto_dispatch_block(_node(node_id="seat_173")) == "node_173"
    assert auto_dispatch_block(_node(node_id="n_aa173bb0cc1")) == ""
    assert auto_dispatch_block(_node(), serial="3B1FABCDEF") == "protected_phone"
    assert auto_dispatch_block(_node(), serial="3B1F4KE5MS140P4X") == "protected_phone"
    assert auto_dispatch_block(_node(), serial="S1") == ""


def test_room_nodes_and_explicit_list():
    for host in ("CHINAMI", "CHINAMI-B", "AORY-A", "AORY-B"):
        assert auto_dispatch_block(_node(host_name=host, label=host, node_id="n_" + host)) == ""
    assert auto_dispatch_block(_node(), exclude=["CHINAMI"]) == "excluded"
    assert auto_dispatch_block(_node(host_name="CHINAMI-B", node_id="n_b"), exclude=["CHINAMI"]) == ""
    assert auto_dispatch_block(_node(node_id="n_keep"), exclude=["n_keep"]) == "excluded"
    assert auto_dispatch_block(_node(), exclude={"bad": True}) == "uncertain"
    assert auto_dispatch_block("not-a-node") == "uncertain"
    assert auto_dispatch_block({}) == "uncertain"


def test_guard_fail_closed_when_the_check_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.fleet.dispatch_guard.is_seat_173", boom)
    assert auto_dispatch_block(_node()) == "uncertain"


def _enroll(st, host, label, group, mid):
    code = st.create_enroll_code(label=label, group_name=group)["code"]
    res = st.enroll(code=code, machine_id=mid, host_name=host, proto_version=1)
    assert res["ok"], res
    nid = res["node_id"]
    st.heartbeat(nid, {
        "caps": [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1],
        "phones": [{"serial": "S1", "state": "device"}],
    })
    st.set_remote_ops(nid, True)
    return nid


def test_enqueue_skips_live_and_protected_and_still_queues_rooms(tmp_path):
    st = FleetStore(tmp_path / "fleet.db")
    try:
        live = _enroll(st, "GANZHI-176", "直播机-01", "live", "m-live")
        assert st.enqueue(live, "site_todo", payload={"todos": []}) is None
        assert st.take_dispatch_block() == "live_stream"
        assert st.enqueue(live, TASK_PHONE_LIKE, payload={"app": "facebook", "like_probe": True},
                          target={"serial": "S1"}) is None
        assert st.take_dispatch_block() == "live_stream"
        assert st.enqueue(live, TASK_PHONE_APP_RESTART, payload={"package": "com.facebook.katana"},
                          target={"serial": "S1"}) is None
        assert st.take_dispatch_block() == "live_stream"
        assert st.list_tasks(node_id=live) == []
        ping = st.enqueue(live, TASK_PING)
        assert ping and ping["kind"] == TASK_PING

        for host, label, group, mid in (
            ("CHINAMI", "机房", "room", "m-c"),
            ("CHINAMI-B", "机房B", "room", "m-cb"),
            ("AORY-A", "AORY-A", "room", "m-aa"),
            ("AORY-B", "AORY-B", "room", "m-ab"),
        ):
            nid = _enroll(st, host, label, group, mid)
            rec = st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook", "like_probe": True},
                             target={"serial": "S1"})
            assert rec and rec["kind"] == TASK_PHONE_LIKE, host
            assert st.take_dispatch_block() == ""

        room = _enroll(st, "CHINAMI", "机房2", "room", "m-c2")
        blocked = st.enqueue(room, "phone_tap", payload={"x": 1, "y": 1}, target={"serial": "3B1FABCDEF"})
        assert blocked is None
        reason = st.take_dispatch_block()
        assert reason == "protected_phone"
        assert "3B1F" not in reason
        st.dispatch_exclude = ["CHINAMI"]
        assert st.enqueue(room, "site_todo", payload={"todos": []}) is None
        assert st.take_dispatch_block() == "excluded"
        st.dispatch_exclude = {"not": "a list"}
        assert st.enqueue(room, "site_todo", payload={"todos": []}) is None
        assert st.take_dispatch_block() == "uncertain"
        assert st.enqueue(room, TASK_PING)["kind"] == TASK_PING
        blob = json.dumps(st.list_tasks(node_id=room))
        assert "3B1FABCDEF" not in blob
    finally:
        st.close()


def test_each_live_signal_alone_blocks_enqueue(tmp_path):
    st = FleetStore(tmp_path / "one.db")
    try:
        cases = (
            ("CHINAMI", "直播机-01", "room", "m-name"),
            ("GANZHI-176", "机房", "room", "m-host"),
            ("CHINAMI", "机房", "live", "m-group"),
        )
        for host, label, group, mid in cases:
            nid = _enroll(st, host, label, group, mid)
            assert st.enqueue(nid, "site_todo", payload={"todos": []}) is None
            assert st.take_dispatch_block() == "live_stream"
    finally:
        st.close()
