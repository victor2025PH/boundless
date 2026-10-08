"""Facebook post: caption before publish, and fail closed when confirmation is requested.

Mock adb only. Does not exercise like / comment / follow.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.fleet.phone_flows import PhoneFlows, bundled_ui_map, compile_flow
from src.fleet.phone_rules import PhoneOpError, sanitize_phone_result
from src.fleet.post_publish import (
    MIN_SEPARATION_PERMILLE, anchors_separated, assess_publish, distinct_next_point, points_collide,
)
from src.fleet.protocol import STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, TASK_PHONE_POST, TASK_PHONE_TAP, TASK_PHONE_TEXT
from tests.test_fleet_phone_flows import W, H, FakeAdb, _body, _ops

_SRC = Path(__file__).resolve().parents[1] / "src" / "fleet" / "post_publish.py"


def _node(text="", desc="", clickable="true", klass="android.widget.Button", bounds="[100,200][140,240]"):
    return (
        f'<node text="{text}" content-desc="{desc}" class="{klass}" '
        f'clickable="{clickable}" bounds="{bounds}" />'
    )


def _xml(*nodes):
    return '<hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>"


class XmlAdb(FakeAdb):
    def __init__(self, xml="", xmls=None, **kw):
        super().__init__(**kw)
        self._xmls = list(xmls) if xmls is not None else [xml]

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 6 and args[2:6] == ("shell", "uiautomator", "dump", "/dev/tty"):
            self.calls.append(args)
            body = self._xmls.pop(0) if self._xmls else ""
            if not isinstance(body, str):
                body = ""
            return SimpleNamespace(returncode=0, stdout=body.encode(), stderr=b"")
        return super().__call__(cmd, **kw)


def _taps(actions):
    return [args for args in actions if len(args) > 4 and args[4] == "tap"]


def _text_index(actions):
    return next(i for i, args in enumerate(actions) if len(args) > 4 and args[4] == "text")


def _submit_px():
    ui = bundled_ui_map()
    steps = compile_flow("facebook", "post", {"text": "hello fleet"}, W, H, ui)
    kind, payload = steps[-1]
    assert kind == TASK_PHONE_TAP
    return payload["x"], payload["y"]


def _no_like_transport(actions):
    blob = " ".join(" ".join(args) for args in actions)
    assert "chatx_like_hierarchy" not in blob
    assert "media_session" not in blob
    assert not any(args[2:6] == ("shell", "input", "keyevent", "4") for args in actions)
    assert not any(args[2:6] == ("shell", "input", "keyevent", "127") for args in actions)


def test_module_does_not_touch_like_locator():
    text = _SRC.read_text(encoding="utf-8")
    assert "like_locate" not in text
    assert "read_ui_hierarchy" not in text


def test_bundled_facebook_anchors_are_separated_and_caption_is_first():
    ui = bundled_ui_map()
    fb = ui["apps"]["facebook"]
    assert anchors_separated(fb["anchors"]["media_next"], fb["anchors"]["post_submit"])
    assert not anchors_separated((860, 120), (880, 120), MIN_SEPARATION_PERMILLE)
    media = fb["flows"]["post_media"]
    assert all(step.get("anchor") != "media_next" for step in media)
    text_i = next(i for i, step in enumerate(media) if step.get("op") == "text")
    submit_i = next(i for i, step in enumerate(media) if step.get("anchor") == "post_submit")
    assert text_i < submit_i
    assert any(step.get("anchor") == "media_next" for step in ui["apps"]["instagram"]["flows"]["post_media"])
    assert any(step.get("anchor") == "media_next" for step in ui["apps"]["tiktok"]["flows"]["post_media"])


def test_compile_keeps_caption_before_submit_and_hides_anchor_names():
    ui = bundled_ui_map()
    steps = compile_flow("facebook", "post_media", {"text": "hello fleet", "media": 1, "media_slot": 2}, W, H, ui)
    plain = compile_flow("facebook", "post", {"text": "hello fleet"}, W, H, ui)
    assert len(steps) > len(plain)
    text_i = next(i for i, (kind, _p) in enumerate(steps) if kind == TASK_PHONE_TEXT)
    submit_i = max(i for i, (kind, _p) in enumerate(steps) if kind == TASK_PHONE_TAP)
    assert text_i < submit_i
    sx, sy = steps[submit_i][1]["x"], steps[submit_i][1]["y"]
    for kind, payload in steps[:text_i]:
        if kind == TASK_PHONE_TAP:
            assert not points_collide(payload["x"], payload["y"], sx, sy, W, H)
        assert "_anchor" not in payload
    slot0 = compile_flow("facebook", "post_media", {"text": "hello fleet", "media_slot": 0}, W, H, ui)
    slot2 = compile_flow("facebook", "post_media", {"text": "hello fleet", "media_slot": 2}, W, H, ui)
    taps0 = [(p["x"], p["y"]) for k, p in slot0 if k == TASK_PHONE_TAP]
    taps2 = [(p["x"], p["y"]) for k, p in slot2 if k == TASK_PHONE_TAP]
    assert taps0 != taps2


def test_compile_drops_a_colliding_next_even_when_the_map_still_has_one():
    ui = {"apps": {"facebook": {
        "anchors": {
            "media_next": (860, 120),
            "post_submit": (880, 120),
            "composer_field": (500, 400),
        },
        "flows": {"post_media": [
            {"op": "tap", "anchor": "media_next"},
            {"op": "tap", "anchor": "post_submit"},
            {"op": "tap", "anchor": "composer_field"},
            {"op": "text", "field": "text"},
        ]},
    }}}
    steps = compile_flow("facebook", "post_media", {"text": "hello fleet"}, 1000, 1000, ui)
    kinds = [kind for kind, _p in steps]
    assert kinds == [TASK_PHONE_TAP, TASK_PHONE_TEXT, TASK_PHONE_TAP]
    assert steps[0][1]["y"] == 400
    sx, sy = steps[-1][1]["x"], steps[-1][1]["y"]
    assert not points_collide(steps[0][1]["x"], steps[0][1]["y"], sx, sy, 1000, 1000)
    assert all("_anchor" not in payload for _k, payload in steps)


def test_distinct_next_requires_an_unambiguous_label():
    near = _xml(_node(text="Next", bounds="[140,30][176,70]"))
    assert distinct_next_point(near) == (158, 50)
    assert distinct_next_point(_xml(_node(desc="Continue", bounds="[10,10][30,30]"))) == (20, 20)
    for label in ("Post", "Share", "Publish", "分享"):
        assert distinct_next_point(_xml(_node(text=label))) is None
    mixed = _xml(_node(text="Next", desc="Post"))
    assert distinct_next_point(mixed) is None
    assert distinct_next_point("") is None
    assert distinct_next_point("UI hierchary dumped to: /dev/tty") is None
    overlap = _xml(
        _node(text="Next", bounds="[800,40][980,160]"),
        _node(text="Post", bounds="[800,40][980,160]"),
    )
    assert distinct_next_point(overlap) is None
    inert = _xml(_node(text="Next", clickable="false", klass="android.widget.TextView"))
    assert distinct_next_point(inert) is None
    parent = (
        '<hierarchy><node clickable="true" bounds="[10,10][80,80]" class="android.view.ViewGroup">'
        + _node(text="下一步", clickable="false", klass="android.widget.TextView", bounds="[10,10][80,80]")
        + "</node></hierarchy>"
    )
    assert distinct_next_point(parent) == (45, 45)
    right = _xml(
        _node(text="Next", bounds="[10,10][40,40]"),
        _node(text="Next", bounds="[200,10][240,40]"),
    )
    assert distinct_next_point(right) == (220, 25)
    prefixed = "UI hierchary dumped to: /dev/tty\n" + near
    assert distinct_next_point(prefixed) == (158, 50)


def test_assess_publish_fails_closed_without_a_clear_success():
    toast = _xml(_node(
        text="Post shared", klass="android.widget.TextView", clickable="false", bounds="[40,700][400,760]",
    ))
    assert assess_publish(toast) == "confirmed"
    still = _xml(
        _node(text="Post shared", klass="android.widget.TextView", clickable="false", bounds="[40,700][400,760]"),
        _node(text="Post", bounds="[800,40][980,160]"),
    )
    assert assess_publish(still) == "unproven"
    assert assess_publish("") == "unproven"
    assert assess_publish(_xml(_node(text="Home"))) == "unproven"
    assert assess_publish("", ["Post shared"]) == "unproven"


def test_live_media_post_taps_next_only_on_a_distinct_signal():
    ui = bundled_ui_map()
    compiled = compile_flow(
        "facebook", "post_media", {"text": "hello fleet", "media": 1, "media_slot": 0}, W, H, ui)
    center = (120, 220)
    xml = _xml(_node(text="Next", bounds="[100,200][140,240]"))
    ops, fake, _slept = _ops(XmlAdb(xml=xml, frame=None))
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", media=1), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok"), detail
    actions = fake.actions()
    text_at = _text_index(actions)
    taps_before = [args[-2:] for args in actions[:text_at] if len(args) > 4 and args[4] == "tap"]
    assert (str(center[0]), str(center[1])) in taps_before
    blind = (str(860 * W // 1000), str(120 * H // 1000))
    assert blind not in taps_before
    sx, sy = _submit_px()
    assert all(not points_collide(int(x), int(y), sx, sy, W, H) for x, y in taps_before)
    assert actions[text_at][2:4] == ("shell", "input")
    submit_at = max(i for i, args in enumerate(actions) if len(args) > 5 and args[-2:] == (str(sx), str(sy)))
    assert text_at < submit_at
    _no_like_transport(actions)
    assert result.get("publish_verified") is not True
    assert [p["text"] for k, p in compiled if k == TASK_PHONE_TEXT] == ["hello fleet"]


def test_live_media_post_skips_next_when_the_control_is_publish_or_empty():
    for xml in ("", _xml(_node(text="Post", bounds="[760,40][980,160]")), _xml(_node(text="Share"))):
        ops, fake, _slept = _ops(XmlAdb(xml=xml))
        flows = PhoneFlows(enabled=True)
        status, _res, detail = flows.execute(
            TASK_PHONE_POST, _body("facebook", "post", media=1), {"serial": "S1"}, ops=ops)
        assert (status, detail) == (STATUS_DONE, "ok"), (xml, detail)
        actions = fake.actions()
        text_at = _text_index(actions)
        dumps = [i for i, args in enumerate(actions) if args[2:6] == ("shell", "uiautomator", "dump", "/dev/tty")]
        assert len(dumps) == 1 and dumps[0] < text_at
        sx, sy = _submit_px()
        taps_before = [
            (int(args[-2]), int(args[-1])) for args in actions[:text_at] if len(args) > 4 and args[4] == "tap"
        ]
        assert all(not points_collide(x, y, sx, sy, W, H) for x, y in taps_before)
        _no_like_transport(actions)


def test_ocr_publish_label_cancels_a_next_tap_and_does_not_invent_one():
    xml = _xml(_node(text="Next", bounds="[100,200][140,240]"))
    ops, fake, _slept = _ops(XmlAdb(xml=xml))
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [{"text": "Post"}])
    status, _res, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", media=1), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    text_at = _text_index(fake.actions())
    assert ("120", "220") not in [args[-2:] for args in fake.actions()[:text_at] if len(args) > 4 and args[4] == "tap"]

    ops2, fake2, _slept2 = _ops(XmlAdb(xml=""))
    flows2 = PhoneFlows(enabled=True, ocr=lambda _raw: ["Next"])
    status, _res, detail = flows2.execute(
        TASK_PHONE_POST, _body("facebook", "post", media=1), {"serial": "S1"}, ops=ops2)
    assert (status, detail) == (STATUS_DONE, "ok")
    text_at = _text_index(fake2.actions())
    before = [args[-2:] for args in fake2.actions()[:text_at] if len(args) > 4 and args[4] == "tap"]
    ui = bundled_ui_map()
    compiled = compile_flow("facebook", "post_media", {"text": "hello fleet", "media": 1, "media_slot": 0}, W, H, ui)
    planned_before = []
    for kind, payload in compiled:
        if kind == TASK_PHONE_TEXT:
            break
        if kind == TASK_PHONE_TAP:
            planned_before.append((str(payload["x"]), str(payload["y"])))
    assert before == planned_before


def test_text_post_does_not_dump_unless_verify_publish_is_set():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert not any(args[2:5] == ("shell", "uiautomator", "dump") for args in fake.actions())
    assert result.get("publish_verified") is not True


def test_verify_publish_fails_closed_after_the_submit_tap():
    ops, fake, _slept = _ops(XmlAdb(xml=""))
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", verify_publish=True), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "post_not_confirmed")
    assert result.get("stderr") == "publish:unproven"
    assert result.get("publish_verified") is not True
    actions = fake.actions()
    text_at = _text_index(actions)
    sx, sy = _submit_px()
    submit_at = max(i for i, args in enumerate(actions) if len(args) > 5 and args[-2:] == (str(sx), str(sy)))
    dumps = [i for i, args in enumerate(actions) if args[2:6] == ("shell", "uiautomator", "dump", "/dev/tty")]
    assert text_at < submit_at < dumps[0]
    assert len(dumps) == 1
    _no_like_transport(actions)

    toast = _xml(_node(
        text="Your post is now live", klass="android.widget.TextView", clickable="false", bounds="[20,600][400,680]",
    ))
    ops2, fake2, _slept2 = _ops(XmlAdb(xml=toast))
    flows2 = PhoneFlows(enabled=True)
    status, result, detail = flows2.execute(
        TASK_PHONE_POST, _body("facebook", "post", verify_publish=True), {"serial": "S1"}, ops=ops2)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result.get("publish_verified") is True
    clean = sanitize_phone_result(TASK_PHONE_POST, result)
    assert clean.get("publish_verified") is True

    still = _xml(
        _node(text="Post shared", klass="android.widget.TextView", clickable="false", bounds="[20,600][400,680]"),
        _node(text="POST", bounds="[800,20][980,120]"),
    )
    ops3, _fake3, _slept3 = _ops(XmlAdb(xml=still))
    flows3 = PhoneFlows(enabled=True)
    status, result, detail = flows3.execute(
        TASK_PHONE_POST, _body("facebook", "post", verify_publish=True), {"serial": "S1"}, ops=ops3)
    assert (status, detail) == (STATUS_FAILED, "post_not_confirmed")
    assert result.get("publish_verified") is not True


def test_dry_run_gates_media_next_and_does_not_claim_publication():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", media=1, dry_run=True, verify_publish=True),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert fake.calls == []
    assert result["media_next"] == "gated"
    assert result.get("publish_verified") is not True
    plan = result["plan"]
    text_i = next(i for i, step in enumerate(plan) if step["op"] == "text")
    submit_i = max(i for i, step in enumerate(plan) if step["op"] == "tap")
    assert text_i < submit_i
    sx, sy = plan[submit_i]["x"], plan[submit_i]["y"]
    for step in plan[:text_i]:
        if step["op"] == "tap":
            assert not points_collide(step["x"], step["y"], sx, sy, 1000, 1000)
    clean = sanitize_phone_result(TASK_PHONE_POST, result)
    assert clean["media_next"] == "gated"
    assert "publish_verified" not in clean
    assert "hello fleet" not in json.dumps({k: v for k, v in result.items() if k != "plan"})
    dirty = dict(result, media_next="yes", publish_verified="yes")
    scrubbed = sanitize_phone_result(TASK_PHONE_POST, dirty)
    assert "media_next" not in scrubbed and "publish_verified" not in scrubbed

    status, result, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", dry_run=True), {"serial": "S1"}, ops=ops)
    assert detail == "dry_run" and "media_next" not in result and fake.calls == []


def test_bad_verify_flag_and_non_ascii_caption_never_touch_adb():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, _res, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", verify_publish="yes"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "bad_verify_publish")
    status, _res, detail = flows.execute(
        TASK_PHONE_POST, _body("facebook", "post", text="你好"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "text_non_ascii_unsupported")
    assert fake.calls == []
    try:
        from src.fleet.phone_flow_rules import validate_flow_payload
        validate_flow_payload(TASK_PHONE_POST, {"app": "facebook", "text": "hi", "verify_publish": 1})
    except PhoneOpError as exc:
        assert exc.code == "bad_verify_publish"
    else:
        raise AssertionError("non-bool verify_publish was accepted")


def test_protected_phone_is_rejected_before_adb():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    for serial in ("3B1F4KE5MS140P4X", "192.168.0.148:5555"):
        status, _res, detail = flows.execute(
            TASK_PHONE_POST, _body("facebook", "post", media=1, verify_publish=True),
            {"serial": serial}, ops=ops)
        assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    assert fake.calls == []
