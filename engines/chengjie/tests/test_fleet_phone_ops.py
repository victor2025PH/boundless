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
    st.set_remote_ops(nid, True)
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


# ── 3) 节点执行器（假 adb，不碰真机） ──────────────────────────────────────────
import base64 as _b64  # noqa: E402
import struct as _struct  # noqa: E402
import threading as _threading  # noqa: E402
import zlib as _zlib  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

from src.fleet import phone_ops as po  # noqa: E402
from src.fleet.phone_ops import PhoneOps, check_adb_args, screenshot_png  # noqa: E402
from src.fleet.phone_rules import PhoneOpError, escape_input_text, validate_payload  # noqa: E402
from src.fleet.phones import is_protected  # noqa: E402
from src.fleet.protocol import (  # noqa: E402
    STATUS_FAILED, TASK_PHONE_KEY, TASK_PHONE_SWIPE, TASK_PHONE_TEXT,
)

DEVICES = (
    "List of devices attached\n"
    "S1                     device product:gale_global model:23106RN0DA device:gale transport_id:3\n"
    "S2                     device product:gale_global model:23106RN0DA device:gale transport_id:4\n"
    "3B1F4KE5MS140P4X       device product:x model:CPH2653 device:OP5D55L1 transport_id:9\n"
    "192.168.0.148:5555     device product:CPH2653 model:CPH2653 device:OP5D55L1 transport_id:7\n"
    "192.168.0.50:5555      device product:p model:Pixel_7 device:panther transport_id:8\n"
    "U1                     unauthorized transport_id:5\n"
    "M1                     device product:m model:SM_A515F device:a51 transport_id:6\n"
)


def _frame(w=720, h=1600, fmt=1, hdr=12, rgba=(10, 20, 30, 255)):
    head = _struct.pack("<III", w, h, fmt) + (b"\x00" * (hdr - 12))
    return head + bytes(rgba) * (w * h)


class FakeAdb:
    def __init__(self, devices=DEVICES, frame=None, rc=0, stderr=b"", block=None):
        self.devices, self.frame, self.rc, self.stderr, self.block = devices, frame, rc, stderr, block
        self.calls = []

    def __call__(self, cmd, **kw):
        assert cmd[0] == "adb" and kw["shell"] is False and kw["timeout"] > 0
        args = tuple(cmd[1:])
        self.calls.append(args)
        if args == ("version",):
            return _NS(returncode=0, stdout=b"Android Debug Bridge version 1.0.41\nVersion 35.0.2\n", stderr=b"")
        if args == ("devices", "-l"):
            return _NS(returncode=0, stdout=self.devices.encode(), stderr=b"")
        if self.block is not None:
            self.block.wait(5)
        if args[2:] == ("exec-out", "screencap"):
            return _NS(returncode=0, stdout=self.frame if self.frame is not None else _frame(), stderr=b"")
        return _NS(returncode=self.rc, stdout=b"", stderr=self.stderr)

    def actions(self):
        return [c for c in self.calls if c not in (("version",), ("devices", "-l"))]


def _ops(adb=None, server=41, **kw):
    fake = adb or FakeAdb()
    clock = {"t": 100.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    ops = PhoneOps(run=fake, locate=lambda _p: "adb", server_version=lambda _port: server,
                   clock=lambda: clock["t"], sleep=sleep, **kw)
    return ops, fake, slept


@pytest.mark.parametrize("args", [
    ("devices", "-l"), ("version",), ("-s", "S1", "exec-out", "screencap"),
    ("-s", "S1", "shell", "input", "tap", "10", "20"),
    ("-s", "S1", "shell", "input", "swipe", "1", "2", "3", "4", "300"),
    ("-s", "S1", "shell", "input", "text", "hello%sworld"),
    ("-s", "S1", "shell", "input", "keyevent", "3"), ("-s", "S1", "shell", "input", "keyevent", "4"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/dev/tty"),
    ("-s", "S1", "shell", "uiautomator", "dump", "--compressed", "/dev/tty"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/chatx_like_hierarchy.xml"),
    ("-s", "S1", "shell", "uiautomator", "dump", "--compressed", "/sdcard/chatx_like_hierarchy.xml"),
    ("-s", "S1", "pull", "/sdcard/chatx_like_hierarchy.xml", "/tmp/chatx_like_x.xml"),
    ("-s", "S1", "shell", "input", "keyevent", "127"),
    ("-s", "S1", "shell", "cmd", "media_session", "dispatch", "pause"),
])
def test_adb_allowlist_accepts_only_the_fixed_forms(args):
    check_adb_args(args)


@pytest.mark.parametrize("args", [
    ("kill-server",), ("start-server",), ("tcpip", "5555"), ("usb",), ("reboot",), ("devices",),
    ("-s", "S1", "reboot"), ("-s", "S1", "tcpip", "5555"), ("-s", "S1", "usb"), ("connect", "1.2.3.4:5555"),
    ("-s", "S1", "install", "x.apk"), ("-s", "S1", "shell", "rm", "-rf", "/sdcard"),
    ("-s", "S1", "shell", "input", "text", "a;reboot"), ("-s", "S1", "shell", "input", "text", "a%b"),
    ("-s", "S1", "shell", "input", "keyevent", "26"), ("-s", "S1", "exec-out", "screencap", "-p"),
    ("-s", "S1", "shell", "uiautomator", "dump"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/dev/tty", "--compressed"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/chatx_like_hierarchy.xml", "--compressed"),
    ("-s", "S1", "pull", "/sdcard/window_dump.xml", "/tmp/chatx_like_x.xml"),
    ("-s", "S1", "pull", "/sdcard/chatx_like_hierarchy.xml", "/tmp/../x.xml"),
    ("-s", "S1", "shell", "input", "keyevent", "85"),
    ("-s", "S1", "shell", "cmd", "media_session", "dispatch", "play"),
    ("-s", "S1", "exec-out", "uiautomator", "dump", "/dev/tty"),
    ("-s", "S1", "shell", "input", "tap", "10"), ("-s", "S1", "shell", "input", "tap", "-1", "2"),
    ("-s", "3B1F4KE5MS140P4X", "exec-out", "screencap"), ("-s", "192.168.0.148:5555", "exec-out", "screencap"),
    ("-s", "-x", "exec-out", "screencap"), ("-s", "S1", "forward", "tcp:9000", "tcp:9000"),
])
def test_adb_allowlist_rejects_everything_else(args):
    with pytest.raises(PhoneOpError) as ei:
        check_adb_args(args)
    assert ei.value.code == "adb_args_not_allowed"


class _DumpScript(FakeAdb):
    """One outcome per dump. A missing later step repeats the last one."""

    def __init__(self, steps, foreground=b""):
        super().__init__()
        self.steps = list(steps)
        self.dump_n = 0
        self.pending = None
        self.foreground = foreground

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 6 and args[2:6] == ("shell", "dumpsys", "activity", "activities"):
            self.calls.append(args)
            return _NS(returncode=0, stdout=self.foreground, stderr=b"")
        if len(args) >= 5 and args[2:5] == ("shell", "uiautomator", "dump"):
            self.calls.append(args)
            step = self.steps[min(self.dump_n, len(self.steps) - 1)]
            self.dump_n += 1
            self.pending = step.get("file")
            return _NS(returncode=step.get("rc", 0), stdout=step.get("out", b""), stderr=step.get("err", b""))
        if len(args) >= 4 and args[2] == "pull":
            self.calls.append(args)
            if self.pending:
                from pathlib import Path
                Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[-1]).write_bytes(self.pending)
            return _NS(returncode=0, stdout=b"", stderr=b"")
        return FakeAdb.__call__(self, cmd, **kw)


_HIER_XML = b"<hierarchy rotation=\"0\"></hierarchy>"
_DUMPED_TO = b"UI hierchary dumped to: /sdcard/chatx_like_hierarchy.xml\n"


def _read_hierarchy(steps, *, screen=(180, 400), foreground=b""):
    adb = _DumpScript(steps, foreground=foreground)
    ops, _fake, _slept = _ops(adb=adb)
    if screen:
        ops._screen["S1"] = screen
    return ops.read_ui_hierarchy("S1"), adb


def test_hierarchy_pulls_the_fixed_file_and_pauses_once():
    got, adb = _read_hierarchy([{"out": _DUMPED_TO, "file": _HIER_XML}])
    assert got["attempts"] == 1 and got["via"] == "file" and got["compressed"] is False
    assert got["error"] == "" and got["xml"].startswith("<hierarchy")
    dumps = [c for c in adb.calls if len(c) >= 5 and c[2:5] == ("shell", "uiautomator", "dump")]
    assert dumps == [("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/chatx_like_hierarchy.xml")]
    assert any(len(c) >= 4 and c[2] == "pull" and c[3] == "/sdcard/chatx_like_hierarchy.xml" for c in adb.calls)
    assert sum(1 for c in adb.calls if c[2:] == ("shell", "input", "keyevent", "127")) == 1
    assert sum(1 for c in adb.calls if c[2:] == ("shell", "cmd", "media_session", "dispatch", "pause")) == 1
    taps = [c for c in adb.calls if len(c) > 4 and c[4] == "tap"]
    assert taps == []
    assert not any("rm" in c for c in adb.calls)


def test_hierarchy_presses_back_once_when_the_page_left_the_feed():
    permalink = b"mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.PermalinkActivity t9}\n"
    got, adb = _read_hierarchy([{"out": _DUMPED_TO, "file": _HIER_XML}], foreground=permalink)
    assert got["xml"].startswith("<hierarchy")
    assert sum(1 for c in adb.calls if c[2:] == ("shell", "input", "keyevent", "4")) == 1
    assert sum(1 for c in adb.calls if c[2:] == ("shell", "input", "keyevent", "127")) == 1
    assert not any(len(c) > 4 and c[4] == "tap" for c in adb.calls)
    idle = {"rc": 1, "err": b"ERROR: could not get idle state\n", "file": None}
    _got, retried = _read_hierarchy([idle, idle, {"out": _DUMPED_TO, "file": _HIER_XML}], foreground=permalink)
    assert sum(1 for c in retried.calls if c[2:] == ("shell", "input", "keyevent", "4")) == 1


def test_hierarchy_does_not_press_back_on_the_feed_or_an_unknown_page():
    feed = b"mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.FbMainTabActivity t9}\n"
    _got, adb = _read_hierarchy([{"out": _DUMPED_TO, "file": _HIER_XML}], foreground=feed)
    assert not any(c[2:] == ("shell", "input", "keyevent", "4") for c in adb.calls)
    unknown = b"mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.SomeNewActivity t9}\n"
    _got, adb = _read_hierarchy([{"out": _DUMPED_TO, "file": _HIER_XML}], foreground=unknown)
    assert not any(c[2:] == ("shell", "input", "keyevent", "4") for c in adb.calls)
    other = b"mCurrentFocus=Window{abc u0 com.android.systemui/.Something}\n"
    _got, adb = _read_hierarchy([{"out": _DUMPED_TO, "file": _HIER_XML}], foreground=other)
    assert not any(c[2:] == ("shell", "input", "keyevent", "4") for c in adb.calls)


def test_hierarchy_keeps_xml_written_before_the_idle_error():
    got, adb = _read_hierarchy([{
        "rc": 1, "err": b"ERROR: could not get idle state\n", "file": _HIER_XML,
    }])
    assert got["attempts"] == 1 and got["via"] == "file" and got["error"] == ""
    assert got["xml"].startswith("<hierarchy")
    assert adb.dump_n == 1


def test_hierarchy_retries_then_compressed_file():
    idle = {"rc": 1, "err": b"ERROR: could not get idle state\n", "file": None}
    got, adb = _read_hierarchy([idle, idle, {"out": _DUMPED_TO, "file": _HIER_XML}])
    assert got["attempts"] == 3 and got["via"] == "file" and got["compressed"] is True
    assert got["error"] == "" and "<hierarchy" in got["xml"]
    dumps = [c for c in adb.calls if len(c) >= 5 and c[2:5] == ("shell", "uiautomator", "dump")]
    assert len(dumps) == 3
    assert dumps[2][5] == "--compressed" and dumps[2][-1] == "/sdcard/chatx_like_hierarchy.xml"
    assert sum(1 for c in adb.calls if c[-1:] == ("127",)) == 1


def test_hierarchy_falls_back_to_stdout_then_reports_a_scrubbable_error():
    from src.fleet.phone_rules import sanitize_phone_result
    from src.fleet.protocol import TASK_PHONE_LIKE

    idle = {"rc": 1, "err": b"ERROR: could not get idle state\n", "file": None}
    stdout = {"rc": 0, "out": _HIER_XML, "file": None}
    got, _adb = _read_hierarchy([idle, idle, idle, stdout])
    assert got["attempts"] == 4 and got["via"] == "stdout" and got["compressed"] is False
    assert got["xml"].startswith("<hierarchy") and got["error"] == ""

    serial = "ABCDEFGH1234"
    failed, _adb2 = _read_hierarchy([{
        "rc": 1, "err": f"error: device '{serial}' not found\n".encode(), "file": None,
    }])
    assert failed["attempts"] == 5 and failed["via"] == "stdout" and failed["compressed"] is True
    assert serial in failed["error"] and "[redacted]" not in failed["error"]
    assert "could not get idle state" not in failed["error"]
    diag = {
        "like_probe": True, "serial": serial,
        "like_diag": {
            "uiautomator": {
                "dump": "bad", "like_found": False,
                "attempts": failed["attempts"], "via": failed["via"],
                "compressed": failed["compressed"], "error": failed["error"],
            },
            "template_score": 0.318, "template_matched": False,
            "structure_matched": True, "structure_bounds": "[10,200][160,240]",
            "shape_matched": False, "shape_score": 0.42, "position_matched": False,
            "nodes": [], "png_b64": "iVBORw0KGgoAAA", "secret": "nope",
        },
    }
    clean = sanitize_phone_result(TASK_PHONE_LIKE, diag, wallpaper="07")
    blob = __import__("json").dumps(clean["like_diag"])
    assert serial not in blob and "png" not in blob and "secret" not in blob
    assert "07" in clean["like_diag"]["uiautomator"]["error"]
    assert clean["like_diag"]["uiautomator"]["attempts"] == 5
    assert clean["like_diag"]["uiautomator"]["via"] == "stdout"
    assert clean["like_diag"]["uiautomator"]["compressed"] is True
    assert clean["like_diag"]["shape_score"] == 0.42
    assert clean["like_diag"]["shape_matched"] is False
    assert clean["like_diag"]["structure_bounds"] == "[10,200][160,240]"
    unknown = sanitize_phone_result(TASK_PHONE_LIKE, diag, wallpaper="")
    assert "[redacted]" in unknown["like_diag"]["uiautomator"]["error"]
    assert serial not in __import__("json").dumps(unknown["like_diag"])
    bad = sanitize_phone_result(TASK_PHONE_LIKE, {
        "like_probe": True,
        "like_diag": {
            "uiautomator": {"dump": "bad", "attempts": 99, "via": "socket", "compressed": "yes", "error": "x<y>"},
            "structure_bounds": "nope", "shape_score": True,
        },
    })
    assert bad["like_diag"]["uiautomator"]["attempts"] == 20
    assert bad["like_diag"]["uiautomator"]["via"] == ""
    assert bad["like_diag"]["uiautomator"]["compressed"] is False
    assert "<" not in bad["like_diag"]["uiautomator"]["error"]
    assert bad["like_diag"]["structure_bounds"] == ""
    assert bad["like_diag"]["shape_score"] == 0.0


def test_protected_phone_is_hard_coded():
    for s in ("3B1F4KE5MS140P4X", "3b1f4ke5ms140p4x", "adb-3B1F4KE5MS140P4X-abc._adb-tls-connect._tcp",
              "192.168.0.148:5555", "192.168.0.148:37123", "192.168.0.148"):
        assert is_protected(s), s
    for s in ("S1", "192.168.0.14:5555", "192.168.0.1480:5555", "E6FYKRHYS48HZLAM"):
        assert not is_protected(s), s


@pytest.mark.parametrize("kind,payload,code", [
    (TASK_PHONE_TAP, {"x": -1, "y": 2}, "bad_x"), (TASK_PHONE_TAP, {"x": True, "y": 2}, "bad_x"),
    (TASK_PHONE_TAP, {"x": 1, "y": 10000}, "bad_y"), (TASK_PHONE_TAP, {"x": "1", "y": 2}, "bad_x"),
    (TASK_PHONE_SWIPE, {"x1": 1, "y1": 1, "x2": 2, "y2": 2, "duration_ms": 10}, "bad_duration_ms"),
    (TASK_PHONE_TEXT, {"text": ""}, "bad_text"), (TASK_PHONE_TEXT, {"text": "x" * 201}, "text_too_long"),
    (TASK_PHONE_TEXT, {"text": "你好"}, "text_non_ascii_unsupported"), (TASK_PHONE_TEXT, {"text": "a;b"}, "text_bad_chars"),
    (TASK_PHONE_TEXT, {"text": "$(reboot)"}, "text_bad_chars"), (TASK_PHONE_TEXT, {"text": "-e x"}, "text_bad_chars"),
    (TASK_PHONE_KEY, {"key": "power"}, "bad_key"),
])
def test_validate_payload_rejects(kind, payload, code):
    with pytest.raises(PhoneOpError) as ei:
        validate_payload(kind, payload)
    assert ei.value.code == code


def test_validate_payload_normalizes():
    assert validate_payload(TASK_PHONE_TAP, {"x": 5.0, "y": 7, "evil": 1}) == {"x": 5, "y": 7}
    assert validate_payload(TASK_PHONE_SWIPE, {"x1": 1, "y1": 2, "x2": 3, "y2": 4})["duration_ms"] == 300
    assert validate_payload(TASK_PHONE_TEXT, {"text": "user@mail.com 123"}) == {"text": "user@mail.com 123"}
    assert validate_payload(TASK_PHONE_KEY, {"key": "HOME"}) == {"key": "home"}
    assert validate_payload(TASK_PHONE_SCREENSHOT, {"anything": 1}) == {}
    assert escape_input_text("a b  c") == "a%sb%s%sc"


@pytest.mark.parametrize("serial,code", [
    ("3B1F4KE5MS140P4X", "protected_phone"), ("192.168.0.148:5555", "protected_phone"),
    ("adb-3B1F4KE5MS140P4X-x._adb-tls-connect._tcp", "protected_phone"), ("", "bad_serial"), ("-s", "bad_serial"),
    ("NOPE", "phone_not_found"), ("U1", "phone_not_ready:unauthorized"), ("192.168.0.50:5555", "tcp_phone_not_allowed"),
])
def test_execute_rejects_bad_targets_without_touching_the_phone(serial, code):
    ops, fake, _ = _ops()
    st_, res, detail = ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": serial})
    assert (st_, detail) == (STATUS_REJECTED, code)
    assert fake.actions() == []


def test_execute_honours_phones_exclude_serial_prefix_and_model():
    ops, fake, _ = _ops(exclude=["S2", "192.168.0.50:*", "model:SM_A515F"], allow_tcp=True)
    for serial in ("S2", "192.168.0.50:5555", "M1"):
        assert ops.execute(TASK_PHONE_SCREENSHOT, {}, {"serial": serial})[2] == "excluded_phone", serial
    assert fake.actions() == []
    assert ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "S1"})[0] == STATUS_DONE


def test_execute_tcp_phone_only_when_allowed():
    ops, fake, _ = _ops(allow_tcp=True)
    assert ops.execute(TASK_PHONE_KEY, {"key": "back"}, {"serial": "192.168.0.50:5555"})[0] == STATUS_DONE
    assert fake.actions() == [("-s", "192.168.0.50:5555", "shell", "input", "keyevent", "4")]


@pytest.mark.parametrize("server,locate,code", [(None, "adb", "adb_server_not_running"), (40, "adb", "adb_version_mismatch"),
                                                (41, "", "adb_not_found")])
def test_execute_never_starts_or_restarts_the_adb_server(server, locate, code):
    fake = FakeAdb()
    ops = PhoneOps(run=fake, locate=lambda _p: locate, server_version=lambda _port: server)
    assert ops.execute(TASK_PHONE_SCREENSHOT, {}, {"serial": "S1"})[1:] == ({"serial": "S1"}, code)
    assert all(c in (("version",),) for c in fake.calls)          # no devices/-s commands at all


def test_tap_swipe_text_key_argv_and_pacing():
    ops, fake, slept = _ops()
    assert ops.execute(TASK_PHONE_TAP, {"x": 100, "y": 200}, {"serial": "S1"})[:2] == (
        STATUS_DONE, {"x": 100, "y": 200, "serial": "S1", "elapsed_ms": 0})
    ops.execute(TASK_PHONE_SWIPE, {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 250}, {"serial": "S1"})
    ops.execute(TASK_PHONE_TEXT, {"text": "hi there"}, {"serial": "S1"})
    ops.execute(TASK_PHONE_KEY, {"key": "home"}, {"serial": "S1"})
    assert fake.actions() == [
        ("-s", "S1", "shell", "input", "tap", "100", "200"),
        ("-s", "S1", "shell", "input", "swipe", "1", "2", "3", "4", "250"),
        ("-s", "S1", "shell", "input", "text", "hi%sthere"),
        ("-s", "S1", "shell", "input", "keyevent", "3"),
    ]
    assert slept == [po.MIN_INTERVAL_SEC] * 3        # back-to-back ops on one phone wait 0.5 s


def test_screenshot_png_downscaled_and_bounds_learned():
    ops, fake, _ = _ops()
    st_, res, detail = ops.execute(TASK_PHONE_SCREENSHOT, {}, {"serial": "S1"})
    assert (st_, detail) == (STATUS_DONE, "ok")
    assert (res["device_width"], res["device_height"], res["scale"], res["width"], res["height"]) == (720, 1600, 2, 360, 800)
    png = _b64.b64decode(res["png_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and _struct.unpack(">II", png[16:24]) == (360, 800)
    assert ops.execute(TASK_PHONE_TAP, {"x": 1700, "y": 5}, {"serial": "S1"})[2] == "out_of_bounds"
    assert ops.execute(TASK_PHONE_TAP, {"x": 1500, "y": 700}, {"serial": "S1"})[0] == STATUS_DONE   # landscape ok


def _png_first_pixel(png):
    idat = png.index(b"IDAT")
    n = _struct.unpack(">I", png[idat - 4:idat])[0]
    rows = _zlib.decompress(png[idat + 4:idat + 4 + n])
    return tuple(rows[1:4])


def test_screenshot_formats_and_headers():
    png, w, h, dw, dh, scale = screenshot_png(_frame(100, 50, fmt=5, hdr=16, rgba=(1, 2, 3, 255)))
    assert (w, h, dw, dh, scale) == (100, 50, 100, 50, 1) and _png_first_pixel(png) == (3, 2, 1)
    png, *_ = screenshot_png(_frame(100, 50, fmt=1, rgba=(1, 2, 3, 255)))
    assert _png_first_pixel(png) == (1, 2, 3)
    for bad in (b"", b"x" * 11, _frame(10, 10)[:-1], _struct.pack("<III", 0, 10, 1)):
        with pytest.raises(PhoneOpError) as ei:
            screenshot_png(bad)
        assert ei.value.code == "screencap_bad_frame" and ei.value.failed
    with pytest.raises(PhoneOpError) as ei:
        screenshot_png(_frame(10, 10, fmt=4))
    assert ei.value.code == "screencap_format_4"


def test_screenshot_keeps_shrinking_until_under_the_cap():
    import os
    noise = _struct.pack("<III", 256, 256, 1) + os.urandom(256 * 256 * 4)
    png, w, h, dw, dh, scale = screenshot_png(noise, max_side=1024, max_b64=40_000)
    assert scale > 1 and len(_b64.b64encode(png)) <= 40_000
    with pytest.raises(PhoneOpError) as ei:
        screenshot_png(noise, max_b64=10)
    assert ei.value.code == "screenshot_too_large"


def test_adb_failure_is_failed_with_stderr():
    ops, fake, _ = _ops(adb=FakeAdb(rc=1, stderr=b"error: device offline\n"))
    st_, res, detail = ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "S1"})
    assert (st_, detail) == (STATUS_FAILED, "adb_exit_1") and res["stderr"] == "error: device offline"


def test_agent_scrubs_serial_from_phone_task_error_before_ack(tmp_path, monkeypatch):
    from src.fleet import agent as agent_mod
    from src.fleet.protocol import STATUS_FAILED

    serial = "TESTSERIAL01"
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data["wallpaper_map"] = {serial: "07"}
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    phrase = f"error: device '{serial}' not found"
    ag.phone_ops.execute = lambda *a, **k: (STATUS_FAILED, {"serial": serial, "stderr": phrase}, phrase)
    status, result, detail = ag.execute({
        "kind": TASK_PHONE_TAP, "payload": {"x": 1, "y": 1}, "target": {"serial": serial},
    })
    assert status == STATUS_FAILED
    assert serial not in result["stderr"] and serial not in detail
    assert "07" in result["stderr"] and "07" in detail
    assert result["serial"] == serial


def test_disabled_ops_advertise_nothing_and_reject():
    ops, fake, _ = _ops(enabled=False)
    assert ops.caps() == []
    assert ops.execute(TASK_PHONE_SCREENSHOT, {}, {"serial": "S1"})[2] == "phone_ops_disabled"
    assert fake.calls == []
    ops.configure(enabled=True)
    assert ops.caps() == [CAP_PHONE_OPS_V1]


def test_per_phone_lock_serializes_and_times_out(monkeypatch):
    monkeypatch.setattr(po, "LOCK_WAIT_SEC", 0.2)
    gate = _threading.Event()
    ops = PhoneOps(run=FakeAdb(block=gate), locate=lambda _p: "adb", server_version=lambda _port: 41)
    out = {}
    t = _threading.Thread(target=lambda: out.setdefault("a", ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "S1"})))
    t.start()
    import time as _t
    _t.sleep(0.05)
    assert ops.execute(TASK_PHONE_TAP, {"x": 2, "y": 2}, {"serial": "S1"})[2] == "phone_busy"
    gate.set()
    t.join(5)
    assert out["a"][0] == STATUS_DONE


def test_unknown_kind_and_garbage_target():
    ops, fake, _ = _ops()
    assert ops.execute("phone_install", {}, {"serial": "S1"})[2] == "unknown_kind:phone_install"
    assert ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, "S1")[2] == "bad_serial"


def test_opt_in_phone_op_brings_bundled_server_up_then_taps(monkeypatch):
    from src.fleet import adb_bundle

    bundled = "D:/ChatX Agent/platform-tools/adb.exe"
    monkeypatch.setattr(adb_bundle, "bundled_adb_candidates", lambda: (bundled,))
    probes = {"n": 0}

    def probe(_port):
        probes["n"] += 1
        return None if probes["n"] == 1 else 41

    class _Run:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, **kw):
            assert kw["shell"] is False and kw["timeout"] > 0
            self.calls.append(tuple(cmd))
            args = tuple(cmd[1:])
            if args == ("start-server",):
                assert cmd[0] == bundled and kw["timeout"] == adb_bundle.START_TIMEOUT_SEC
                return _NS(returncode=0, stdout=b"", stderr=b"")
            if args == ("version",):
                return _NS(returncode=0, stdout=b"Android Debug Bridge version 1.0.41\nVersion 35.0.2\n", stderr=b"")
            if args == ("devices", "-l"):
                return _NS(returncode=0, stdout=DEVICES.encode(), stderr=b"")
            return _NS(returncode=0, stdout=b"", stderr=b"")

    fake = _Run()
    ops = PhoneOps(run=fake, locate=lambda _p: bundled, server_version=probe, manage_server=True,
                   live_stream=False, clock=lambda: 100.0, sleep=lambda _s: None)
    status, result, detail = ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "S1"})
    assert (status, detail) == (STATUS_DONE, "ok") and result["serial"] == "S1"
    assert fake.calls[0] == (bundled, "start-server")
    n = len(fake.calls)
    assert ops.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "3B1F4KE5MS140P4X"})[2] == "protected_phone"
    assert len(fake.calls) == n


def test_phone_ops_live_stream_and_default_do_not_bring_a_server_up(monkeypatch):
    from src.fleet import adb_bundle

    bundled = "D:/ChatX Agent/platform-tools/adb.exe"
    monkeypatch.setattr(adb_bundle, "bundled_adb_candidates", lambda: (bundled,))

    class _Run:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, **kw):
            self.calls.append(tuple(cmd))
            return _NS(returncode=0, stdout=b"", stderr=b"")

    live_run = _Run()
    live = PhoneOps(run=live_run, locate=lambda _p: bundled, server_version=lambda _p: None,
                    manage_server=True, live_stream=True)
    assert live.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "S1"})[2] == "adb_server_not_running"
    assert live_run.calls == []
    off = _Run()
    default = PhoneOps(run=off, locate=lambda _p: bundled, server_version=lambda _p: None, manage_server=False)
    assert default.execute(TASK_PHONE_TAP, {"x": 1, "y": 1}, {"serial": "S1"})[2] == "adb_server_not_running"
    assert off.calls == []


def test_new_modules_have_no_dangerous_adb_or_live_port_tokens():
    root = _Path(po.__file__).resolve().parent
    for name in ("phone_ops.py", "phone_rules.py"):
        code = (root / name).read_text(encoding="utf-8")
        body = code.split('"""', 2)[2]          # skip the module docstring
        for bad in ("kill-server", "start-server", "tcpip", "reboot", "9000", "connect", "install", "\"usb\"", "root"):
            assert bad not in body.replace("adb_args_not_allowed", ""), (name, bad)


# ── 3b) 主控手机操作端点 + 节点端到端 ──────────────────────────────────────────
from tests.test_fleet_control import _FakeNet  # noqa: E402

PNG_B64 = _b64.b64encode(screenshot_png(_frame(20, 10))[0]).decode()
PHONES_HB = [{"serial": "S1", "state": "device", "model": "23106RN0DA", "transport": "usb"},
             {"serial": "U1", "state": "unauthorized", "model": "", "transport": "usb"}]


def _capable(st, mid="m-cap", phones=PHONES_HB, remote_ops=True):
    import time as _t
    nid = _enroll(st, mid=mid)["node_id"]
    st.heartbeat(nid, {"agent_version": "0.3.7", "proto_version": 1, "caps": [CAP_PHONE_OPS_V1], "phones": phones},
                 now=_t.time())
    if remote_ops and hasattr(st, "set_remote_ops"):
        st.set_remote_ops(nid, True)
    return nid


def test_phone_endpoint_queues_validated_task(st):
    c = _client(st)
    nid = _capable(st)
    r = c.post(f"/api/fleet/nodes/{nid}/phones/S1/tap", headers=OP, json={"x": 10, "y": 20, "evil": "rm"})
    assert r.status_code == 200, r.text
    t = r.json()["task"]
    assert t["kind"] == TASK_PHONE_TAP and t["target"] == {"serial": "S1"} and t["payload"] == {"x": 10, "y": 20}
    assert t["expires_at"] - t["created_at"] == pytest.approx(60)
    assert t["status"] == STATUS_QUEUED


@pytest.mark.parametrize("serial,op,body,code,detail", [
    ("3B1F4KE5MS140P4X", "screenshot", {}, 403, "protected_phone"),
    ("192.168.0.148:5555", "tap", {"x": 1, "y": 1}, 403, "protected_phone"),
    ("S1", "install", {}, 400, "bad_op"), ("S1", "tap", {"x": 1}, 400, "bad_y"),
    ("S1", "text", {"text": "a;reboot"}, 400, "text_bad_chars"),
    ("NOPE", "screenshot", {}, 409, "phone_not_reported"), ("U1", "tap", {"x": 1, "y": 1}, 409, "phone_not_ready:unauthorized"),
])
def test_phone_endpoint_rejects(st, serial, op, body, code, detail):
    c = _client(st)
    nid = _capable(st)
    r = c.post(f"/api/fleet/nodes/{nid}/phones/{serial}/{op}", headers=OP, json=body)
    assert (r.status_code, r.json()["detail"]) == (code, detail)
    assert st.list_tasks(node_id=nid) == []


def test_phone_endpoint_needs_operator_cap_and_online_node(st):
    c = _client(st)
    nid = _capable(st)
    assert c.post(f"/api/fleet/nodes/{nid}/phones/S1/screenshot", json={}).status_code == 401
    assert c.post("/api/fleet/nodes/n_nope/phones/S1/screenshot", headers=OP, json={}).status_code == 404
    old = _enroll(st, mid="m-old")["node_id"]
    st.heartbeat(old, {"agent_version": "0.3.6", "proto_version": 1, "phones": PHONES_HB}, now=T0)
    r = c.post(f"/api/fleet/nodes/{old}/phones/S1/screenshot", headers=OP, json={})
    assert (r.status_code, r.json()["detail"]) == (409, "node_lacks_cap:phone_ops_v1")
    stale = _enroll(st, mid="m-stale")["node_id"]
    import time as _t
    st.heartbeat(stale, {"caps": [CAP_PHONE_OPS_V1], "phones": PHONES_HB}, now=_t.time() - 10_000)
    if hasattr(st, "set_remote_ops"):
        st.set_remote_ops(stale, True)
    r = c.post(f"/api/fleet/nodes/{stale}/phones/S1/screenshot", headers=OP, json={})
    assert (r.status_code, r.json()["detail"]) == (409, "node_offline")


def test_generic_task_route_refuses_phone_kinds(st):
    c = _client(st)
    nid = _capable(st)
    r = c.post(f"/api/fleet/nodes/{nid}/tasks", headers=OP, json={"kind": TASK_PHONE_TAP, "payload": {"x": 1, "y": 1},
                                                                  "target": {"serial": "3B1F4KE5MS140P4X"}})
    assert (r.status_code, r.json()["detail"]) == (400, "phone_ops_use_phones_endpoint")


def test_ack_sanitizes_phone_results_and_list_strips_png(st):
    c = _client(st)
    nid = _capable(st)
    t = c.post(f"/api/fleet/nodes/{nid}/phones/S1/screenshot", headers=OP, json={}).json()["task"]
    key = st.enroll  # noqa: F841
    st.pull(nid)
    rec = st.ack(t["task_id"], node_id=nid, status=STATUS_DONE, result={"png_b64": PNG_B64}, now=None)
    assert rec["result"]["png_b64"] == PNG_B64
    # through the route: junk keys and a non-PNG payload are dropped
    from src.fleet.phone_rules import sanitize_phone_result
    clean = sanitize_phone_result(TASK_PHONE_SCREENSHOT, {"png_b64": "PHN2Zz4=", "width": 3, "html": "<x>", "scale": True})
    assert clean == {"width": 3}
    listed = c.get(f"/api/fleet/tasks?node_id={nid}", headers=OP).json()["tasks"][0]["result"]
    assert listed["png_b64"] == "" and listed["has_png"] is True
    full = c.get(f"/api/fleet/tasks/{t['task_id']}", headers=OP).json()["task"]["result"]
    assert full["png_b64"] == PNG_B64


def test_agent_end_to_end_screenshot_via_controller(st, tmp_path, monkeypatch):
    from src.fleet.agent import AgentConfig, NodeAgent
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    client = _client(st)
    net = _FakeNet(client)
    cfg = AgentConfig(tmp_path / "state")
    agent = NodeAgent(cfg, http=net, app_version="t")
    fake = FakeAdb()
    agent.phone_ops = PhoneOps(run=fake, locate=lambda _p: "adb", server_version=lambda _port: 41)
    agent.phones.collect = lambda: ([dict(p) for p in PHONES_HB], "")
    code = client.post("/api/fleet/enroll-codes", json={}, headers=OP).json()["code"]
    agent.enroll(code, controller_url="https://ctl.test/fleet")
    hb = agent.build_heartbeat()
    assert hb["caps"] == [CAP_PHONE_OPS_V1]
    agent.heartbeat()
    nid = cfg.node_id
    if hasattr(st, "set_remote_ops"):
        st.set_remote_ops(nid, True)
    t = client.post(f"/api/fleet/nodes/{nid}/phones/S1/screenshot", headers=OP, json={}).json()["task"]
    out = agent.run_once(wait=0)
    assert out["tasks"][0]["status"] == STATUS_DONE
    rec = st.get_task(t["task_id"])
    assert rec["status"] == STATUS_DONE and rec["result"]["png_b64"].startswith("iVBORw0KGgo")
    assert rec["result"]["device_width"] == 720 and fake.actions() == [("-s", "S1", "exec-out", "screencap")]


# ── 4) 审计 + 节点「允许远程操作」开关（缺省关） ────────────────────────────────
def test_remote_ops_default_off_and_toggle_route(st):
    c = _client(st)
    nid = _capable(st, remote_ops=False)
    assert st.get_node(nid)["remote_ops_enabled"] is False
    r = c.post(f"/api/fleet/nodes/{nid}/phones/S1/screenshot", headers=OP, json={})
    assert (r.status_code, r.json()["detail"]) == (409, "remote_ops_disabled")
    assert st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}) is None
    assert c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": "yes"}).status_code == 400
    assert c.post(f"/api/fleet/nodes/{nid}", json={"remote_ops_enabled": True}).status_code == 401
    r = c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": True})
    assert r.status_code == 200 and r.json()["node"]["remote_ops_enabled"] is True
    assert st.get_node(nid)["label"] == "机器A"          # label untouched by the toggle
    assert c.post(f"/api/fleet/nodes/{nid}/phones/S1/screenshot", headers=OP, json={}).status_code == 200
    assert c.post("/api/fleet/nodes/n_nope", headers=OP, json={"remote_ops_enabled": True}).status_code == 404


def test_disabling_remote_ops_cancels_queued_and_pull_rejects(st):
    nid = _capable(st)
    a = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"})
    st.set_remote_ops(nid, False)
    assert st.get_task(a["task_id"])["status"] == "cancelled"
    st.set_remote_ops(nid, True)
    b = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"})
    # flip the flag behind the store's back (e.g. a second controller process) → pull still refuses
    import json as _json
    st._conn.execute("UPDATE nodes SET meta_json=? WHERE node_id=?", (_json.dumps({"remote_ops_enabled": False}), nid))
    st._conn.commit()
    assert st.pull(nid) == []
    assert st.get_task(b["task_id"])["detail"] == "remote_ops_disabled"


def test_actor_header_recorded_for_audit(st):
    c = _client(st)
    nid = _capable(st)
    h = dict(OP, **{"X-Fleet-Actor": "zhituo:alice<script>"})
    t = c.post(f"/api/fleet/nodes/{nid}/phones/S1/tap", headers=h, json={"x": 1, "y": 2}).json()["task"]
    assert t["created_by"] == "operator via zhituo:alicescript"
    t2 = c.post(f"/api/fleet/nodes/{nid}/phones/S1/key", headers=OP, json={"key": "back"}).json()["task"]
    assert t2["created_by"] == "operator"
    audit = c.get(f"/api/fleet/tasks?node_id={nid}&kind={TASK_PHONE_TAP}", headers=OP).json()["tasks"]
    assert [(x["created_by"], x["target"]["serial"], x["payload"]) for x in audit] == [
        ("operator via zhituo:alicescript", "S1", {"x": 1, "y": 2})]


def test_app_restart_endpoint_queues_only_facebook_on_an_operable_phone(st, caplog):
    import logging

    c = _client(st)
    nid = _capable(st)
    caplog.set_level(logging.INFO)
    r = c.post(f"/api/fleet/nodes/{nid}/phones/S1/app_restart", headers=OP, json={})
    assert r.status_code == 200, r.text
    t = r.json()["task"]
    assert t["kind"] == "phone_app_restart"
    assert t["payload"] == {"package": "com.facebook.katana"}
    assert t["target"] == {"serial": "S1"}
    assert t["created_by"] == "operator"
    logged = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("app_restart")]
    assert logged and "S1" not in logged[0] and "com.facebook.katana" in logged[0] and "[redacted]" in logged[0]
    lite = c.post(f"/api/fleet/nodes/{nid}/phones/S1/app_restart", headers=OP,
                  json={"package": "com.facebook.lite"}).json()["task"]
    assert lite["payload"] == {"package": "com.facebook.lite"}
    bad = c.post(f"/api/fleet/nodes/{nid}/phones/S1/app_restart", headers=OP, json={"package": "com.android.settings"})
    assert (bad.status_code, bad.json()["detail"]) == (400, "bad_package")
    generic = c.post(f"/api/fleet/nodes/{nid}/tasks", headers=OP, json={"kind": "phone_app_restart", "payload": {}})
    assert (generic.status_code, generic.json()["detail"]) == (400, "phone_ops_use_phones_endpoint")


def test_app_restart_refuses_a_phone_outside_the_operable_pool(st):
    c = _client(st)
    nid = _capable(st)
    missing = c.post(f"/api/fleet/nodes/{nid}/phones/NOPE/app_restart", headers=OP, json={})
    assert (missing.status_code, missing.json()["detail"]) == (409, "phone_not_reported")
    offline = c.post(f"/api/fleet/nodes/{nid}/phones/U1/app_restart", headers=OP, json={})
    assert (offline.status_code, offline.json()["detail"]) == (409, "phone_not_ready:unauthorized")
    protected = c.post(f"/api/fleet/nodes/{nid}/phones/3B1F4KE5MS140P4X/app_restart", headers=OP, json={})
    assert (protected.status_code, protected.json()["detail"]) == (403, "protected_phone")


def test_app_restart_refuses_live_stream_and_seat_173(st):
    c = _client(st)
    live = _enroll(st, mid="m-live", group_name="直播")["node_id"]
    import time as _t
    st.heartbeat(live, {"agent_version": "0.3.23", "proto_version": 1, "caps": [CAP_PHONE_OPS_V1], "phones": PHONES_HB},
                 now=_t.time())
    st.set_remote_ops(live, True)
    r = c.post(f"/api/fleet/nodes/{live}/phones/S1/app_restart", headers=OP, json={})
    assert (r.status_code, r.json()["detail"]) == (409, "live_stream_host")
    seat = _capable(st, mid="m-173")
    st._conn.execute("UPDATE nodes SET host_name=? WHERE node_id=?", ("YUYAN-173", seat))
    st._conn.commit()
    r = c.post(f"/api/fleet/nodes/{seat}/phones/S1/app_restart", headers=OP, json={})
    assert (r.status_code, r.json()["detail"]) == (409, "seat_173")


def test_agent_app_restart_stops_then_launches_and_scrubs_the_serial(tmp_path, monkeypatch, caplog):
    import logging

    from src.fleet import agent as agent_mod
    from src.fleet.protocol import STATUS_DONE, STATUS_REJECTED

    serial = "TESTSERIAL01"
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    monkeypatch.setattr(agent_mod, "host_name", lambda: "PC-A")
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data["wallpaper_map"] = {serial: "09"}
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    devices = (
        "List of devices attached\n"
        f"{serial}             device product:g model:23106RN0DA device:gale transport_id:1\n"
    )
    fake = FakeAdb(devices=devices)
    ag.phone_ops = PhoneOps(run=fake, locate=lambda _p: "adb", server_version=lambda _port: 41,
                            clock=lambda: 100.0, sleep=lambda _s: None)
    caplog.set_level(logging.INFO)
    status, result, detail = ag.execute({
        "kind": "phone_app_restart",
        "payload": {},
        "target": {"serial": serial},
    })
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["package"] == "com.facebook.katana" and result["stopped"] is True and result["launched"] is True
    shells = [c[2:] for c in fake.actions()]
    assert ("shell", "am", "force-stop", "com.facebook.katana") in shells
    assert ("shell", "am", "start", "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER", "-p", "com.facebook.katana") in shells
    logged = [rec.getMessage() for rec in caplog.records if "app_restart" in rec.getMessage()]
    assert logged and serial not in " ".join(logged) and "09" in logged[-1]
    n = len(fake.calls)
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: True)
    status, _result, detail = ag.execute({
        "kind": "phone_app_restart", "payload": {"package": "com.facebook.katana"},
        "target": {"serial": serial},
    })
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert len(fake.calls) == n
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    monkeypatch.setattr(agent_mod, "host_name", lambda: "YUYAN-173")
    status, _result, detail = ag.execute({
        "kind": "phone_app_restart", "payload": {"package": "com.facebook.lite"},
        "target": {"serial": serial},
    })
    assert (status, detail) == (STATUS_REJECTED, "seat_173")
    assert len(fake.calls) == n
    assert ag.phone_ops.execute_app_restart(
        {"package": "com.facebook.katana"}, {"serial": "3B1F4KE5MS140P4X"},
    )[2] == "protected_phone"
    assert len(fake.calls) == n


# ── 5) agent.json 热加载 / save 不盖手工改动 / 单文件孤儿进程 ──────────────────
import json as _json5  # noqa: E402

from src.fleet import agent as _agent_mod  # noqa: E402
from src.fleet.agent import AgentConfig as _Cfg, NodeAgent as _Agent  # noqa: E402
from src.fleet.service import onefile_parent_pid, start_parent_watch  # noqa: E402


def _edit_disk(cfg, **changes):
    d = _json5.loads(cfg.path.read_text(encoding="utf-8"))
    for k, v in changes.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    cfg.path.write_text(_json5.dumps(d), encoding="utf-8")
    import os as _os
    st_ = cfg.path.stat()
    _os.utime(cfg.path, ns=(st_.st_atime_ns, st_.st_mtime_ns + 5_000_000))


def _saved_cfg(tmp_path, monkeypatch, **data):
    monkeypatch.setattr(_agent_mod, "state_dir_is_locked", lambda _d: True)
    cfg = _Cfg(tmp_path / "state")
    cfg.data.update(data)
    cfg.save()
    return cfg


def test_save_keeps_manual_operator_edits(tmp_path, monkeypatch):
    cfg = _saved_cfg(tmp_path, monkeypatch, phones_exclude=["A", "model:CPH2653"], adb_path="C:/x/adb.exe")
    _edit_disk(cfg, phones_exclude=["A"], adb_path=None)          # a person removes the model rule and adb_path
    cfg.data["heartbeat_sec"] = 45                                # the agent changes its own key
    cfg.save()
    disk = _json5.loads(cfg.path.read_text(encoding="utf-8"))
    assert disk["phones_exclude"] == ["A"] and "adb_path" not in disk and disk["heartbeat_sec"] == 45
    assert cfg.data["phones_exclude"] == ["A"]


def test_refresh_ignores_untrusted_state_dir(tmp_path, monkeypatch):
    cfg = _saved_cfg(tmp_path, monkeypatch, phones_exclude=["A"])
    _edit_disk(cfg, phones_exclude=[], adb_path="C:/evil/adb.exe")
    monkeypatch.setattr(_agent_mod, "state_dir_is_locked", lambda _d: False)
    assert cfg.refresh_operator_keys() is False and cfg.data["phones_exclude"] == ["A"] and "adb_path" not in cfg.data


def test_agent_hot_reloads_phone_settings_on_heartbeat(tmp_path, monkeypatch, st):
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    cfg = _saved_cfg(tmp_path, monkeypatch, phones_exclude=["S1", "model:CPH2653"])
    client = _client(st)
    agent = _Agent(cfg, http=_FakeNet(client), app_version="t")
    code = client.post("/api/fleet/enroll-codes", json={}, headers=OP).json()["code"]
    agent.enroll(code, controller_url="https://ctl.test/fleet")
    assert "MODEL:CPH2653" in agent.phones.excludes and "S1" in agent.phone_ops.excludes
    agent.heartbeat()
    assert agent.reload_operator_config() is False                # nothing changed on disk
    _edit_disk(cfg, phones_exclude=["S1"], phone_ops_enabled=False)
    agent.heartbeat()
    assert "MODEL:CPH2653" not in agent.phones.excludes and "3B1F4KE5MS140P4X" in agent.phones.excludes
    assert agent.phone_ops.enabled is False and agent.build_heartbeat()["caps"] == []
    disk = _json5.loads(cfg.path.read_text(encoding="utf-8"))
    assert disk["phones_exclude"] == ["S1"] and disk["node_key"] == cfg.node_key


def test_onefile_parent_pid_detection():
    kw = dict(windows=True, frozen=True, executable=r"C:\Program Files\ChatX Agent\chatx-agent.exe", getppid=lambda: 4242)
    assert onefile_parent_pid(meipass=r"C:\Windows\Temp\_MEI12345", **kw) == 4242
    assert onefile_parent_pid(meipass=r"C:\Program Files\ChatX Agent", **kw) == 0          # onedir
    assert onefile_parent_pid(meipass="", **kw) == 0
    assert onefile_parent_pid(meipass=r"C:\T\_MEI1", windows=False, frozen=True, getppid=lambda: 1) == 0
    assert onefile_parent_pid(meipass=r"C:\T\_MEI1", windows=True, frozen=False, getppid=lambda: 1) == 0


def test_parent_watch_exits_when_bootloader_goes_away():
    exe = r"C:\Program Files\ChatX Agent\chatx-agent.exe"
    exits = []
    ok = start_parent_watch(ppid=4242, opener=lambda pid: ("h", exe.upper()), waiter=lambda h: None,
                            exit_fn=exits.append, executable=exe, background=False)
    assert ok and exits == [0]
    exits.clear()
    assert start_parent_watch(ppid=4242, opener=lambda pid: ("h", r"C:\Windows\explorer.exe"), waiter=lambda h: None,
                              exit_fn=exits.append, executable=exe, background=False) is False
    assert start_parent_watch(ppid=4242, opener=lambda pid: None, exit_fn=exits.append, executable=exe) is False
    assert start_parent_watch(ppid=0, exit_fn=exits.append) is False
    assert exits == []


# ── 7) 控制台：手机列 / 原因码中文 / 手机任务审计人 ────────────────────────────
import shutil as _sh7  # noqa: E402
import subprocess as _sp7  # noqa: E402

_CONSOLE7 = _Path(__file__).resolve().parents[1] / "domains" / "fleet_control" / "web" / "templates" / "fleet_console.html"
_HARNESS7 = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {}, calls = [];
function el(id) {
  if (!els[id]) els[id] = {id, textContent: '', innerHTML: '', value: '', className: '', style: {}, attrs: {}, handlers: {},
    disabled: false, setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; }, addEventListener(ev, fn) { this.handlers[ev] = fn; },
    scrollIntoView() {}, focus() {}, select() {}};
  return els[id];
}
global.document = {getElementById: el};
global.setInterval = () => 0; global.clearTimeout = () => {}; global.setTimeout = () => 1;
const now = Date.now() / 1000;
const step = JSON.parse(process.argv[3]);
function reply(d) { return Promise.resolve({ok: true, json: () => Promise.resolve(d)}); }
global.apiFetch = (url, opt) => {
  calls.push([(opt && opt.method) || 'GET', url, opt && opt.body ? JSON.parse(opt.body) : null]);
  if (url.startsWith('/api/fleet/overview')) return reply({nodes: {total: 3, online: 3}});
  if (url.startsWith('/api/fleet/nodes?')) return reply({nodes: step.nodes});
  if (url.startsWith('/api/fleet/tasks?')) return reply({tasks: step.tasks || []});
  return reply({pending: [], room_keys: [], ok: true});
};
eval(js);
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  for (let i = 0; i < 5; i++) await tick();
  if (step.click) {
    const btn = {getAttribute: (k) => ({'data-n': step.click, 'data-k': '__remote'})[k]};
    el('fc-list').handlers.click({target: {closest: () => btn}});
    for (let i = 0; i < 5; i++) await tick();
    if (step.confirm) el('fc-dlg-ok').handlers.click(); else el('fc-dlg-cancel').handlers.click();
    for (let i = 0; i < 5; i++) await tick();
  }
  console.log(JSON.stringify({list: el('fc-list').innerHTML, tasks: el('fc-tasks').innerHTML, calls,
    dlg: el('fc-dlg-body').innerHTML, toast: el('fc-toast').textContent}));
})();
"""


def _console_run(tmp_path, step):
    node = _sh7.which("node")
    if not node:
        pytest.skip("node not installed")
    h = tmp_path / "h7.js"
    h.write_text(_HARNESS7, encoding="utf-8")
    out = _sp7.run([node, str(h), str(_CONSOLE7), _json5.dumps(step)], capture_output=True, text=True, timeout=60,
                   encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return _json5.loads(out.stdout.strip().splitlines()[-1])


def _cnode(nid, **kw):
    row = {"node_id": nid, "status": "active", "state": "online", "label": nid.upper(), "last_seen": 1.9e9}
    row.update(kw)
    return row


def test_console_phones_column_readable_errors(tmp_path):
    nodes = [
        _cnode("n1", phones=[{"serial": "E6FY", "model": "23106RN0DA", "state": "device"},
                             {"serial": "HQ79", "model": "23106RN0DA", "state": "unauthorized"}], phones_error=""),
        _cnode("n2", phones=[], phones_error="adb_server_not_running"),
        _cnode("n3"),
    ]
    tasks = [{"task_id": "t1", "node_id": "n1", "kind": "phone_tap", "status": "rejected", "detail": "remote_ops_disabled",
              "created_by": "operator via zhituo:alice", "created_at": 1.9e9, "result": {}}]
    res = _console_run(tmp_path, {"nodes": nodes, "tasks": tasks})
    assert "2 台（可用 1）" in res["list"]
    assert "adb 服务没在运行" in res["list"] and 'title="adb_server_not_running"' in res["list"]
    assert "未上报" in res["list"]
    assert "手机点击" in res["tasks"] and "没开远程操作" in res["tasks"] and "zhituo:alice" in res["tasks"]


# ── 8) 控制台：远程操作开关按钮 / 窄屏布局 / 加载态 ────────────────────────────
def test_console_remote_ops_toggle_on_needs_confirm(tmp_path):
    nodes = [_cnode("n1", caps=["phone_ops_v1"], remote_ops_enabled=False), _cnode("n2"),
             _cnode("n3", caps=["phone_ops_v1"], remote_ops_enabled=True)]
    res = _console_run(tmp_path, {"nodes": nodes, "click": "n1", "confirm": True})
    assert res["list"].count("开启远程操作") == 1 and res["list"].count("关闭远程操作") == 1
    assert "远程操作已开" in res["list"]
    assert "受保护的直播机除外" in res["dlg"]
    posts = [c for c in res["calls"] if c[0] == "POST"]
    assert posts == [["POST", "/api/fleet/nodes/n1", {"remote_ops_enabled": True}]]
    assert "已开启" in res["toast"]
    cancelled = _console_run(tmp_path, {"nodes": nodes, "click": "n1", "confirm": False})
    assert [c for c in cancelled["calls"] if c[0] == "POST"] == []


def test_console_remote_ops_toggle_off_posts_directly(tmp_path):
    nodes = [_cnode("n3", caps=["phone_ops_v1"], remote_ops_enabled=True)]
    res = _console_run(tmp_path, {"nodes": nodes, "click": "n3", "confirm": False})
    assert [c for c in res["calls"] if c[0] == "POST"] == [["POST", "/api/fleet/nodes/n3", {"remote_ops_enabled": False}]]
    assert "已关闭" in res["toast"] and "已取消" in res["toast"]


def test_console_mobile_layout_and_loading_rows():
    html = _CONSOLE7.read_text(encoding="utf-8")
    assert "@media (max-width:760px)" in html and 'class="fc-top"' in html
    for tb, span in (("fc-pending", 8), ("fc-rooms", 6), ("fc-list", 9), ("fc-tasks", 5)):
        assert f'<tbody id="{tb}"><tr class="fc-loading"><td colspan="{span}">加载中…</td></tr></tbody>' in html
    assert "loadFailRows(" in html
