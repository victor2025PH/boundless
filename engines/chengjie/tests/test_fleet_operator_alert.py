"""Local room-PC operator alert (agent 0.3.15, remote enable in 0.3.16).

The desktop window cannot be opened on this runner. These tests cover the
snapshot the window reads: wallpaper numbers, zh/en copy, and the headless
path that must not raise.
"""
from __future__ import annotations

import json
import re

from src.fleet import agent as agent_mod
from src.fleet import operator_alert as oa
from src.fleet.phones import PROTECTED_SERIALS
from src.fleet.protocol import (
    LEGACY_ALLOWED_KINDS, REMOTE_PHONE_KINDS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_KINDS, TASK_OPERATOR_ALERT_DIAG, TASK_PUSH_CONFIG,
)

SERIAL = "ZZPHONE99"
OTHER = "OTHERPHONE1"
PROTECTED = PROTECTED_SERIALS[0]
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _cfg(**extra):
    data = {"operator_alert_enabled": True, "wallpaper_map": {SERIAL: "07"}}
    data.update(extra)
    return data


def _phone(serial=SERIAL, state="offline"):
    return {"serial": serial, "state": state, "model": "M", "transport": "usb"}


def _alert(tmp, **kw):
    calls = []

    def present(state, payload):
        calls.append(payload)
        return {"ok": True, "mode": "headless", "shown": False}

    alert = oa.OperatorAlert(tmp, clock=kw.pop("clock", lambda: 1000.0), present=present, **kw)
    return alert, calls


def _blob(payload):
    return json.dumps(payload, ensure_ascii=False)


def _texts(obj, acc=None):
    if acc is None:
        acc = []
    if isinstance(obj, str):
        acc.append(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            acc.append(str(key))
            _texts(value, acc)
    elif isinstance(obj, list):
        for value in obj:
            _texts(value, acc)
    return acc


def test_string_packs_cover_zh_and_en():
    assert set(oa.STRINGS) == {"zh", "en"}
    assert set(oa.STRINGS["zh"]) == set(oa.STRINGS["en"])
    for key in oa.STRINGS["zh"]:
        assert oa.STRINGS["zh"][key].strip()
        assert oa.STRINGS["en"][key].strip()
        assert _PLACEHOLDER.findall(oa.STRINGS["zh"][key]) == _PLACEHOLDER.findall(oa.STRINGS["en"][key])
    for reason in (
        "offline", "unauthorized", "disconnected", "action_failures", "not_ready",
        "adb_server_down", "adb_not_found", "adb_timeout", "adb_version", "adb_error",
        "no_devices", "phones_disabled",
    ):
        assert f"reason_{reason}" in oa.STRINGS["zh"]


def test_panel_script_is_ascii_and_uses_snapshot_strings():
    assert oa.PANEL_SCRIPT.isascii()
    assert "ShowBalloonTip" in oa.PANEL_SCRIPT
    assert "strings" in oa.PANEL_SCRIPT
    assert "FileShare" in oa.PANEL_SCRIPT
    assert "ConvertFrom-Json" in oa.PANEL_SCRIPT


def test_config_is_opt_in_and_clamped():
    off = oa.parse_alert_config({
        "operator_alert_enabled": "true",
        "operator_alert_refresh_sec": 1,
        "operator_alert_language": "fr",
        "operator_alert_fail_streak": 99,
    })
    assert off == {"enabled": False, "refresh_sec": 30, "language": "zh", "fail_streak": 20}
    on = oa.parse_alert_config({
        "operator_alert_enabled": True,
        "operator_alert_refresh_sec": 99999,
        "operator_alert_language": "EN",
        "operator_alert_fail_streak": 1,
    })
    assert on == {"enabled": True, "refresh_sec": 3600, "language": "en", "fail_streak": 2}
    assert oa.parse_alert_config({})["enabled"] is False
    for key in ("operator_alert_enabled", "operator_alert_refresh_sec", "operator_alert_language",
                "operator_alert_fail_streak", "wallpaper_map"):
        assert key in agent_mod.AgentConfig.OPERATOR_KEYS


def test_wallpaper_map_shapes_drop_protected_and_junk():
    assert oa.parse_wallpaper_map({"07": SERIAL.lower()}) == {SERIAL: "07"}
    assert oa.parse_wallpaper_map({SERIAL.lower(): "07"}) == {SERIAL: "07"}
    assert oa.parse_wallpaper_map({SERIAL: 7}) == {SERIAL: "7"}
    assert oa.parse_wallpaper_map([{"wallpaper_no": "07", "serial": SERIAL.lower()}]) == {SERIAL: "07"}
    assert oa.parse_wallpaper_map({PROTECTED: "1", "1": PROTECTED}) == {}
    assert oa.parse_wallpaper_map([{"wallpaper_no": "nope", "serial": SERIAL}]) == {}
    assert oa.parse_wallpaper_map({"not a serial": "1"}) == {}


def test_offline_phone_payload_uses_wallpaper_number_not_serial(tmp_path):
    alert, calls = _alert(tmp_path)
    alert.observe([_phone()], "", _cfg())
    phone = calls[0]["phones"][0]
    assert phone["wallpaper_no"] == "07"
    assert phone["reason"] == "offline"
    assert phone["key"] == "07"
    assert calls[0]["raised"] == ["07"]
    assert SERIAL not in _blob(calls[0])
    assert all(SERIAL not in text.upper() for text in _texts(calls[0]))


def test_unnumbered_phone_does_not_leak_serial(tmp_path):
    alert, calls = _alert(tmp_path)
    alert.observe([_phone(OTHER)], "", {"operator_alert_enabled": True})
    phone = calls[0]["phones"][0]
    assert phone["unnumbered"] is True
    assert phone["wallpaper_no"] == ""
    assert phone["key"].startswith("unnumbered:")
    assert OTHER not in _blob(calls[0])


def test_protected_phone_is_omitted(tmp_path):
    alert, calls = _alert(tmp_path)
    alert.observe(
        [_phone(PROTECTED), _phone()],
        "",
        _cfg(wallpaper_map={SERIAL: "07", PROTECTED: "99"}),
    )
    assert [p["wallpaper_no"] for p in calls[0]["phones"]] == ["07"]
    assert PROTECTED not in _blob(calls[0])


def test_healthy_device_is_omitted_until_it_disappears(tmp_path):
    alert, calls = _alert(tmp_path)
    alert.observe([_phone(state="device")], "", _cfg())
    assert calls == []
    alert.observe([], "", _cfg())
    assert calls[0]["pc"]["reason"] == "no_devices"
    assert calls[0]["phones"][0]["reason"] == "disconnected"
    assert calls[0]["phones"][0]["wallpaper_no"] == "07"
    alert.observe([_phone(state="device")], "", _cfg())
    assert calls[-1]["phones"] == []
    assert calls[-1]["raised"] == []
    assert "07" in calls[-1]["cleared"]
    assert "pc" in calls[-1]["cleared"]


def test_adb_error_does_not_invent_disconnects_for_missing_numbers(tmp_path):
    alert, calls = _alert(tmp_path)
    alert.observe([], "adb_server_not_running", _cfg(wallpaper_map={SERIAL: "07", OTHER: "08"}))
    assert calls[0]["pc"]["reason"] == "adb_server_down"
    assert calls[0]["phones"] == []
    alert.observe([], "adb_version_mismatch client=1 server=2", _cfg())
    assert calls[-1]["pc"]["reason"] == "adb_version"
    alert.observe([_phone()], "adb_timeout", _cfg(wallpaper_map={SERIAL: "07", OTHER: "08"}))
    assert calls[-1]["pc"]["reason"] == "adb_timeout"
    assert [p["wallpaper_no"] for p in calls[-1]["phones"]] == ["07"]
    alert.observe([], "disabled", _cfg())
    assert calls[-1]["pc"]["reason"] == "phones_disabled"
    assert calls[-1]["phones"] == []
    alert.observe([], "adb_exit_1", {"operator_alert_enabled": True})
    assert calls[-1]["pc"]["reason"] == "adb_error"


def test_bad_adb_state_beats_failure_streak(tmp_path):
    alert, calls = _alert(tmp_path)
    for _ in range(5):
        alert.note_action(SERIAL, STATUS_FAILED)
    alert.observe([_phone(state="unauthorized")], "", _cfg())
    assert calls[0]["phones"][0]["reason"] == "unauthorized"
    alert.observe([_phone(state="recovery")], "", _cfg())
    assert calls[-1]["phones"][0]["reason"] == "not_ready"
    assert calls[-1]["phones"][0]["detail"] == "recovery"
    assert calls[-1]["raised"] == []
    other, more = _alert(tmp_path / "other")
    other.observe([_phone(state="no_permissions")], "", _cfg())
    assert more[0]["phones"][0]["reason"] == "unauthorized"


def test_fail_streak_raises_then_done_clears(tmp_path):
    alert, calls = _alert(tmp_path)
    cfg = _cfg(operator_alert_fail_streak=3)
    phone = [_phone(state="device")]
    alert.note_action(SERIAL, STATUS_REJECTED)
    alert.note_action(SERIAL, STATUS_FAILED)
    alert.note_action(SERIAL, STATUS_FAILED)
    alert.observe(phone, "", cfg)
    assert calls == []
    alert.note_action(SERIAL, STATUS_FAILED)
    alert.observe(phone, "", cfg)
    assert calls[0]["phones"][0]["reason"] == "action_failures"
    assert calls[0]["phones"][0]["detail"] == "3"
    assert calls[0]["raised"] == ["07"]
    alert.note_action(SERIAL, STATUS_DONE)
    alert.observe(phone, "", cfg)
    assert calls[-1]["phones"] == []
    assert calls[-1]["cleared"] == ["07"]
    assert SERIAL not in _blob(calls[-1])


def test_refresh_rewrites_without_a_new_toast(tmp_path):
    now = {"t": 1000.0}
    alert, calls = _alert(tmp_path, clock=lambda: now["t"])
    alert.observe([_phone()], "", _cfg())
    alert.observe([_phone()], "", _cfg())
    assert len(calls) == 1
    now["t"] = 1180.0
    alert.observe([_phone()], "", _cfg())
    assert len(calls) == 2
    assert calls[1]["raised"] == []
    assert calls[1]["cleared"] == []
    assert calls[1]["phones"][0]["age_sec"] == 180
    assert calls[1]["phones"][0]["since"] == 1000.0


def test_language_sidecar_overrides_agent_json(tmp_path):
    oa.save_language(tmp_path, "en")
    assert oa.load_language(tmp_path) == "en"
    assert oa.resolve_language(tmp_path, "zh") == "en"
    alert, calls = _alert(tmp_path)
    alert.observe([_phone()], "", _cfg(operator_alert_language="zh"))
    assert calls[0]["language"] == "en"
    assert calls[0]["strings"]["zh"]["toggle"] == "English"
    assert calls[0]["strings"]["en"]["toggle"] == "中文"
    (tmp_path / oa.LANG_NAME).write_text("{", encoding="utf-8")
    assert oa.load_language(tmp_path) is None
    assert oa.resolve_language(tmp_path, "en") == "en"


def test_headless_writes_snapshot_and_does_not_launch(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(oa, "gui_available", lambda: False)
    monkeypatch.setattr(oa, "launch_panel", lambda *a, **k: launched.append(a))
    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0)
    alert.observe([_phone()], "", _cfg())
    assert launched == []
    snap = json.loads((tmp_path / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert snap["phones"][0]["wallpaper_no"] == "07"
    assert "机房" in snap["strings"]["zh"]["title"]
    assert SERIAL not in json.dumps(snap)


def test_launch_failure_stays_headless(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "gui_available", lambda: True)

    def boom(_state):
        raise OSError("no desktop")

    monkeypatch.setattr(oa, "launch_panel", boom)
    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0)
    alert.observe([_phone()], "", _cfg())
    assert (tmp_path / oa.SNAP_NAME).is_file()


def test_presenter_exception_is_swallowed(tmp_path):
    def boom(_state, _payload):
        raise RuntimeError("boom")

    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0, present=boom)
    alert.observe([_phone()], "", _cfg())


def test_disabled_writes_nothing_until_it_has_alerted(tmp_path):
    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0)
    alert.observe([_phone()], "", {})
    assert not (tmp_path / oa.SNAP_NAME).exists()
    alert.observe([_phone()], "", _cfg())
    assert json.loads((tmp_path / oa.SNAP_NAME).read_text(encoding="utf-8"))["enabled"] is True
    alert.observe([_phone()], "", {"operator_alert_enabled": False})
    snap = json.loads((tmp_path / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert snap["enabled"] is False
    assert "phones" not in snap


def test_live_stream_host_does_not_present(tmp_path):
    alert, calls = _alert(tmp_path, live_stream=True)
    alert.note_action(SERIAL, STATUS_FAILED)
    alert.observe([_phone()], "", _cfg())
    assert calls == []
    assert alert._streaks == {}
    assert not (tmp_path / oa.SNAP_NAME).exists()


def test_gui_available_is_false_for_tests_headless_and_non_windows(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(oa._HEADLESS_ENV, raising=False)
    monkeypatch.setattr(oa.os, "name", "posix")
    assert oa.gui_available() is False
    monkeypatch.setattr(oa.os, "name", "nt")
    assert oa.gui_available() is True
    monkeypatch.setenv(oa._HEADLESS_ENV, "yes")
    assert oa.gui_available() is False
    monkeypatch.delenv(oa._HEADLESS_ENV)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_gui")
    assert oa.gui_available() is False


def test_agent_heartbeat_observes_without_putting_serial_on_the_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    seen = []
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data["operator_alert_enabled"] = True
    cfg.data["wallpaper_map"] = {SERIAL: "12"}
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    ag.phones.collect = lambda: ([_phone()], "")
    ag.operator_alert.present = lambda state, payload: seen.append(payload) or {"ok": True}

    hb = ag.build_heartbeat()

    assert hb["phones"][0]["serial"] == SERIAL
    assert seen[0]["phones"][0]["wallpaper_no"] == "12"
    assert seen[0]["phones"][0]["reason"] == "offline"
    assert SERIAL not in _blob(seen[0])
    assert agent_mod.AGENT_VERSION == "0.3.22"


def test_agent_execute_counts_phone_failures_and_ignores_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data["operator_alert_enabled"] = True
    cfg.data["operator_alert_fail_streak"] = 2
    cfg.data["wallpaper_map"] = {SERIAL: "4"}
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    ag.phone_ops.execute = lambda kind, payload, target: (STATUS_REJECTED, {"serial": SERIAL}, "nope")
    ag.execute({"kind": "phone_tap", "payload": {}, "target": {}})
    assert ag.operator_alert._streaks.get(SERIAL, 0) == 0

    ag.phone_ops.execute = lambda kind, payload, target: (STATUS_FAILED, {"serial": SERIAL}, "boom")
    ag.phone_flows.execute = lambda kind, payload, target, ops=None: (STATUS_FAILED, {"serial": SERIAL}, "boom")
    ag.execute({"kind": "phone_tap", "payload": {}, "target": {"serial": SERIAL}})
    ag.execute({"kind": "phone_post", "payload": {}, "target": {}})
    assert ag.operator_alert._streaks[SERIAL] == 2

    seen = []
    ag.phones.collect = lambda: ([_phone(state="device")], "")
    ag.operator_alert.present = lambda state, payload: seen.append(payload) or {"ok": True}
    ag.build_heartbeat()
    assert seen[0]["phones"][0]["reason"] == "action_failures"
    assert seen[0]["phones"][0]["wallpaper_no"] == "4"
    assert SERIAL not in _blob(seen[0])

    ag.phone_ops.execute = lambda kind, payload, target: (STATUS_DONE, {"serial": SERIAL}, "ok")
    ag.execute({"kind": "phone_tap", "payload": {}, "target": {}})
    ag.build_heartbeat()
    assert seen[-1]["phones"] == []
    assert seen[-1]["cleared"] == ["4"]


def test_agent_alert_error_does_not_break_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: False)
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data["operator_alert_enabled"] = True
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")

    def boom(*_a, **_k):
        raise RuntimeError("alert down")

    ag.operator_alert.observe = boom
    hb = ag.build_heartbeat()
    assert hb["phones_error"] in ("adb_server_not_running", "adb_not_found")


def test_live_stream_agent_does_not_write_an_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: True)
    fleet = tmp_path / "fleet"
    cfg = agent_mod.AgentConfig(fleet)
    cfg.data["operator_alert_enabled"] = True
    cfg.data["wallpaper_map"] = {SERIAL: "3"}
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    ag.phones.collect = lambda: ([_phone()], "")
    ag.build_heartbeat()
    assert not (fleet / oa.SNAP_NAME).exists()
    ag.execute({"kind": "phone_tap", "payload": {}, "target": {"serial": SERIAL}})
    assert ag.operator_alert._streaks == {}


def _locked_agent(tmp_path, monkeypatch, *, live: bool = False):
    monkeypatch.setattr(agent_mod, "is_live_stream_host", lambda state_dir=None: live)
    fleet = tmp_path / "fleet"
    cfg = agent_mod.AgentConfig(fleet)
    cfg.data["phones_exclude"] = ["ABC123"]
    cfg.save()
    agent = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    return agent, fleet


def _push(patch):
    return {"task_id": "t-push", "kind": TASK_PUSH_CONFIG, "payload": {"patch": patch}}


def test_push_config_writes_operator_alert_keys_and_snapshot_hides_serial(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="fleet.agent")
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    patch = {
        "operator_alert_enabled": True,
        "operator_alert_refresh_sec": 60,
        "operator_alert_language": "EN",
        "operator_alert_fail_streak": 4,
        "wallpaper_map": {"07": SERIAL},
        "phone_flows_enabled": False,
    }
    status, result, detail = agent.execute(_push(patch))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["operator_alert_enabled"] is True
    assert result["operator_alert_refresh_sec"] == 60
    assert result["operator_alert_language"] == "en"
    assert result["operator_alert_fail_streak"] == 4
    assert result["wallpaper_map_entries"] == 1
    assert result["phone_flows_enabled"] is False
    assert SERIAL not in _blob(result)
    assert SERIAL not in caplog.text
    disk = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert disk["operator_alert_enabled"] is True
    assert disk["operator_alert_refresh_sec"] == 60
    assert disk["operator_alert_language"] == "en"
    assert disk["operator_alert_fail_streak"] == 4
    assert disk["wallpaper_map"] == {"07": SERIAL}
    assert disk["phones_exclude"] == ["ABC123"]
    assert disk["phone_flows_enabled"] is False
    agent.operator_alert.observe([_phone()], "", agent.cfg.data)
    snap = json.loads((fleet / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert snap["phones"][0]["wallpaper_no"] == "07"
    assert snap["phones"][0]["reason"] == "offline"
    assert SERIAL not in _blob(snap)
    assert all(SERIAL not in text.upper() for text in _texts(snap))
    for key in (
        "operator_alert_enabled", "operator_alert_refresh_sec", "operator_alert_language",
        "operator_alert_fail_streak", "wallpaper_map",
    ):
        assert key in agent_mod._PUSH_CONFIG_KEYS


def test_push_config_rejects_unknown_and_bad_operator_alert_values(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    before = (fleet / "agent.json").read_text(encoding="utf-8")
    cases = (
        {},
        {"operator_alert_enabled": "true"},
        {"operator_alert_enabled": 1},
        {"operator_alert_language": "fr"},
        {"operator_alert_refresh_sec": True},
        {"operator_alert_refresh_sec": 60.5},
        {"operator_alert_fail_streak": "3"},
        {"wallpaper_map": SERIAL},
        {"operator_alert_enabled": True, "phones_exclude": []},
        {"operator_alert_bogus": True},
        {"operator_alert_enabled": True, "node_key": "nk_secret"},
    )
    for patch in cases:
        status, result, detail = agent.execute(_push(patch))
        assert (status, detail) == (STATUS_REJECTED, "not_supported_in_agent_v1"), patch
        assert result == {}
        assert (fleet / "agent.json").read_text(encoding="utf-8") == before
        assert agent.cfg.data.get("operator_alert_enabled") is not True


def test_push_config_live_stream_does_not_enable_operator_alert(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch, live=True)
    before = (fleet / "agent.json").read_text(encoding="utf-8")
    status, result, detail = agent.execute(_push({
        "operator_alert_enabled": True,
        "wallpaper_map": {"7": SERIAL},
    }))
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert result == {}
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before
    assert agent.operator_alert.live_stream is True
    agent.operator_alert.observe(
        [_phone()], "", {"operator_alert_enabled": True, "wallpaper_map": {SERIAL: "7"}})
    assert not (fleet / oa.SNAP_NAME).exists()
    status, result, detail = agent.execute({"task_id": "t-diag", "kind": TASK_OPERATOR_ALERT_DIAG, "payload": {}})
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["enabled"] is False
    assert result["live_stream"] is True
    assert result["phones"] == []
    assert SERIAL not in _blob(result)


def test_operator_alert_diag_redacts_snapshot_serials(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    agent.execute(_push({
        "operator_alert_enabled": True,
        "operator_alert_language": "zh",
        "operator_alert_refresh_sec": 90,
        "operator_alert_fail_streak": 3,
        "wallpaper_map": [{"wallpaper_no": "07", "serial": SERIAL}],
    }))
    poison = {
        "enabled": True,
        "language": SERIAL,
        "refresh_sec": 1,
        "phones": [{
            "wallpaper_no": "07",
            "reason": "offline",
            "serial": SERIAL,
            "detail": SERIAL,
            "_serial": SERIAL,
            "key": SERIAL,
        }, {
            "wallpaper_no": SERIAL,
            "reason": SERIAL,
            "unnumbered": False,
        }],
        "pc": {"reason": "adb_server_down", "serial": SERIAL},
        "secret": SERIAL,
        "strings": {"zh": {"title": SERIAL}},
    }
    (fleet / oa.SNAP_NAME).write_text(json.dumps(poison), encoding="utf-8")
    status, result, detail = agent.execute({"task_id": "t-diag", "kind": TASK_OPERATOR_ALERT_DIAG, "payload": {}})
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["enabled"] is True
    assert result["language"] == "zh"
    assert result["refresh_sec"] == 90
    assert result["fail_streak"] == 3
    assert result["pc_reason"] == "adb_server_down"
    assert result["phones"][0] == {"wallpaper_no": "07", "reason": "offline"}
    assert result["phones"][1]["wallpaper_no"] == ""
    assert result["phones"][1]["reason"] == ""
    assert result["phones"][1]["unnumbered"] is True
    assert "serial" not in result["phones"][0]
    assert SERIAL not in _blob(result)
    assert TASK_OPERATOR_ALERT_DIAG in TASK_KINDS
    assert TASK_OPERATOR_ALERT_DIAG not in REMOTE_PHONE_KINDS
    assert TASK_OPERATOR_ALERT_DIAG not in LEGACY_ALLOWED_KINDS
