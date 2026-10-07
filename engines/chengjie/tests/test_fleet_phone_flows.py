"""0.3.8 社交动作：Facebook / Instagram / TikTok 的发帖、点赞、评论、关注。

进程内 mock adb，不碰真机，也不碰受保护的直播机。
"""
from __future__ import annotations

import json
import os
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fleet.phone_flow_rules import kind_for_flow, validate_flow_payload
from src.fleet.phone_flows import PhoneFlows, bundled_ui_map, compile_flow, validate_ui_map
from src.fleet.phone_ops import MIN_INTERVAL_SEC, PhoneOps
from src.fleet.phone_rules import PhoneOpError, escape_input_text, sanitize_phone_result
from src.fleet.protocol import (
    CAP_PHONE_FLOWS_V1, CAP_PHONE_OPS_V1, FLOW_NAMES, PHONE_FLOW_KINDS, PHONE_FLOW_TTL_SEC, PHONE_TASK_KINDS,
    SOCIAL_APPS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, TASK_KINDS, TASK_PHONE_COMMENT, TASK_PHONE_FOLLOW,
    TASK_PHONE_KEY, TASK_PHONE_LIKE, TASK_PHONE_POST, TASK_PHONE_SWIPE, TASK_PHONE_TAP, TASK_PHONE_TEXT, TASK_PING,
    TASK_PRIORITY, missing_cap,
)
from tests.test_fleet_control import OP, _FakeNet, _client, _enroll, st  # noqa: F401

APPS = ("facebook", "instagram", "tiktok")
FLOWS = ("post", "like", "comment", "follow")
KIND = {"post": TASK_PHONE_POST, "like": TASK_PHONE_LIKE, "comment": TASK_PHONE_COMMENT, "follow": TASK_PHONE_FOLLOW}
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
W, H = 180, 400


def _frame(w=W, h=H, fmt=1, hdr=12):
    head = struct.pack("<III", w, h, fmt) + (b"\x00" * (hdr - 12))
    return head + bytes((10, 20, 30, 255)) * (w * h)


class FakeAdb:
    def __init__(self, devices=DEVICES, frame=None, rc=0, stderr=b"", block=None):
        self.devices, self.frame, self.rc, self.stderr, self.block = devices, frame, rc, stderr, block
        self.calls = []

    def __call__(self, cmd, **kw):
        assert cmd[0] == "adb" and kw["shell"] is False and kw["timeout"] > 0
        args = tuple(cmd[1:])
        self.calls.append(args)
        if args == ("version",):
            return SimpleNamespace(returncode=0, stdout=b"Android Debug Bridge version 1.0.41\nVersion 35.0.2\n", stderr=b"")
        if args == ("devices", "-l"):
            return SimpleNamespace(returncode=0, stdout=self.devices.encode(), stderr=b"")
        if self.block is not None:
            self.block.wait(5)
        if len(args) >= 4 and args[2:] == ("exec-out", "screencap"):
            return SimpleNamespace(returncode=0, stdout=self.frame if self.frame is not None else _frame(), stderr=b"")
        return SimpleNamespace(returncode=self.rc, stdout=b"", stderr=self.stderr)

    def actions(self):
        return [c for c in self.calls if c not in (("version",), ("devices", "-l"))]


def _ops(adb=None, **kw):
    fake = adb or FakeAdb(frame=_frame())
    clock = {"t": 100.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    ops = PhoneOps(run=fake, locate=lambda _p: "adb", server_version=lambda _port: 41,
                   clock=lambda: clock["t"], sleep=sleep, **kw)
    return ops, fake, slept


def _body(app, flow, **kw):
    p = {"app": app}
    if flow in ("post", "comment"):
        p["text"] = kw.pop("text", "hello fleet")
    if flow == "post":
        p["media"] = kw.pop("media", 0)
        if "media_slot" in kw:
            p["media_slot"] = kw.pop("media_slot")
    if flow in ("like", "comment"):
        p["scrolls"] = kw.pop("scrolls", 0)
    if flow == "follow":
        p["handle"] = kw.pop("handle", "some.user")
    p.update(kw)
    return p


def _assert_actions(actions, compiled):
    assert actions and actions[0][2:] == ("exec-out", "screencap")
    rest = actions[1:]
    assert len(rest) == len(compiled)
    for args, (kind, p) in zip(rest, compiled):
        if kind == TASK_PHONE_KEY:
            code = "3" if p["key"] == "home" else "4"
            assert args[2:] == ("shell", "input", "keyevent", code)
        elif kind == TASK_PHONE_TAP:
            assert args[2:] == ("shell", "input", "tap", str(p["x"]), str(p["y"]))
        elif kind == TASK_PHONE_SWIPE:
            vals = tuple(str(p[k]) for k in ("x1", "y1", "x2", "y2", "duration_ms"))
            assert args[2:] == ("shell", "input", "swipe") + vals
        else:
            assert kind == TASK_PHONE_TEXT
            assert args[2:] == ("shell", "input", "text", escape_input_text(p["text"]))
    for args in actions:
        tail = args[2:]
        ok = tail == ("exec-out", "screencap") or (
            len(tail) >= 3 and tail[:2] == ("shell", "input") and tail[2] in ("tap", "swipe", "text", "keyevent"))
        assert ok, args
        blob = " ".join(args)
        for bad in ("install", "push", "reboot", "tcpip", "am "):
            assert bad not in blob


def _flow_node(st, caps, *, remote=True, phones=None, mid="m-flow"):
    nid = _enroll(st, mid=mid)["node_id"]
    if phones is None:
        phones = [{"serial": "S1", "state": "device", "model": "Pixel", "transport": "usb"}]
    st.heartbeat(nid, {"agent_version": "0.3.8", "proto_version": 1, "caps": list(caps), "phones": phones},
                 now=time.time())
    if remote:
        st.set_remote_ops(nid, True)
    return nid


# ── 协议 ──────────────────────────────────────────────────────────────────────
def test_flow_kinds_are_appended_and_need_their_own_cap():
    assert len(PHONE_TASK_KINDS) == 5
    assert TASK_KINDS[0] == TASK_PING
    assert list(PHONE_FLOW_KINDS) == [TASK_PHONE_POST, TASK_PHONE_LIKE, TASK_PHONE_COMMENT, TASK_PHONE_FOLLOW]
    for k in PHONE_FLOW_KINDS:
        assert k in TASK_KINDS and TASK_PRIORITY[k] == 6
    assert missing_cap(TASK_PHONE_POST, [CAP_PHONE_OPS_V1]) == CAP_PHONE_FLOWS_V1
    assert missing_cap(TASK_PHONE_POST, [CAP_PHONE_FLOWS_V1]) == ""
    assert missing_cap(TASK_PHONE_TAP, [CAP_PHONE_FLOWS_V1]) == CAP_PHONE_OPS_V1
    assert SOCIAL_APPS == ("facebook", "instagram", "tiktok")
    assert FLOW_NAMES == ("post", "like", "comment", "follow")
    assert kind_for_flow("Post") == TASK_PHONE_POST and kind_for_flow("nope") == ""


# ── 参数 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind,payload,code", [
    (TASK_PHONE_POST, {"text": "hi"}, "bad_app"),
    (TASK_PHONE_POST, {"app": "whatsapp", "text": "hi"}, "bad_app"),
    (TASK_PHONE_POST, {"app": "facebook"}, "bad_text"),
    (TASK_PHONE_POST, {"app": "facebook", "text": ""}, "bad_text"),
    (TASK_PHONE_COMMENT, {"app": "tiktok", "text": "你好"}, "text_non_ascii_unsupported"),
    (TASK_PHONE_POST, {"app": "facebook", "text": "a;reboot"}, "text_bad_chars"),
    (TASK_PHONE_FOLLOW, {"app": "instagram", "handle": ""}, "bad_handle"),
    (TASK_PHONE_FOLLOW, {"app": "instagram", "handle": "bad name"}, "bad_handle"),
    (TASK_PHONE_FOLLOW, {"app": "instagram", "handle": "a" * 65}, "bad_handle"),
    (TASK_PHONE_LIKE, {"app": "facebook", "scrolls": 9}, "bad_scrolls"),
    (TASK_PHONE_LIKE, {"app": "facebook", "scrolls": -1}, "bad_scrolls"),
    (TASK_PHONE_POST, {"app": "facebook", "text": "hi", "media": "yes"}, "bad_media"),
    (TASK_PHONE_POST, {"app": "facebook", "text": "hi", "media_slot": 6}, "bad_media_slot"),
    (TASK_PHONE_LIKE, {"app": "facebook", "anchors": {"Nope": {"x": 1, "y": 2}}}, "bad_anchor"),
    (TASK_PHONE_LIKE, {"app": "facebook", "anchors": {"like_button": {"x": 10000, "y": 1}}}, "bad_anchor"),
])
def test_validate_flow_payload_rejects(kind, payload, code):
    with pytest.raises(PhoneOpError) as ei:
        validate_flow_payload(kind, payload)
    assert ei.value.code == code


def test_validate_flow_payload_normalizes_and_drops_unknown_keys():
    out = validate_flow_payload(TASK_PHONE_POST, {"app": "Facebook", "text": "hello fleet", "evil": "rm", "media": True})
    assert out == {"app": "facebook", "text": "hello fleet", "media": 1, "media_slot": 0}
    like = validate_flow_payload(TASK_PHONE_LIKE, {"app": "TIKTOK", "scrolls": 2.0, "text": "nope"})
    assert like == {"app": "tiktok", "scrolls": 2}
    follow = validate_flow_payload(TASK_PHONE_FOLLOW, {"app": "instagram", "handle": "some.user"})
    assert follow["handle"] == "some.user"


# ── 三个应用各自的坐标 ────────────────────────────────────────────────────────
def test_bundled_map_has_all_apps_and_flows():
    ui = bundled_ui_map()
    assert set(ui["apps"]) == set(APPS)
    for app in APPS:
        flows = ui["apps"][app]["flows"]
        assert set(flows) == {"post", "post_media", "like", "comment", "follow", "warmup", "dm", "watch"}
        for steps in flows.values():
            assert steps[0] == {"op": "key", "key": "home"}
            assert steps[1]["op"] == "tap" and steps[1]["anchor"] == "app_icon"
        assert "gallery_0" not in json.dumps(flows["post"])
        assert "gallery_0" in json.dumps(flows["post_media"])


def test_like_and_icon_coordinates_differ_across_apps():
    ui = bundled_ui_map()
    icons, likes = [], []
    for app in APPS:
        steps = compile_flow(app, "like", {"scrolls": 0}, 720, 1600, ui)
        taps = [p for k, p in steps if k == TASK_PHONE_TAP]
        icons.append((taps[0]["x"], taps[0]["y"]))
        likes.append((taps[-1]["x"], taps[-1]["y"]))
        assert sum(1 for k, _ in steps if k == TASK_PHONE_SWIPE) == 0
        scrolled = compile_flow(app, "like", {"scrolls": 2}, 720, 1600, ui)
        assert sum(1 for k, _ in scrolled if k == TASK_PHONE_SWIPE) == 2
    assert len(set(icons)) == 3 and len(set(likes)) == 3


@pytest.mark.parametrize("app", APPS)
def test_post_comment_follow_text_and_gallery_slot(app):
    ui = bundled_ui_map()
    post = compile_flow(app, "post", {"text": "hello fleet", "media_slot": 0}, 720, 1600, ui)
    media0 = compile_flow(app, "post_media", {"text": "hello fleet", "media": 1, "media_slot": 0}, 720, 1600, ui)
    media2 = compile_flow(app, "post_media", {"text": "hello fleet", "media": 1, "media_slot": 2}, 720, 1600, ui)
    assert [p["text"] for k, p in post if k == TASK_PHONE_TEXT] == ["hello fleet"]
    plain = {(p["x"], p["y"]) for k, p in post if k == TASK_PHONE_TAP}
    g0 = [(p["x"], p["y"]) for k, p in media0 if k == TASK_PHONE_TAP]
    g2 = [(p["x"], p["y"]) for k, p in media2 if k == TASK_PHONE_TAP]
    assert [pt for pt in g0 if pt not in plain]
    assert g0 != g2
    comment = compile_flow(app, "comment", {"text": "nice", "scrolls": 2}, 720, 1600, ui)
    assert [p["text"] for k, p in comment if k == TASK_PHONE_TEXT] == ["nice"]
    assert sum(1 for k, _ in comment if k == TASK_PHONE_SWIPE) == 2
    follow = compile_flow(app, "follow", {"handle": "some.user"}, 720, 1600, ui)
    assert [p["text"] for k, p in follow if k == TASK_PHONE_TEXT] == ["some.user"]


def test_anchor_override_wins_and_unknown_name_is_rejected():
    ui = bundled_ui_map()
    steps = compile_flow("facebook", "like", {"scrolls": 0, "anchors": {"like_button": {"x": 11, "y": 22}}}, 720, 1600, ui)
    assert [p for k, p in steps if k == TASK_PHONE_TAP][-1] == {"x": 11, "y": 22}
    with pytest.raises(PhoneOpError) as ei:
        compile_flow("facebook", "like", {"scrolls": 0, "anchors": {"nope": {"x": 1, "y": 2}}}, 720, 1600, ui)
    assert ei.value.code == "unknown_anchor"


def test_map_schema_is_strict():
    ui = bundled_ui_map()
    broken = json.loads(json.dumps({"version": ui["version"], "apps": {
        app: {"anchors": {n: list(p) for n, p in spec["anchors"].items()},
              "flows": spec["flows"]} for app, spec in ui["apps"].items()}}))
    validate_ui_map(broken)
    missing = json.loads(json.dumps(broken))
    del missing["apps"]["tiktok"]
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(missing)
    assert ei.value.code == "ui_map_invalid"
    extra = json.loads(json.dumps(broken))
    extra["apps"]["facebook"]["flows"]["like"][0]["evil"] = 1
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(extra)
    assert ei.value.code == "ui_map_invalid"


# ── 执行（mock adb）──────────────────────────────────────────────────────────
@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("flow", FLOWS)
def test_execute_each_app_and_flow(app, flow):
    ui = bundled_ui_map()
    payload = validate_flow_payload(KIND[flow], _body(app, flow))
    compiled = compile_flow(app, "post_media" if flow == "post" and payload.get("media") else flow, payload, W, H, ui)
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(KIND[flow], dict(payload, evil="rm"), {"serial": "S1"}, ops=ops)
    if app == "facebook" and flow == "like":
        # Blank frames are not signed in, and the fixed like_button is not a tap target.
        assert (status, detail) == (STATUS_FAILED, "app_not_ready"), detail
        assert result["flow"] == "like"
        ui_fb = bundled_ui_map()["apps"]["facebook"]["anchors"]
        banned = []
        for name in ("like_button", "feed_tab"):
            ax, ay = ui_fb[name]
            banned.append((str(min(W - 1, ax * W // 1000)), str(min(H - 1, ay * H // 1000))))
        taps = [a for a in fake.actions() if len(a) > 4 and a[4] == "tap"]
        assert taps and all((a[-2], a[-1]) not in banned for a in taps)
        return
    assert (status, detail) == (STATUS_DONE, "ok"), (app, flow, detail)
    assert result["app"] == app and result["flow"] == flow
    assert result["steps"] == 1 + len(compiled)
    assert (result["device_width"], result["device_height"]) == (W, H)
    blob = json.dumps(result)
    assert "hello fleet" not in blob and "some.user" not in blob
    if flow in ("post", "comment"):
        assert result["chars"] == len(payload["text"])
    if flow == "follow":
        assert result["chars"] == len(payload["handle"])
    _assert_actions(fake.actions(), compiled)
    assert slept == [MIN_INTERVAL_SEC] * len(compiled)


@pytest.mark.parametrize("app", APPS)
def test_execute_post_with_gallery_slot(app):
    ops, fake, _ = _ops()
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(
        TASK_PHONE_POST, _body(app, "post", media=1, media_slot=2), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["flow"] == "post" and "hello fleet" not in json.dumps(result)
    ui = bundled_ui_map()
    compiled = compile_flow(app, "post_media", {"text": "hello fleet", "media": 1, "media_slot": 2}, W, H, ui)
    plain = compile_flow(app, "post", {"text": "hello fleet", "media_slot": 0}, W, H, ui)
    assert len(compiled) > len(plain)
    _assert_actions(fake.actions(), compiled)


@pytest.mark.parametrize("serial", ["3B1F4KE5MS140P4X", "192.168.0.148:5555"])
def test_protected_phone_never_runs_adb(serial):
    ops, fake, _ = _ops()
    flows = PhoneFlows(enabled=True)
    status, _res, detail = flows.execute(TASK_PHONE_LIKE, _body("facebook", "like"), {"serial": serial}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    assert fake.calls == [] and fake.actions() == []


def test_exclude_and_disabled_do_not_run_actions():
    ops, fake, _ = _ops(exclude=["S2*", "model:SM_A515F"])
    flows = PhoneFlows(enabled=True)
    assert flows.execute(TASK_PHONE_LIKE, _body("instagram", "like"), {"serial": "S2"}, ops=ops)[2] == "excluded_phone"
    assert fake.calls == []
    status, _res, detail = flows.execute(TASK_PHONE_LIKE, _body("instagram", "like"), {"serial": "M1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "excluded_phone")
    assert fake.actions() == []
    off, fake2, _ = _ops()
    assert PhoneFlows(enabled=False).execute(TASK_PHONE_POST, _body("tiktok", "post"), {"serial": "S1"}, ops=off)[2] == "phone_flows_disabled"
    assert fake2.calls == []
    disabled_ops, fake3, _ = _ops(enabled=False)
    got = PhoneFlows(enabled=True).execute(TASK_PHONE_POST, _body("tiktok", "post"), {"serial": "S1"}, ops=disabled_ops)
    assert got[2] == "phone_ops_disabled" and fake3.calls == []


def test_bad_map_and_unknown_anchor_do_not_touch_adb(tmp_path):
    ops, fake, _ = _ops()
    flows = PhoneFlows(enabled=True, ui_map_path=str(tmp_path / "missing.json"))
    assert flows.execute(TASK_PHONE_LIKE, _body("facebook", "like"), {"serial": "S1"}, ops=ops)[2] == "ui_map_missing"
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    flows.configure(enabled=True, ui_map_path=str(bad))
    assert flows.execute(TASK_PHONE_LIKE, _body("facebook", "like"), {"serial": "S1"}, ops=ops)[2] == "ui_map_invalid"
    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{" + b" " * (256 * 1024))
    flows.configure(enabled=True, ui_map_path=str(huge))
    assert flows.execute(TASK_PHONE_LIKE, _body("facebook", "like"), {"serial": "S1"}, ops=ops)[2] == "ui_map_invalid"
    assert fake.calls == []
    flows.configure(enabled=True, ui_map_path="")
    status, _res, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", anchors={"nope": {"x": 1, "y": 2}}), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "unknown_anchor")
    assert fake.calls == []


def test_custom_map_file_changes_the_tap(tmp_path):
    src = json.loads((Path(__file__).resolve().parents[1] / "src" / "fleet" / "phone_ui_map.json").read_text(encoding="utf-8"))
    src["apps"]["instagram"]["anchors"]["like_button"] = [10, 20]
    path = tmp_path / "ui.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    ops, fake, _ = _ops()
    flows = PhoneFlows(enabled=True, ui_map_path=str(path))
    status, _res, detail = flows.execute(TASK_PHONE_LIKE, _body("instagram", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    taps = [a for a in fake.actions() if a[4:5] == ("tap",)]
    want_x = min(W - 1, 10 * W // 1000)
    want_y = min(H - 1, 20 * H // 1000)
    assert (taps[-1][-2], taps[-1][-1]) == (str(want_x), str(want_y))
    src["apps"]["instagram"]["anchors"]["like_button"] = [30, 40]
    path.write_text(json.dumps(src), encoding="utf-8")
    # Same-length JSON can keep mtime_ns, and load_map would serve the cached map.
    bumped = time.time_ns() + 1_000_000
    os.utime(path, ns=(bumped, bumped))
    status, _res, detail = flows.execute(TASK_PHONE_LIKE, _body("instagram", "like"), {"serial": "S1"}, ops=ops)
    assert detail == "ok"
    taps = [a for a in fake.actions() if a[4:5] == ("tap",)]
    assert taps[-1][-2] == str(min(W - 1, 30 * W // 1000))


def test_text_failure_stops_the_flow():
    class Boom(FakeAdb):
        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 5 and args[4] == "text":
                self.rc, self.stderr = 2, b"no ime"
            else:
                self.rc, self.stderr = 0, b""
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, _ = _ops(adb=Boom(frame=_frame()))
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(TASK_PHONE_POST, _body("facebook", "post"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "adb_exit_2")
    assert result["failed_step"] >= 0 and result["completed_steps"] == result["failed_step"] + 1
    assert fake.actions()[-1][4] == "text"
    assert "hello fleet" not in json.dumps(result)


def test_same_phone_lock_covers_the_whole_flow(monkeypatch):
    import src.fleet.phone_ops as po
    monkeypatch.setattr(po, "LOCK_WAIT_SEC", 0.2)
    gate = threading.Event()
    ops, _fake, _ = _ops(adb=FakeAdb(frame=_frame(), block=gate))
    flows = PhoneFlows(enabled=True)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "a", flows.execute(TASK_PHONE_LIKE, _body("tiktok", "like"), {"serial": "S1"}, ops=ops)))
    t.start()
    time.sleep(0.05)
    assert ops.execute(TASK_PHONE_TAP, {"x": 2, "y": 2}, {"serial": "S1"})[2] == "phone_busy"
    gate.set()
    t.join(5)
    assert out["a"][0] == STATUS_DONE


def test_node_busy_when_two_flows_already_hold_slots(monkeypatch):
    import src.fleet.phone_ops as po
    monkeypatch.setattr(po, "LOCK_WAIT_SEC", 0.2)
    gate = threading.Event()
    ops, fake, _ = _ops(adb=FakeAdb(frame=_frame(), block=gate))
    flows = PhoneFlows(enabled=True)
    threads = []
    for serial in ("S1", "S2"):
        threads.append(threading.Thread(target=lambda s=serial: flows.execute(
            TASK_PHONE_LIKE, _body("facebook", "like"), {"serial": s}, ops=ops)))
        threads[-1].start()
    deadline = time.time() + 2
    while time.time() < deadline and sum(1 for c in fake.calls if c[2:] == ("exec-out", "screencap")) < 2:
        time.sleep(0.01)
    status, _res, detail = flows.execute(TASK_PHONE_LIKE, _body("instagram", "like"), {"serial": "M1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "node_busy")
    gate.set()
    for t in threads:
        t.join(5)


# ── 主控队列 / 路由 ───────────────────────────────────────────────────────────
def test_store_gates_flow_tasks_on_cap_and_remote_ops(st):
    nid = _flow_node(st, [CAP_PHONE_OPS_V1], remote=True, mid="m-ops-only")
    assert st.task_refusal(nid, TASK_PHONE_POST) == "node_lacks_cap:phone_flows_v1"
    assert st.enqueue(nid, TASK_PHONE_POST, payload={"app": "facebook"}, target={"serial": "S1"}) is None
    both = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], remote=False, mid="m-both-off")
    assert st.task_refusal(both, TASK_PHONE_LIKE) == "remote_ops_disabled"
    assert st.enqueue(both, TASK_PHONE_LIKE, payload={"app": "tiktok"}, target={"serial": "S1"}) is None
    st.set_remote_ops(both, True)
    rec = st.enqueue(both, TASK_PHONE_COMMENT, payload={"app": "instagram", "text": "hi"}, target={"serial": "S1"})
    assert rec and rec["kind"] == TASK_PHONE_COMMENT
    st.heartbeat(both, {"caps": [CAP_PHONE_OPS_V1], "phones": [{"serial": "S1", "state": "device"}]}, now=time.time())
    assert st.pull(both) == []
    assert st.get_task(rec["task_id"])["detail"] == "node_lacks_cap:phone_flows_v1"
    st.heartbeat(both, {"caps": [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1],
                        "phones": [{"serial": "S1", "state": "device"}]}, now=time.time())
    queued = st.enqueue(both, TASK_PHONE_FOLLOW, payload={"app": "facebook", "handle": "ada"}, target={"serial": "S1"})
    st.set_remote_ops(both, False)
    assert st.get_task(queued["task_id"])["status"] == "cancelled"
    assert st.get_task(queued["task_id"])["detail"] == "remote_ops_disabled"


def test_social_endpoint_queues_each_app(st):
    c = _client(st)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], mid="m-social")
    r = c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/post", headers=OP,
               json={"app": "Instagram", "text": "hello fleet", "media": 1, "media_slot": 2, "evil": "rm"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["kind"] == TASK_PHONE_POST and task["ttl_sec"] == PHONE_FLOW_TTL_SEC
    assert task["payload"] == {"app": "instagram", "text": "hello fleet", "media": 1, "media_slot": 2}
    assert task["target"] == {"serial": "S1"}
    for app, flow in (("facebook", "like"), ("tiktok", "follow"), ("instagram", "comment")):
        body = _body(app, flow)
        rr = c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/{flow}", headers=OP, json=body)
        assert rr.status_code == 200, (app, flow, rr.text)
        assert rr.json()["task"]["kind"] == KIND[flow]
        assert rr.json()["task"]["payload"]["app"] == app


def test_social_endpoint_rejects_protected_bad_input_and_wrong_route(st):
    c = _client(st)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], mid="m-social-rej")
    assert c.post(f"/api/fleet/nodes/{nid}/phones/3B1F4KE5MS140P4X/social/like", headers=OP,
                  json={"app": "facebook"}).status_code == 403
    assert c.post(f"/api/fleet/nodes/{nid}/phones/192.168.0.148:5555/social/like", headers=OP,
                  json={"app": "facebook"}).json()["detail"] == "protected_phone"
    assert c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/post", headers=OP,
                  json={"app": "myspace", "text": "hi"}).json()["detail"] == "bad_app"
    assert c.post(f"/api/fleet/nodes/{nid}/phones/S1/social/dance", headers=OP, json={}).json()["detail"] == "bad_flow"
    only = _flow_node(st, [CAP_PHONE_OPS_V1], mid="m-no-flow-cap")
    r = c.post(f"/api/fleet/nodes/{only}/tasks", headers=OP, json={"kind": "phone_post", "payload": {"app": "facebook"}})
    assert (r.status_code, r.json()["detail"]) == (409, "node_lacks_cap:phone_flows_v1")
    r = c.post(f"/api/fleet/nodes/{nid}/tasks", headers=OP, json={"kind": "phone_like", "payload": {"app": "tiktok"}})
    assert (r.status_code, r.json()["detail"]) == (400, "phone_flows_use_social_endpoint")
    off = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], remote=False, mid="m-remote-off")
    r = c.post(f"/api/fleet/nodes/{off}/phones/S1/social/like", headers=OP, json={"app": "facebook"})
    assert (r.status_code, r.json()["detail"]) == (409, "remote_ops_disabled")


def test_ack_keeps_flow_summary_and_drops_text(st):
    res = _enroll(st, mid="m-ack-flow")
    nid, key = res["node_id"], res["node_key"]
    st.heartbeat(nid, {"caps": [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1]}, now=time.time())
    st.set_remote_ops(nid, True)
    rec = st.enqueue(nid, TASK_PHONE_POST, payload={"app": "facebook", "text": "secret"}, target={"serial": "S1"})
    c = _client(st)
    r = c.post("/api/fleet/tasks/ack", headers={"Authorization": f"Bearer {key}"}, json={
        "task_id": rec["task_id"], "status": "done",
        "result": {"serial": "S1", "app": "facebook", "flow": "post", "steps": 8, "chars": 6,
                   "text": "secret", "handle": "ada", "device_width": 180, "elapsed_ms": 12},
    })
    assert r.status_code == 200
    stored = st.get_task(rec["task_id"])["result"]
    assert stored["app"] == "facebook" and stored["flow"] == "post" and stored["steps"] == 8
    assert "text" not in stored and "handle" not in stored and "secret" not in json.dumps(stored)
    clean = sanitize_phone_result(TASK_PHONE_POST, {"app": "tiktok", "flow": "like", "steps": 4, "text": "nope"})
    assert clean == {"app": "tiktok", "flow": "like", "steps": 4}


def test_ack_scrubs_raw_serial_from_phone_task_errors(st):
    serial = "TESTSERIAL01"
    res = _enroll(st, mid="m-ack-serial")
    nid, key = res["node_id"], res["node_key"]
    st.heartbeat(nid, {"caps": [CAP_PHONE_OPS_V1]}, now=time.time())
    st.set_remote_ops(nid, True)
    phrase = f"error: device '{serial}' not found"
    rec = st.enqueue(nid, TASK_PHONE_TAP, payload={"x": 1, "y": 1},
                     target={"serial": serial, "wallpaper": "07"})
    c = _client(st)
    r = c.post("/api/fleet/tasks/ack", headers={"Authorization": f"Bearer {key}"}, json={
        "task_id": rec["task_id"], "status": "failed", "detail": phrase,
        "result": {"serial": serial, "stderr": phrase, "error": phrase},
    })
    assert r.status_code == 200
    stored = st.get_task(rec["task_id"])
    assert stored["result"]["serial"] == serial
    assert serial not in stored["result"]["stderr"]
    assert serial not in stored["result"]["error"]
    assert serial not in stored["detail"]
    assert "07" in stored["result"]["stderr"] and "07" in stored["detail"]
    bare = sanitize_phone_result(TASK_PHONE_TAP, {"stderr": phrase})
    assert serial not in bare["stderr"] and "[redacted]" in bare["stderr"]


# ── 节点 ──────────────────────────────────────────────────────────────────────
def test_agent_caps_default_off_and_run_social_flow(st, tmp_path, monkeypatch):
    from src.fleet.agent import AGENT_VERSION, AgentConfig, NodeAgent
    assert AGENT_VERSION == "0.3.20"
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    bare = AgentConfig(tmp_path / "bare")
    bare.data.update({"controller_url": "http://127.0.0.1:1", "instances": []})
    bare_agent = NodeAgent(bare, http=lambda *a, **k: (200, {}), app_version="t")
    assert bare_agent.build_heartbeat()["caps"] == [CAP_PHONE_OPS_V1]
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
    assert CAP_PHONE_OPS_V1 in hb["caps"] and CAP_PHONE_FLOWS_V1 in hb["caps"]
    agent.heartbeat()
    nid = cfg.node_id
    st.set_remote_ops(nid, True)
    task = client.post(f"/api/fleet/nodes/{nid}/phones/S1/social/comment", headers=OP,
                       json={"app": "tiktok", "text": "hello fleet", "scrolls": 1}).json()["task"]
    out = agent.run_once(wait=0)
    assert out["tasks"][0]["status"] == STATUS_DONE
    rec = st.get_task(task["task_id"])
    assert rec["status"] == STATUS_DONE
    assert rec["result"]["app"] == "tiktok" and rec["result"]["flow"] == "comment"
    assert "hello fleet" not in json.dumps(rec["result"])
    assert any(a[2:] == ("exec-out", "screencap") for a in fake.actions())
    assert any(a[4:5] == ("text",) for a in fake.actions())


def test_agent_hot_reloads_phone_flows(tmp_path, monkeypatch, st):
    from src.fleet import agent as agent_mod
    from src.fleet.agent import AgentConfig, NodeAgent
    monkeypatch.setattr(agent_mod, "state_dir_is_locked", lambda _d: True)
    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    cfg = AgentConfig(tmp_path / "state")
    cfg.data.update({"controller_url": "http://127.0.0.1:1", "instances": []})
    cfg.save()
    client = _client(st)
    agent = NodeAgent(cfg, http=_FakeNet(client), app_version="t")
    code = client.post("/api/fleet/enroll-codes", json={}, headers=OP).json()["code"]
    agent.enroll(code, controller_url="https://ctl.test/fleet")
    assert CAP_PHONE_FLOWS_V1 not in agent.build_heartbeat()["caps"]

    def edit(**changes):
        d = json.loads(cfg.path.read_text(encoding="utf-8"))
        for k, v in changes.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        cfg.path.write_text(json.dumps(d), encoding="utf-8")
        stt = cfg.path.stat()
        import os
        os.utime(cfg.path, ns=(stt.st_atime_ns, stt.st_mtime_ns + 5_000_000))

    edit(phone_flows_enabled=True)
    agent.heartbeat()
    assert agent.phone_flows.enabled is True
    assert agent.build_heartbeat()["caps"] == [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1, "phone_flows_v2"]
    edit(phone_ops_enabled=False)
    agent.heartbeat()
    assert agent.build_heartbeat()["caps"] == []
    edit(phone_ops_enabled=True, phone_flows_enabled=None)
    agent.heartbeat()
    assert agent.phone_flows.enabled is False
    assert CAP_PHONE_FLOWS_V1 not in agent.build_heartbeat()["caps"]


# ── 控制台 ────────────────────────────────────────────────────────────────────
_CONSOLE = Path(__file__).resolve().parents[1] / "domains" / "fleet_control" / "web" / "templates" / "fleet_console.html"
_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {}, calls = [];
function el(id) {
  if (!els[id]) els[id] = {id, textContent: '', innerHTML: '', value: '', className: '', style: {}, attrs: {}, handlers: {},
    disabled: false, checked: false, setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; }, addEventListener(ev, fn) { this.handlers[ev] = fn; },
    scrollIntoView() {}, focus() {}, select() {}};
  return els[id];
}
global.document = {getElementById: el};
global.setInterval = () => 0; global.clearTimeout = () => {}; global.setTimeout = () => 1;
const step = JSON.parse(process.argv[3]);
function reply(d) { return Promise.resolve({ok: true, json: () => Promise.resolve(d)}); }
global.apiFetch = (url, opt) => {
  calls.push([(opt && opt.method) || 'GET', url, opt && opt.body ? JSON.parse(opt.body) : null]);
  if (url.startsWith('/api/fleet/overview')) return reply({nodes: {total: 1, online: 1}});
  if (url.startsWith('/api/fleet/nodes?')) return reply({nodes: step.nodes});
  if (url.startsWith('/api/fleet/tasks?')) return reply({tasks: step.tasks || []});
  return reply({pending: [], room_keys: [], ok: true});
};
eval(js);
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  for (let i = 0; i < 5; i++) await tick();
  if (step.social) {
    const btn = {getAttribute: (k) => ({'data-n': step.social.node, 'data-k': '__social'})[k]};
    el('fc-list').handlers.click({target: {closest: () => btn}});
    for (let i = 0; i < 3; i++) await tick();
    const fields = step.social.fields || {};
    Object.keys(fields).forEach((id) => { el(id).value = String(fields[id]); });
    if (step.social.media) el('fc-soc-media').checked = true;
    el('fc-dlg-ok').handlers.click();
    for (let i = 0; i < 5; i++) await tick();
  }
  console.log(JSON.stringify({list: el('fc-list').innerHTML, tasks: el('fc-tasks').innerHTML, calls,
    dlg: el('fc-dlg-body').innerHTML, err: el('fc-dlg-err').textContent, toast: el('fc-toast').textContent}));
})();
"""


def _console(tmp_path, step):
    node = __import__("shutil").which("node")
    if not node:
        pytest.skip("node not installed")
    h = tmp_path / "hflow.js"
    h.write_text(_HARNESS, encoding="utf-8")
    out = __import__("subprocess").run([node, str(h), str(_CONSOLE), json.dumps(step)], capture_output=True, text=True,
                                       timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _cnode(nid, **kw):
    row = {"node_id": nid, "status": "active", "state": "online", "label": nid.upper(), "last_seen": 1.9e9,
           "phones": [{"serial": "S1", "state": "device", "model": "Pixel"}]}
    row.update(kw)
    return row


def test_console_social_button_and_post(tmp_path):
    nodes = [
        _cnode("n1", caps=["phone_ops_v1", "phone_flows_v1"], remote_ops_enabled=True),
        _cnode("n2", caps=["phone_flows_v1"], remote_ops_enabled=False),
        _cnode("n3", caps=["phone_ops_v1"], remote_ops_enabled=True),
    ]
    tasks = [{"task_id": "t1", "node_id": "n1", "kind": "phone_post", "status": "rejected",
              "detail": "node_lacks_cap:phone_flows_v1", "created_at": 1.9e9, "result": {}}]
    res = _console(tmp_path, {"nodes": nodes, "tasks": tasks, "social": {
        "node": "n1", "media": True,
        "fields": {"fc-soc-serial": "S1", "fc-soc-app": "tiktok", "fc-soc-flow": "post",
                   "fc-soc-text": "hello fleet", "fc-soc-slot": "2"},
    }})
    assert res["list"].count("社交动作") == 1
    assert "发帖" in res["tasks"] and "0.3.8" in res["tasks"]
    assert "phone_ui_map.json" in res["dlg"] and "受保护的直播机除外" in res["dlg"]
    posts = [c for c in res["calls"] if c[0] == "POST"]
    assert posts == [["POST", "/api/fleet/nodes/n1/phones/S1/social/post",
                      {"app": "tiktok", "text": "hello fleet", "media": 1, "media_slot": 2}]]


# ── 冻结包缺坐标文件 ──────────────────────────────────────────────────────────
def _map_with_like(dest: Path, xy):
    src = Path(__file__).resolve().parents[1] / "src" / "fleet" / "phone_ui_map.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    data["apps"]["facebook"]["anchors"]["like_button"] = list(xy)
    dest.write_text(json.dumps(data), encoding="utf-8")
    return dest


def _like_taps(plan):
    return [step for step in plan if step.get("op") == "tap"]


def test_missing_bundle_uses_state_dir_map(tmp_path, monkeypatch):
    from src.fleet import phone_flows as pf

    monkeypatch.setattr(pf, "_BUNDLED", tmp_path / "no-bundle" / "phone_ui_map.json")
    state = tmp_path / "fleet"
    state.mkdir()
    _map_with_like(state / "phone_ui_map.json", (11, 22))
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True, state_dir=state, ops=ops)
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, {**_body("facebook", "like"), "dry_run": True}, {"serial": "S1"})
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert fake.calls == []
    assert {"op": "tap", "x": 11, "y": 22} in _like_taps(result["plan"])
    assert pf.default_ui_map_path(state) == state / "phone_ui_map.json"


def test_present_bundle_wins_over_state_dir(tmp_path):
    state = tmp_path / "fleet"
    state.mkdir()
    _map_with_like(state / "phone_ui_map.json", (11, 22))
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True, state_dir=state, ops=ops)
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, {**_body("facebook", "like"), "dry_run": True}, {"serial": "S1"})
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert fake.calls == []
    taps = _like_taps(result["plan"])
    assert {"op": "tap", "x": 900, "y": 560} in taps
    assert {"op": "tap", "x": 11, "y": 22} not in taps


def test_explicit_ui_map_path_does_not_fall_through(tmp_path, monkeypatch):
    from src.fleet import phone_flows as pf

    state = tmp_path / "fleet"
    state.mkdir()
    _map_with_like(state / "phone_ui_map.json", (11, 22))
    monkeypatch.setattr(pf, "_BUNDLED", tmp_path / "no-bundle" / "phone_ui_map.json")
    ops, fake, _slept = _ops()
    missing = tmp_path / "custom" / "missing.json"
    flows = PhoneFlows(enabled=True, ui_map_path=str(missing), state_dir=state, ops=ops)
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, {**_body("facebook", "like"), "dry_run": True}, {"serial": "S1"})
    assert (status, detail) == (STATUS_REJECTED, "ui_map_missing")
    assert fake.calls == []
    tuned = tmp_path / "custom" / "phone_ui_map.json"
    tuned.parent.mkdir()
    _map_with_like(tuned, (33, 44))
    flows.configure(enabled=True, ui_map_path=str(tuned))
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, {**_body("facebook", "like"), "dry_run": True}, {"serial": "S1"})
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert {"op": "tap", "x": 33, "y": 44} in _like_taps(result["plan"])


def test_missing_bundle_and_state_dir_is_ui_map_missing(tmp_path, monkeypatch):
    from src.fleet import phone_flows as pf

    monkeypatch.setattr(pf, "_BUNDLED", tmp_path / "no-bundle" / "phone_ui_map.json")
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True, state_dir=tmp_path / "empty", ops=ops)
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, {**_body("facebook", "like"), "dry_run": True}, {"serial": "S1"})
    assert (status, detail) == (STATUS_REJECTED, "ui_map_missing")
    assert fake.calls == []


def test_agent_phone_flows_keeps_state_dir(tmp_path, monkeypatch):
    from src.fleet.agent import AgentConfig, NodeAgent

    monkeypatch.setenv("CHATX_FLEET_STATE_DIR", str(tmp_path / "state"))
    cfg = AgentConfig(tmp_path / "state")
    cfg.data.update({"controller_url": "http://127.0.0.1:1", "instances": []})
    agent = NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    assert agent.phone_flows.state_dir == cfg.state_dir


def test_build_agent_add_data_sits_beside_frozen_phone_flows():
    import importlib.util
    import os

    path = Path(__file__).resolve().parents[1] / "fleet_agent" / "build_agent.py"
    spec = importlib.util.spec_from_file_location("chatx_build_agent_ui_map", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.UI_MAP_DEST == "src/fleet"
    src_s, dest = mod.ui_map_add_data().rsplit(os.pathsep, 1)
    assert dest == "src/fleet"
    assert Path(src_s).is_file() and Path(src_s).name == "phone_ui_map.json"
    text = path.read_text(encoding="utf-8")
    assert "--add-data" in text and "ui_map_add_data()" in text
    assert "os.pathsep" in text


def test_installers_copy_ui_map_only_when_absent():
    root = Path(__file__).resolve().parents[1]
    boot = (root / "fleet_agent/setup/bootstrap.ps1").read_text(encoding="utf-8")
    ps1 = (root / "fleet_agent/Install-ChatXAgent.ps1").read_text(encoding="utf-8")
    iss = (root / "fleet_agent/setup/ChatXAgent.iss").read_text(encoding="utf-8")
    assert boot.isascii()
    for src in (boot, ps1):
        body = src.split("function Copy-DefaultUiMap", 1)[1].split("\nfunction ", 1)[0]
        assert body.index("Test-Path -LiteralPath $dest") < body.index("Copy-Item")
    assert boot.index("exit 3") < boot.index("Copy-DefaultUiMap $InstallDir $StateDir")
    assert ps1.index("Fail $lockError") < ps1.index("Copy-DefaultUiMap $stateDir")
    assert r"..\..\src\fleet\phone_ui_map.json" in iss
    assert 'DestName: "phone_ui_map.json"' in iss and 'DestDir: "{app}"' in iss
    step = iss.split("procedure CurStepChanged", 1)[1]
    assert step.index("LockStateDir(dir)") < step.index("if not FileExists(dir + '\\phone_ui_map.json')")


# ── 源码栏 ────────────────────────────────────────────────────────────────────
def test_new_modules_have_no_dangerous_adb_tokens():
    root = Path(__file__).resolve().parents[1] / "src" / "fleet"
    for name in ("phone_flows.py", "phone_flow_rules.py", "phone_flow_robust.py", "phone_ui_map.json"):
        text = (root / name).read_text(encoding="utf-8")
        for bad in ("kill-server", "start-server", "tcpip", "reboot", "9000", "connect", "install", "\"usb\"", "root"):
            assert bad not in text, (name, bad)
