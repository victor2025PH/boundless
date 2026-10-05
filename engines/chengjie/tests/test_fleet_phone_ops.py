"""0.3.7 远程手机操作：能力协商 / 结果上限 / 节点执行器 / 审计 / 配置热加载（进程内，不碰真机）。

    python -m pytest tests/test_fleet_phone_ops.py -q -p no:cacheprovider
"""
from __future__ import annotations

import pytest

from src.fleet.protocol import (
    CAP_PHONE_OPS_V1, HEARTBEAT_KEYS, PHONE_TASK_KINDS, STATUS_DONE, STATUS_QUEUED, STATUS_REJECTED, TASK_KINDS,
    TASK_PHONE_SCREENSHOT, TASK_PHONE_TAP, TASK_PING, TASK_PRIORITY, TASK_UPGRADE, missing_cap, sanitize_caps,
    sanitize_heartbeat,
)
from tests.test_fleet_control import OP, T0, _clean, _client, _enroll, st  # noqa: F401  (fixtures)

CAPS_HB = {"agent_version": "0.3.7", "proto_version": 1, "caps": [CAP_PHONE_OPS_V1]}


def _node_with_caps(st, caps=(CAP_PHONE_OPS_V1,), mid="m-aaaa"):
    nid = _enroll(st, mid=mid)["node_id"]
    st.heartbeat(nid, {"agent_version": "0.3.7", "proto_version": 1, "caps": list(caps)}, now=T0 + 1)
    return nid


# ── 1) 能力协商 ───────────────────────────────────────────────────────────────
def test_protocol_phone_kinds_appended_without_touching_old_ones():
    for k in PHONE_TASK_KINDS:
        assert k in TASK_KINDS and TASK_PRIORITY[k] == 6
    assert TASK_KINDS[:9][0] == TASK_PING and TASK_UPGRADE in TASK_KINDS
    assert len(set(TASK_KINDS)) == len(TASK_KINDS)
    assert "caps" in HEARTBEAT_KEYS


@pytest.mark.parametrize("raw,want", [
    (["phone_ops_v1", "PHONE_OPS_V1", "x y", "", 3, "a" * 40, "ok_2"], ["phone_ops_v1", "ok_2"]),
    ("phone_ops_v1", []), (None, []), ({"phone_ops_v1": 1}, []),
    ([f"c{i}" for i in range(40)], [f"c{i}" for i in range(16)]),
])
def test_sanitize_caps_shapes(raw, want):
    assert sanitize_caps(raw) == want


def test_sanitize_heartbeat_keeps_caps_only_clean():
    hb = sanitize_heartbeat({"caps": ["phone_ops_v1", "<script>"], "evil": 1})
    assert hb == {"caps": ["phone_ops_v1"]}


def test_missing_cap_only_for_phone_kinds():
    assert missing_cap(TASK_PHONE_TAP, []) == CAP_PHONE_OPS_V1
    assert missing_cap(TASK_PHONE_TAP, [CAP_PHONE_OPS_V1]) == ""
    assert missing_cap(TASK_PING, []) == "" and missing_cap(TASK_UPGRADE, None) == ""


def test_store_node_exposes_caps_and_refuses_without_cap(st):
    old = _enroll(st, mid="m-old")["node_id"]
    st.heartbeat(old, {"agent_version": "0.3.6", "proto_version": 1, "phones": []}, now=T0 + 1)
    assert st.get_node(old, now=T0 + 2)["caps"] == []
    assert st.task_refusal(old, TASK_PHONE_SCREENSHOT) == "node_lacks_cap:phone_ops_v1"
    assert st.task_refusal(old, TASK_PING) == ""
    assert st.task_refusal(old, "rm_rf") == "bad_kind"
    assert st.task_refusal("n_nope", TASK_PING) == "node_not_found"
    assert st.enqueue(old, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}, now=T0 + 2) is None
    assert st.enqueue(old, TASK_PING, now=T0 + 2) is not None
    new = _node_with_caps(st, mid="m-new")
    assert st.get_node(new, now=T0 + 2)["caps"] == [CAP_PHONE_OPS_V1]
    assert st.task_refusal(new, TASK_PHONE_SCREENSHOT) == ""
    st.revoke(new, now=T0 + 3)
    assert st.task_refusal(new, TASK_PHONE_SCREENSHOT) == "node_revoked"


def test_pull_rejects_phone_task_when_node_drops_cap(st):
    nid = _node_with_caps(st)
    t = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}, now=T0 + 2)
    p = st.enqueue(nid, TASK_PING, now=T0 + 2)
    st.heartbeat(nid, {"agent_version": "0.3.6", "proto_version": 1}, now=T0 + 3)   # downgraded: no caps
    got = st.pull(nid, now=T0 + 4)
    assert [x["task_id"] for x in got] == [p["task_id"]]
    rec = st.get_task(t["task_id"])
    assert rec["status"] == STATUS_REJECTED and rec["detail"] == "node_lacks_cap:phone_ops_v1"


def test_pull_delivers_phone_task_to_capable_node(st):
    nid = _node_with_caps(st)
    t = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}, now=T0 + 2)
    got = st.pull(nid, now=T0 + 3)
    assert [x["task_id"] for x in got] == [t["task_id"]]


def test_generic_task_route_409_for_node_without_cap(st):
    c = _client(st)
    nid = _enroll(st, mid="m-old")["node_id"]
    r = c.post(f"/api/fleet/nodes/{nid}/tasks", headers=OP, json={"kind": TASK_PHONE_TAP, "payload": {"x": 1, "y": 1}})
    assert r.status_code == 409 and r.json()["detail"] == "node_lacks_cap:phone_ops_v1"
    assert st.list_tasks(node_id=nid) == []
    assert c.post(f"/api/fleet/nodes/{nid}/tasks", headers=OP, json={"kind": TASK_PING}).status_code == 200


# ── 2) 回传结果上限 + 保留期 ───────────────────────────────────────────────────
from src.fleet.protocol import (  # noqa: E402
    PRUNED_RESULT, RESULT_MAX_BYTES, SCREENSHOT_RESULT_KEEP_SEC, TASK_RESULT_KEEP_SEC, bound_result, result_limit,
)


def test_bound_result_limits_by_kind():
    assert result_limit(TASK_PING) == RESULT_MAX_BYTES and result_limit(TASK_PHONE_SCREENSHOT) > 1_000_000
    small = {"pong": True}
    assert bound_result(TASK_PING, small) is small
    big = {"blob": "x" * (RESULT_MAX_BYTES + 10)}
    out = bound_result(TASK_PING, big)
    assert out["error"] == "result_too_large" and out["limit"] == RESULT_MAX_BYTES and out["bytes"] > RESULT_MAX_BYTES
    assert bound_result(TASK_PHONE_SCREENSHOT, big) is big
    assert bound_result(TASK_PHONE_SCREENSHOT, {"png_b64": "A" * 2_000_000})["error"] == "result_too_large"
    assert bound_result(TASK_PING, "nope") == {} and bound_result(TASK_PING, {"x": {1, 2}}) == {"error": "result_not_json"}


def test_ack_stores_bounded_result(st):
    nid = _enroll(st)["node_id"]
    t = st.enqueue(nid, TASK_PING, now=T0)
    st.pull(nid, now=T0 + 1)
    rec = st.ack(t["task_id"], node_id=nid, status=STATUS_DONE, result={"blob": "y" * 100_000}, now=T0 + 2)
    assert rec["result"]["error"] == "result_too_large"


def test_prune_clears_old_screenshots_after_an_hour_and_others_after_a_week(st):
    nid = _node_with_caps(st)
    shot = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}, now=T0 + 2)
    ping = st.enqueue(nid, TASK_PING, now=T0 + 2)
    st.pull(nid, limit=5, now=T0 + 3)
    st.ack(shot["task_id"], node_id=nid, status=STATUS_DONE, result={"png_b64": "QUFB", "width": 1}, now=T0 + 4)
    st.ack(ping["task_id"], node_id=nid, status=STATUS_DONE, result={"pong": True}, now=T0 + 4)
    assert st.prune_results(now=T0 + 4 + SCREENSHOT_RESULT_KEEP_SEC - 5) == 0
    assert st.prune_results(now=T0 + 5 + SCREENSHOT_RESULT_KEEP_SEC) == 1
    assert st.get_task(shot["task_id"])["result"] == PRUNED_RESULT
    assert st.get_task(shot["task_id"])["status"] == STATUS_DONE          # row kept for audit
    assert st.get_task(ping["task_id"])["result"] == {"pong": True}
    assert st.prune_results(now=T0 + 5 + TASK_RESULT_KEEP_SEC) == 1
    assert st.get_task(ping["task_id"])["result"] == PRUNED_RESULT
    assert st.prune_results(now=T0 + 10 + TASK_RESULT_KEEP_SEC) == 0


def test_ack_triggers_throttled_prune(st):
    nid = _node_with_caps(st)
    shot = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}, now=T0 + 2)
    st.pull(nid, now=T0 + 3)
    st.ack(shot["task_id"], node_id=nid, status=STATUS_DONE, result={"png_b64": "QUFB"}, now=T0 + 4)
    later = st.enqueue(nid, TASK_PING, now=T0 + 2 * SCREENSHOT_RESULT_KEEP_SEC)
    st.pull(nid, now=T0 + 2 * SCREENSHOT_RESULT_KEEP_SEC + 1)
    st.ack(later["task_id"], node_id=nid, status=STATUS_DONE, result={}, now=T0 + 2 * SCREENSHOT_RESULT_KEEP_SEC + 2)
    assert st.get_task(shot["task_id"])["result"] == PRUNED_RESULT
