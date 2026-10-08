"""社交动作的核对、重试、登录预检和步骤间抖动。mock adb，不碰真机。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fleet.agent import AGENT_VERSION
from src.fleet.phone_flow_robust import MAX_JITTER_MS, jitter_seconds, parse_jitter_ms
from src.fleet.phone_flows import PhoneFlows, bundled_ui_map, compile_flow, compile_flow_checked, validate_ui_map
from src.fleet.phone_ops import MAX_CONCURRENT, MIN_INTERVAL_SEC
from src.fleet.phone_rules import PhoneOpError
from src.fleet.protocol import (
    CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2, CAP_PHONE_OPS_V1, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_PHONE_SWIPE, TASK_PHONE_TAP,
)
from tests.test_fleet_phone_flows import (
    W, H, FakeAdb, _assert_actions, _body, _frame, _ops,
)

_MAP = Path(__file__).resolve().parents[1] / "src" / "fleet" / "phone_ui_map.json"
_LOGGED = (24, 119, 242)
_WALL = (66, 103, 178)


def _px(anchor, app="facebook"):
    ui = bundled_ui_map()
    ax, ay = ui["apps"][app]["anchors"][anchor]
    return ax * W // 1000, ay * H // 1000


def _paint(n, marks):
    raw = bytearray(_frame())
    raw[12] = n & 255
    for x, y, rgb in marks:
        off = 12 + (y * W + x) * 4
        raw[off:off + 3] = bytes(rgb)
    return bytes(raw)


class IndexAdb(FakeAdb):
    def __init__(self, builder):
        super().__init__(frame=_frame())
        self.builder = builder
        self.n = 0

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 4 and args[2:] == ("exec-out", "screencap"):
            self.frame = self.builder(self.n)
            self.n += 1
        return FakeAdb.__call__(self, cmd, **kw)


def _taps(actions):
    return [a for a in actions if len(a) > 4 and a[4] == "tap"]


def _shots(actions):
    return [a for a in actions if a[2:] == ("exec-out", "screencap")]


def _write_map(tmp_path, mutate):
    raw = json.loads(_MAP.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "ui.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_bundled_robust_defaults_and_step_marks():
    ui = bundled_ui_map()
    assert ui["robust"] == {"verify": False, "retries": 2, "jitter_ms": [0, 0]}
    steps, checks = compile_flow_checked("facebook", "like", {"scrolls": 0}, W, H, ui)
    assert ui["apps"]["facebook"]["flows"]["like"][0] == {"op": "key", "key": "home"}
    assert checks[0] is None
    assert checks[1]["change"] is True and checks[1]["preflight"] is True and checks[1]["label"] == "app_icon"
    assert checks[1]["require"] == ("logged_in",) and checks[1]["forbid"] == ("login_wall",)
    assert all(c is None or c["label"] != "like_button" for c in checks)
    assert not any(k == TASK_PHONE_SWIPE for k, _ in steps)
    scrolled, sch = compile_flow_checked("facebook", "like", {"scrolls": 2}, W, H, ui)
    swipes = [(k, c) for (k, _), c in zip(scrolled, sch) if k == TASK_PHONE_SWIPE]
    assert len(swipes) == 2 and all(c["change"] is True for _, c in swipes)


def test_default_execute_matches_the_old_action_and_sleep_contract():
    ui = bundled_ui_map()
    payload = {"app": "tiktok", "scrolls": 0}
    compiled = compile_flow("tiktok", "like", payload, W, H, ui)

    def boom():
        raise AssertionError("rng called")

    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True, rng=boom)
    status, result, detail = flows.execute("phone_like", payload, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["steps"] == 1 + len(compiled)
    _assert_actions(fake.actions(), compiled)
    assert slept == [MIN_INTERVAL_SEC] * len(compiled)
    assert len(_shots(fake.actions())) == 1


def test_fixed_jitter_is_added_on_top_of_the_minimum_gap():
    ui = bundled_ui_map()
    payload = {"app": "tiktok", "scrolls": 0}
    compiled = compile_flow("tiktok", "like", payload, W, H, ui)
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True, jitter_ms=(100, 100))
    status, _res, detail = flows.execute("phone_like", payload, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    _assert_actions(fake.actions(), compiled)
    assert slept == [0.1, MIN_INTERVAL_SEC] * len(compiled)
    assert len(_shots(fake.actions())) == 1


def test_jitter_clamp_and_illformed_override():
    assert parse_jitter_ms([0, 100000]) == (0, MAX_JITTER_MS)
    assert parse_jitter_ms("fast") is None
    assert parse_jitter_ms([5, 1]) is None
    assert jitter_seconds((0, 0), lambda: (_ for _ in ()).throw(AssertionError("rng"))) == 0
    ui = bundled_ui_map()
    payload = {"app": "tiktok", "scrolls": 0}
    compiled = compile_flow("tiktok", "like", payload, W, H, ui)
    ops, _fake, slept = _ops()
    flows = PhoneFlows(enabled=True, jitter_ms=(0, 100000), rng=lambda: 1.0)
    status, _res, detail = flows.execute("phone_like", payload, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert slept[0] == 2.0 and 100 not in slept
    assert slept == [2.0, MIN_INTERVAL_SEC] * len(compiled)
    assert MIN_INTERVAL_SEC == 0.5 and MAX_CONCURRENT == 2


def test_verify_identical_frames_stop_at_the_app_icon():
    icon = tuple(str(v) for v in _px("app_icon", "tiktok"))
    home = tuple(str(v) for v in _px("home_tab", "tiktok"))
    ops, fake, _ = _ops()
    flows = PhoneFlows(enabled=True, verify=True)
    status, result, detail = flows.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "screen_not_reached")
    assert result["failed_step"] == 1 and result["completed_steps"] == result["failed_step"] + 1
    assert result["stderr"] == "anchor:app_icon"
    taps = _taps(fake.actions())
    assert len(taps) == 3 and all((a[-2], a[-1]) == icon for a in taps)
    assert all((a[-2], a[-1]) != home for a in taps)
    assert len(_shots(fake.actions())) == 4


def test_unchanged_screen_wins_over_the_login_wall():
    wall = _paint(7, [(*_px("login_mark"), _WALL)])
    ops, _fake, _ = _ops(adb=IndexAdb(lambda _i: wall))
    flows = PhoneFlows(enabled=True, verify=True)
    status, result, detail = flows.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "screen_not_reached")
    assert result["stderr"] == "anchor:app_icon"


def test_verify_success_when_the_screen_changes_and_the_app_is_signed_in():
    mark = (*_px("home_tab", "tiktok"), _LOGGED)

    def builder(i):
        return _paint(50 + i, [mark])

    ui = bundled_ui_map()
    payload = {"app": "tiktok", "scrolls": 0}
    compiled = compile_flow("tiktok", "like", payload, W, H, ui)
    _steps, checks = compile_flow_checked("tiktok", "like", payload, W, H, ui)
    ops, fake, _ = _ops(adb=IndexAdb(builder))
    flows = PhoneFlows(enabled=True, verify=True)
    status, result, detail = flows.execute("phone_like", payload, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok"), detail
    assert result["steps"] == 1 + len(compiled)
    assert len(_shots(fake.actions())) == 1 + sum(1 for c in checks if c)


def test_not_logged_in_stops_before_later_taps():
    wall = (*_px("login_mark"), _WALL)
    feed = tuple(str(v) for v in _px("feed_tab"))
    ops, fake, _ = _ops(adb=IndexAdb(lambda i: _paint(30 + i, [wall])))
    flows = PhoneFlows(enabled=True, verify=True)
    status, result, detail = flows.execute("phone_post", _body("facebook", "post"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "not_logged_in")
    assert result["failed_step"] == 1 and result["completed_steps"] == 2
    assert result["stderr"] == "probe:login_wall"
    assert all((a[-2], a[-1]) != feed for a in _taps(fake.actions()))
    assert not any(a[4:5] == ("text",) for a in fake.actions())


def test_app_not_ready_when_neither_probe_matches():
    ops, fake, _ = _ops(adb=IndexAdb(lambda i: _paint(40 + i, [])))
    flows = PhoneFlows(enabled=True, verify=True)
    status, result, detail = flows.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "app_not_ready")
    assert result["stderr"] == "probe:logged_in"
    assert result["failed_step"] == 1
    assert len(_taps(fake.actions())) == 3


def test_anchor_mismatch_after_preflight_passes(tmp_path):
    def mutate(raw):
        raw["robust"]["verify"] = True
        raw["robust"]["retries"] = 0
        raw["apps"]["tiktok"]["flows"]["like"][2]["expect"] = {"change": True, "probe": "logged_in"}

    path = _write_map(tmp_path, mutate)
    mark = (*_px("home_tab", "tiktok"), _LOGGED)
    like = tuple(str(v) for v in _px("like_button", "tiktok"))

    def builder(i):
        if i <= 1:
            return _paint(10 + i, [mark])
        return _paint(90 + i, [])

    ops, fake, _ = _ops(adb=IndexAdb(builder))
    flows = PhoneFlows(enabled=True, verify=True, ui_map_path=str(path))
    status, result, detail = flows.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "anchor_mismatch")
    assert result["failed_step"] == 2 and result["completed_steps"] == 3
    assert result["stderr"] == "probe:logged_in"
    assert all((a[-2], a[-1]) != like for a in _taps(fake.actions()))


def test_retry_then_success_and_retry_budget(tmp_path):
    mark = (*_px("home_tab", "tiktok"), _LOGGED)

    def builder(i):
        if i < 3:
            return _frame()
        return _paint(80 + i, [mark])

    ops, fake, _ = _ops(adb=IndexAdb(builder))
    flows = PhoneFlows(enabled=True, verify=True)
    status, _res, detail = flows.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok"), detail
    icon = tuple(str(v) for v in _px("app_icon", "tiktok"))
    assert sum(1 for a in _taps(fake.actions()) if (a[-2], a[-1]) == icon) == 3

    def one(raw):
        raw["robust"]["retries"] = 1

    path = _write_map(tmp_path, one)
    ops2, fake2, _ = _ops()
    flows2 = PhoneFlows(enabled=True, verify=True, ui_map_path=str(path))
    status, result, detail = flows2.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops2)
    assert (status, detail) == (STATUS_FAILED, "screen_not_reached")
    assert len(_taps(fake2.actions())) == 2
    assert result["failed_step"] == 1


@pytest.mark.parametrize("serial", ["3B1F4KE5MS140P4X", "192.168.0.148:5555"])
def test_protected_phone_with_verify_never_calls_adb(serial):
    ops, fake, _ = _ops()
    flows = PhoneFlows(enabled=True, verify=True, jitter_ms=(100, 400))
    status, _res, detail = flows.execute("phone_post", _body("facebook", "post"), {"serial": serial}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    assert fake.calls == []


def test_jitter_stays_inside_the_phone_lock(monkeypatch):
    import src.fleet.phone_ops as po

    monkeypatch.setattr(po, "LOCK_WAIT_SEC", 0.2)
    ops, _fake, _ = _ops()
    flows = PhoneFlows(enabled=True, jitter_ms=(50, 50))
    seen = {}
    orig = ops.add_human_gap

    def gap(serial, seconds):
        if "busy" not in seen:
            seen["busy"] = ops.execute(TASK_PHONE_TAP, {"x": 2, "y": 2}, {"serial": "S1"})[2]
        orig(serial, seconds)

    ops.add_human_gap = gap
    status, _res, detail = flows.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert seen["busy"] == "phone_busy"


def test_agent_knobs_and_map_override(tmp_path):
    from src.fleet.agent import _phone_flows_settings

    assert _phone_flows_settings({})["verify"] is None
    assert _phone_flows_settings({})["jitter_ms"] is None
    assert _phone_flows_settings({"phone_flow_verify": True})["verify"] is True
    assert _phone_flows_settings({"phone_flow_verify": "yes"})["verify"] is False
    assert _phone_flows_settings({"phone_flow_verify": "true"})["verify"] is False
    assert _phone_flows_settings({"phone_flow_jitter_ms": [100, 100]})["jitter_ms"] == (100, 100)
    assert _phone_flows_settings({"phone_flow_jitter_ms": "fast"})["jitter_ms"] is None

    def turn_on(raw):
        raw["robust"]["verify"] = True

    path = _write_map(tmp_path, turn_on)
    ops, fake, _slept = _ops()
    off = PhoneFlows(enabled=True, verify=False, ui_map_path=str(path))
    status, _res, detail = off.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert len(_shots(fake.actions())) == 1
    follow_ops, _follow_fake, _ = _ops()
    follow = PhoneFlows(enabled=True, ui_map_path=str(path))
    status, result, detail = follow.execute("phone_like", _body("tiktok", "like"), {"serial": "S1"}, ops=follow_ops)
    assert (status, detail) == (STATUS_FAILED, "screen_not_reached")
    assert result["stderr"] == "anchor:app_icon"
    settings = _phone_flows_settings({"phone_flows_enabled": True, "phone_flow_jitter_ms": "fast"})
    quiet, _fake, slept = _ops()
    flows = PhoneFlows(enabled=True, **{k: settings[k] for k in ("verify", "jitter_ms")})
    payload = {"app": "tiktok", "scrolls": 0}
    compiled = compile_flow("tiktok", "like", payload, W, H, bundled_ui_map())
    assert flows.execute("phone_like", payload, {"serial": "S1"}, ops=quiet)[0] == STATUS_DONE
    assert slept == [MIN_INTERVAL_SEC] * len(compiled)


def test_caps_coexist_on_one_agent(tmp_path, monkeypatch):
    from src.fleet import adb_bundle
    from src.fleet import agent as agent_mod
    from src.fleet import phones as ph

    monkeypatch.setattr(adb_bundle, "bundled_adb_candidates", lambda: ())

    def boom(*_a, **_k):
        raise AssertionError("adb process spawned")

    monkeypatch.setattr(ph.subprocess, "run", boom)
    monkeypatch.setattr(adb_bundle.subprocess, "run", boom)
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data.update({
        "controller_url": "http://127.0.0.1:1", "instances": [],
        "adb_manage_server": True, "phone_flows_enabled": True,
        "phone_flow_verify": True,
    })
    ag = agent_mod.NodeAgent(cfg, http=lambda *_a, **_k: (200, {}), app_version="t")
    hb = ag.build_heartbeat()
    assert AGENT_VERSION == "0.3.22"
    assert hb["caps"] == [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2]
    assert ag.phones.manage_server is True and ag.phone_ops.manage_server is True
    assert ag.phone_flows.enabled is True and ag.phone_flows._verify is True


def test_enable_phone_adb_keeps_flow_verify(tmp_path, monkeypatch):
    from src.fleet import agent as agent_mod

    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    fleet = tmp_path / "fleet"
    assert agent_mod.main(["--state-dir", str(fleet), "enable-phone-adb"]) == 0
    data = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    data["phone_flow_verify"] = True
    data["phone_flow_jitter_ms"] = [20, 80]
    data["phones_exclude"] = ["ABC123"]
    (fleet / "agent.json").write_text(json.dumps(data), encoding="utf-8")
    assert agent_mod.main(["--state-dir", str(fleet), "enable-phone-adb"]) == 0
    again = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert again["adb_manage_server"] is True
    assert again["phone_flow_verify"] is True
    assert again["phone_flow_jitter_ms"] == [20, 80]
    assert again["phones_exclude"] == ["ABC123"]


def test_invalid_expect_probe_and_jitter_reject_the_map():
    raw = json.loads(_MAP.read_text(encoding="utf-8"))
    bad_jitter = json.loads(json.dumps(raw))
    bad_jitter["robust"]["jitter_ms"] = [0, 99999]
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(bad_jitter)
    assert ei.value.code == "ui_map_invalid"
    bad_expect = json.loads(json.dumps(raw))
    bad_expect["apps"]["facebook"]["flows"]["like"][1]["expect"] = "nope"
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(bad_expect)
    assert ei.value.code == "ui_map_invalid"
    empty = json.loads(json.dumps(raw))
    empty["apps"]["facebook"]["flows"]["like"][1]["expect"] = {"change": False}
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(empty)
    assert ei.value.code == "ui_map_invalid"
    missing = json.loads(json.dumps(raw))
    missing["apps"]["facebook"]["probes"]["logged_in"]["at"] = "missing_anchor"
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(missing)
    assert ei.value.code == "ui_map_invalid"
    order = json.loads(json.dumps(raw))
    order["robust"]["jitter_ms"] = [8, 1]
    with pytest.raises(PhoneOpError) as ei:
        validate_ui_map(order)
    assert ei.value.code == "ui_map_invalid"
