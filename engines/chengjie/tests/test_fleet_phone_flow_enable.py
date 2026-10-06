"""Remote phone_flows_enabled and social dry_run. No device, no adb input."""
from __future__ import annotations

import base64
import json
import logging
import os
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from src.fleet import admin as admin_mod
from src.fleet.agent import AgentConfig, NodeAgent
from src.fleet.phone_flow_rules import FLOW_BY_KIND, validate_flow_payload
from src.fleet.phone_flows import PhoneFlows, _MAX_MAP_BYTES, bundled_ui_map, compile_flow
from src.fleet.phone_rules import PhoneOpError, sanitize_phone_result
from src.fleet.protocol import (
    CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2, CAP_PHONE_OPS_V1, PHONE_FLOW_KINDS, PHONE_SESSION_KINDS,
    STATUS_DONE, STATUS_REJECTED, TASK_PHONE_COMMENT, TASK_PHONE_DM, TASK_PHONE_FOLLOW, TASK_PHONE_LIKE,
    TASK_PHONE_POST, TASK_PHONE_WARMUP, TASK_PHONE_WATCH, TASK_PUSH_CONFIG,
)
from tests.test_fleet_control import OP, _client, st  # noqa: F401
from tests.test_fleet_phone_flows import _flow_node, _ops


def _social_body(kind: str) -> dict:
    if kind == TASK_PHONE_POST:
        return {"app": "facebook", "text": "hello fleet"}
    if kind == TASK_PHONE_COMMENT:
        return {"app": "instagram", "text": "hello fleet"}
    if kind == TASK_PHONE_FOLLOW:
        return {"app": "tiktok", "handle": "some.user"}
    if kind == TASK_PHONE_LIKE:
        return {"app": "facebook"}
    if kind == TASK_PHONE_WARMUP:
        return {"app": "tiktok", "scrolls": 2, "likes": 1}
    if kind == TASK_PHONE_WATCH:
        return {"app": "instagram", "watches": 2, "likes": 0}
    if kind == TASK_PHONE_DM:
        return {"app": "facebook", "handle": "some.user", "text": "hello fleet"}
    raise AssertionError(kind)


def _map_flow(kind: str, payload: dict) -> str:
    flow = FLOW_BY_KIND[kind]
    if kind == TASK_PHONE_POST and payload.get("media"):
        return "post_media"
    return flow


def _locked_agent(tmp_path, monkeypatch, *, live: bool = False, flows: bool = False):
    monkeypatch.setattr("src.fleet.agent.is_live_stream_host", lambda state_dir=None: live)
    fleet = tmp_path / "fleet"
    cfg = AgentConfig(fleet)
    cfg.data["phones_exclude"] = ["ABC123"]
    if flows:
        cfg.data["phone_flows_enabled"] = True
    cfg.save()
    agent = NodeAgent(cfg, http=lambda *a: (200, {}))
    return agent, fleet


def _push(patch):
    return {"task_id": "t-push", "kind": TASK_PUSH_CONFIG, "payload": {"patch": patch}}


_MAP_SRC = Path(__file__).resolve().parents[1] / "src" / "fleet" / "phone_ui_map.json"
_NOTE = "WALLPAPER02-SECRET-NOTE"


def _tuned_map(note: str = _NOTE, icon=None):
    data = json.loads(_MAP_SRC.read_text(encoding="utf-8"))
    data["note"] = note
    data["apps"]["facebook"]["anchors"]["app_icon"] = list(icon or [150, 555])
    return data


def _like(serial: str = "S1"):
    return {
        "task_id": "t-like", "kind": TASK_PHONE_LIKE,
        "payload": {"app": "facebook", "dry_run": True}, "target": {"serial": serial},
    }


def _assert_secret_absent(*blobs: str) -> None:
    for blob in blobs:
        assert _NOTE not in blob


def test_push_config_writes_phone_flows_enabled_and_hot_reloads(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    assert agent.phone_flows.enabled is False
    status, result, detail = agent.execute(_push({"phone_flows_enabled": True}))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result == {"phone_flows_enabled": True}
    assert agent.phone_flows.enabled is True
    caps = agent.build_heartbeat()["caps"]
    assert CAP_PHONE_FLOWS_V1 in caps and CAP_PHONE_FLOWS_V2 in caps
    disk = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert disk["phone_flows_enabled"] is True
    assert disk["phones_exclude"] == ["ABC123"]
    status, result, detail = agent.execute(_push({"phone_flows_enabled": False}))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result == {"phone_flows_enabled": False}
    assert agent.phone_flows.enabled is False
    caps = agent.build_heartbeat()["caps"]
    assert CAP_PHONE_FLOWS_V1 not in caps and CAP_PHONE_FLOWS_V2 not in caps
    disk = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert disk["phone_flows_enabled"] is False
    assert disk["phones_exclude"] == ["ABC123"]


def test_push_config_refuses_live_stream_before_write(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch, live=True, flows=True)
    assert agent.phone_flows.enabled is True
    before = (fleet / "agent.json").read_text(encoding="utf-8")
    status, result, detail = agent.execute(_push({"phone_flows_enabled": False}))
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert result == {}
    assert agent.phone_flows.enabled is True
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before
    status, _, detail = agent.execute(_push({"phone_flows_enabled": True, "phones_exclude": []}))
    assert (status, detail) == (STATUS_REJECTED, "not_supported_in_agent_v1")
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before


def test_push_config_rejects_other_keys_empty_and_non_bool(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    before = (fleet / "agent.json").read_text(encoding="utf-8")
    cases = (
        {},
        {"patch": {}},
        {"patch": {"phone_flows_enabled": "true"}},
        {"patch": {"phone_flows_enabled": 1}},
        {"patch": {"phones_exclude": []}},
        {"patch": {"phone_flows_enabled": True, "adb_path": "adb"}},
        {"phone_flows_enabled": True},
    )
    for payload in cases:
        status, result, detail = agent.execute({"task_id": "t", "kind": TASK_PUSH_CONFIG, "payload": payload})
        assert (status, detail) == (STATUS_REJECTED, "not_supported_in_agent_v1"), payload
        assert result == {}
        assert agent.phone_flows.enabled is False
        assert (fleet / "agent.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("kind", list(PHONE_FLOW_KINDS) + list(PHONE_SESSION_KINDS))
def test_dry_run_returns_compiled_plan_without_adb(kind):
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True)
    body = dict(_social_body(kind), dry_run=True, evil="rm")
    status, result, detail = flows.execute(kind, body, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert fake.calls == [] and slept == []
    assert result["dry_run"] is True and result["space"] == "permille"
    assert result["plan"] and result["steps"] == len(result["plan"])
    assert any(step["op"] == "tap" for step in result["plan"])
    assert {step["op"] for step in result["plan"]} <= {"tap", "swipe", "key", "text", "dwell"}
    validated = validate_flow_payload(kind, body)
    compiled = compile_flow(validated["app"], _map_flow(kind, validated), validated, 1000, 1000, bundled_ui_map())
    assert len(result["plan"]) == len(compiled)
    for step, (sk, sp) in zip(result["plan"], compiled):
        if step["op"] == "text":
            assert sp["text"] == step["text"] and step["chars"] == len(step["text"])
        elif step["op"] == "tap":
            assert sp == {"x": step["x"], "y": step["y"]}
        elif step["op"] == "key":
            assert sp == {"key": step["key"]}
    assert "evil" not in json.dumps(result)
    assert all(step.get("text") != "rm" for step in result["plan"])


def test_predict_only_normalizes_and_absent_flag_still_injects():
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, {"app": "tiktok", "predict_only": True, "evil": "rm"}, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert result["dry_run"] is True and "predict_only" not in result
    assert fake.calls == [] and slept == []
    out = validate_flow_payload(TASK_PHONE_LIKE, {"app": "facebook", "predict_only": True, "evil": "rm"})
    assert out == {"app": "facebook", "scrolls": 0, "dry_run": True}
    omitted = validate_flow_payload(TASK_PHONE_LIKE, {"app": "facebook", "dry_run": False, "predict_only": False})
    assert omitted == {"app": "facebook", "scrolls": 0}
    real_ops, real_fake, real_slept = _ops()
    status, result, detail = flows.execute(TASK_PHONE_LIKE, {"app": "facebook"}, {"serial": "S1"}, ops=real_ops)
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result.get("dry_run") is not True
    assert any(args[2:4] == ("shell", "input") for args in real_fake.actions())
    assert real_slept
    assert any(args[2:] == ("exec-out", "screencap") for args in real_fake.actions())


def test_bad_dry_run_does_not_inject():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, _result, detail = flows.execute(
        TASK_PHONE_COMMENT, {"app": "facebook", "text": "hi", "dry_run": "true"}, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "bad_dry_run")
    assert fake.calls == []
    with pytest.raises(PhoneOpError) as ei:
        validate_flow_payload(TASK_PHONE_LIKE, {"app": "facebook", "dry_run": True, "predict_only": False})
    assert ei.value.code == "bad_dry_run"


@pytest.mark.parametrize("kind", list(PHONE_FLOW_KINDS) + list(PHONE_SESSION_KINDS))
def test_disabled_flows_reject_social_kinds_even_with_dry_run(kind):
    ops, fake, slept = _ops()
    flows = PhoneFlows(enabled=False)
    status, result, detail = flows.execute(kind, dict(_social_body(kind), dry_run=True), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "phone_flows_disabled")
    assert result == {}
    assert fake.calls == [] and slept == []


def test_phone_ops_disabled_and_protected_reject_dry_run_without_adb():
    ops, fake, _slept = _ops(enabled=False)
    status, _result, detail = PhoneFlows(enabled=True).execute(
        TASK_PHONE_WARMUP, {"app": "tiktok", "dry_run": True}, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "phone_ops_disabled")
    assert fake.calls == []
    for serial in ("3B1F4KE5MS140P4X", "192.168.0.148:5555"):
        live_ops, live_fake, _ = _ops()
        status, _result, detail = PhoneFlows(enabled=True).execute(
            TASK_PHONE_DM, {"app": "instagram", "handle": "some.user", "text": "hi", "dry_run": True},
            {"serial": serial}, ops=live_ops)
        assert (status, detail) == (STATUS_REJECTED, "protected_phone")
        assert live_fake.calls == []


def test_social_endpoint_keeps_dry_run_and_drops_unknown_keys(st):
    client = _client(st)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1, CAP_PHONE_FLOWS_V2], mid="m-dry-run")
    posted = client.post(
        f"/api/fleet/nodes/{nid}/phones/S1/social/post", headers=OP,
        json={"app": "Instagram", "text": "hello fleet", "dry_run": True, "evil": "rm"})
    assert posted.status_code == 200, posted.text
    assert posted.json()["task"]["payload"] == {
        "app": "instagram", "text": "hello fleet", "media": 0, "media_slot": 0, "dry_run": True}
    predicted = client.post(
        f"/api/fleet/nodes/{nid}/phones/S1/social/warmup", headers=OP,
        json={"app": "tiktok", "scrolls": 4, "likes": 1, "predict_only": True, "evil": "rm"})
    assert predicted.status_code == 200, predicted.text
    assert predicted.json()["task"]["payload"] == {"app": "tiktok", "scrolls": 4, "likes": 1, "dry_run": True}
    bad = client.post(
        f"/api/fleet/nodes/{nid}/phones/S1/social/like", headers=OP,
        json={"app": "facebook", "dry_run": "true"})
    assert bad.status_code == 400 and bad.json()["detail"] == "bad_dry_run"


def test_sanitize_keeps_plan_text_only_for_dry_run():
    plan = [
        {"op": "tap", "x": 10, "y": 20, "secret": "nope"},
        {"op": "text", "text": "hello fleet", "chars": 11},
    ]
    clean = sanitize_phone_result(TASK_PHONE_POST, {
        "app": "facebook", "flow": "post", "steps": 2, "dry_run": True, "space": "permille",
        "plan": plan, "text": "secret", "handle": "ada",
    })
    assert clean["dry_run"] is True and clean["space"] == "permille"
    assert clean["plan"] == [{"op": "tap", "x": 10, "y": 20}, {"op": "text", "text": "hello fleet", "chars": 11}]
    assert "text" not in clean and "secret" not in json.dumps({k: v for k, v in clean.items() if k != "plan"})
    smuggled = sanitize_phone_result(TASK_PHONE_POST, {
        "app": "facebook", "flow": "post", "steps": 2, "plan": plan, "text": "secret",
    })
    blob = json.dumps(smuggled)
    assert "plan" not in smuggled and "dry_run" not in smuggled
    assert "hello fleet" not in blob and "secret" not in blob


def test_admin_enable_phone_flows_payload_and_help(monkeypatch):
    calls = []

    def http(method, url, body):
        calls.append(body)
        return {"ok": True, "task_id": "t"}

    adm = admin_mod.Admin("https://bd2026.cc/fleet", "T", http=http)
    adm.enable_phone_flows("n-room")
    assert calls[-1]["kind"] == TASK_PUSH_CONFIG
    assert calls[-1]["payload"] == {"patch": {"phone_flows_enabled": True}}
    assert calls[-1]["ttl_sec"] == 600

    buf = StringIO()
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as ei:
            admin_mod.main(["enable-phone-flows", "--help"])
    assert ei.value.code == 0
    help_text = buf.getvalue()
    assert "--node" in help_text and "--yes" in help_text
    assert "phone_flows_enabled" in help_text
    assert "dry_run" in help_text and "predict_only" in help_text
    assert "live-stream" in help_text

    queued = []

    class FakeAdmin:
        def __init__(self, *args, **kwargs):
            pass

        def enable_phone_flows(self, node_id):
            queued.append(node_id)
            return {"ok": True, "task_id": "t"}

    monkeypatch.setattr(admin_mod, "Admin", FakeAdmin)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert admin_mod.main(["--controller", "https://c", "--token", "t", "enable-phone-flows", "--node", "n1"]) == 1
    assert queued == []
    assert admin_mod.main([
        "--controller", "https://c", "--token", "t", "enable-phone-flows", "--node", "n1", "--yes"]) == 0
    assert queued == ["n1"]


def test_push_config_phone_ui_map_path_hot_reloads(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="fleet.agent")
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    src = tmp_path / "wallpaper02.json"
    src.write_text(json.dumps(_tuned_map()), encoding="utf-8")
    raw = str(src)
    before_flows = agent.phone_flows.enabled
    status, mapped, detail = agent.execute(_push({"phone_ui_map": raw}))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert mapped == {"phone_ui_map": raw, "phone_ui_map_bytes": src.stat().st_size}
    assert agent.phone_flows.enabled is before_flows
    assert agent.phone_flows.ui_map_path == raw
    assert agent.phone_flows.load_map()["apps"]["facebook"]["anchors"]["app_icon"] == (150, 555)
    disk = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert disk["phone_ui_map"] == raw
    assert disk["phones_exclude"] == ["ABC123"]
    assert "phone_ui_map_json" not in disk and "phone_ui_map_b64" not in disk
    status, result, detail = agent.execute(_push({"phone_flows_enabled": True}))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result == {"phone_flows_enabled": True}
    status, liked, detail = agent.execute(_like())
    assert (status, detail) == (STATUS_DONE, "dry_run")
    taps = [(step["x"], step["y"]) for step in liked["plan"] if step["op"] == "tap"]
    assert (150, 555) in taps
    assert (120, 420) not in taps
    status, _prot, detail = agent.execute(_like("3B1F4KE5MS140P4X"))
    assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    _assert_secret_absent(caplog.text, json.dumps(mapped), (fleet / "agent.json").read_text(encoding="utf-8"))
    assert any("push_config set phone_ui_map" in rec.message for rec in caplog.records)


def test_push_config_materializes_ui_map_json_and_b64(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="fleet.agent")
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    data = _tuned_map()
    status, result, detail = agent.execute(_push({
        "phone_flows_enabled": True, "phone_ui_map_json": data,
    }))
    assert (status, detail) == (STATUS_DONE, "ok")
    remote = fleet / "phone_ui_map.remote.json"
    assert remote.is_file()
    assert not (fleet / "phone_ui_map.remote.json.tmp").exists()
    if os.name != "nt":
        assert remote.stat().st_mode & 0o777 == 0o600
    body = remote.read_bytes()
    assert _NOTE.encode() in body
    assert json.loads(body)["apps"]["facebook"]["anchors"]["app_icon"] == [150, 555]
    assert result["phone_flows_enabled"] is True
    assert result["phone_ui_map"] == str(remote.resolve())
    assert result["phone_ui_map_bytes"] == len(body)
    assert agent.phone_flows.ui_map_path == result["phone_ui_map"]
    assert agent.phone_flows.load_map()["apps"]["facebook"]["anchors"]["app_icon"] == (150, 555)
    disk_text = (fleet / "agent.json").read_text(encoding="utf-8")
    disk = json.loads(disk_text)
    assert disk["phone_ui_map"] == result["phone_ui_map"]
    assert disk["phone_flows_enabled"] is True
    assert disk["phones_exclude"] == ["ABC123"]
    assert "phone_ui_map_json" not in disk and "phone_ui_map_b64" not in disk
    status, liked, detail = agent.execute(_like())
    assert (status, detail) == (STATUS_DONE, "dry_run")
    taps = [(step["x"], step["y"]) for step in liked["plan"] if step["op"] == "tap"]
    assert (150, 555) in taps and (120, 420) not in taps
    status, _prot, detail = agent.execute(_like("3B1F4KE5MS140P4X"))
    assert (status, detail) == (STATUS_REJECTED, "protected_phone")

    rewritten = _tuned_map(note="WALLPAPER02-B64-NOTE", icon=[151, 556])
    blob = json.dumps(rewritten, ensure_ascii=False).encode("utf-8")
    status, result, detail = agent.execute(_push({"phone_ui_map_b64": base64.b64encode(blob).decode("ascii")}))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert remote.read_bytes() == blob
    assert result["phone_ui_map_bytes"] == len(blob)
    assert agent.phone_flows.load_map()["apps"]["facebook"]["anchors"]["app_icon"] == (151, 556)
    text_form = json.dumps(_tuned_map(note="WALLPAPER02-TEXT-NOTE", icon=[152, 557]))
    status, result, detail = agent.execute(_push({"phone_ui_map_json": text_form}))
    assert (status, detail) == (STATUS_DONE, "ok")
    assert remote.read_bytes() == text_form.encode("utf-8")
    assert agent.phone_flows.load_map()["apps"]["facebook"]["anchors"]["app_icon"] == (152, 557)
    _assert_secret_absent(
        caplog.text, json.dumps(result), (fleet / "agent.json").read_text(encoding="utf-8"),
    )
    assert _NOTE.encode() not in remote.read_bytes()


def test_push_config_rejects_bad_ui_map_without_write(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="fleet.agent")
    agent, fleet = _locked_agent(tmp_path, monkeypatch)
    remote = fleet / "phone_ui_map.remote.json"
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_tuned_map()), encoding="utf-8")
    missing = tmp_path / "missing.json"
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    huge = tmp_path / "huge.json"
    huge.write_bytes(b"x" * (_MAX_MAP_BYTES + 1))
    folder = tmp_path / "mapdir"
    folder.mkdir()
    link = tmp_path / "link.json"
    try:
        link.symlink_to(good)
    except OSError:
        link = None

    early = (
        {"phone_ui_map": 1},
        {"phone_ui_map_b64": 1},
        {"phone_ui_map_json": ["nope"]},
        {"phone_ui_map_b64": "YQ==", "phone_ui_map_json": {}},
        {"phone_ui_map": str(good), "phone_ui_map_b64": "YQ=="},
        {"phone_ui_map_json": {}, "adb_path": "adb"},
    )
    for patch in early:
        status, result, detail = agent.execute(_push(patch))
        assert (status, detail) == (STATUS_REJECTED, "not_supported_in_agent_v1"), patch
        assert result == {}
        assert not remote.exists()

    shaped = (
        ({"phone_ui_map_json": _NOTE + "{not-json"}, "ui_map_invalid"),
        ({"phone_ui_map_json": {"version": 1}}, "ui_map_invalid"),
        ({"phone_ui_map_json": "{}"}, "ui_map_invalid"),
        ({"phone_ui_map_b64": "!!!!"}, "ui_map_invalid"),
        ({"phone_ui_map_b64": ""}, "ui_map_invalid"),
        ({"phone_ui_map": str(empty)}, "ui_map_invalid"),
        ({"phone_ui_map": str(good) + "\nextra"}, "ui_map_invalid"),
        ({"phone_ui_map": "has\x00nul"}, "ui_map_invalid"),
        ({"phone_ui_map": ""}, "ui_map_missing"),
        ({"phone_ui_map": "   "}, "ui_map_missing"),
        ({"phone_ui_map": "phone_ui_map.json"}, "ui_map_missing"),
        ({"phone_ui_map": str(missing)}, "ui_map_missing"),
        ({"phone_ui_map": str(folder)}, "ui_map_missing"),
        ({"phone_ui_map": str(huge)}, "ui_map_too_large"),
        ({"phone_ui_map_json": "x" * (_MAX_MAP_BYTES + 1)}, "ui_map_too_large"),
        ({"phone_ui_map_b64": base64.b64encode(b"x" * (_MAX_MAP_BYTES + 1)).decode("ascii")}, "ui_map_too_large"),
    )
    oversized = _tuned_map()
    oversized["note"] = "n" * (_MAX_MAP_BYTES + 1)
    shaped = shaped + (({"phone_ui_map_json": oversized}, "ui_map_too_large"),)
    if link is not None:
        shaped = shaped + (({"phone_ui_map": str(link)}, "ui_map_invalid"),)
    for patch, code in shaped:
        status, result, detail = agent.execute(_push(patch))
        assert (status, detail) == (STATUS_REJECTED, code), patch
        assert result == {}
        assert not remote.exists()
        assert "phone_ui_map" not in json.loads((fleet / "agent.json").read_text(encoding="utf-8"))

    status, result, detail = agent.execute(_push({"phone_ui_map_json": _tuned_map()}))
    assert (status, detail) == (STATUS_DONE, "ok")
    kept_file = remote.read_bytes()
    kept_cfg = (fleet / "agent.json").read_text(encoding="utf-8")
    assert _NOTE.encode() in kept_file
    for patch, code in shaped:
        status, result, detail = agent.execute(_push(patch))
        assert (status, detail) == (STATUS_REJECTED, code), patch
        assert remote.read_bytes() == kept_file
        assert (fleet / "agent.json").read_text(encoding="utf-8") == kept_cfg
        assert not (fleet / "phone_ui_map.remote.json.tmp").exists()
    _assert_secret_absent(caplog.text, kept_cfg, json.dumps(result))


def test_push_config_live_stream_refuses_ui_map(tmp_path, monkeypatch):
    agent, fleet = _locked_agent(tmp_path, monkeypatch, live=True, flows=True)
    src = tmp_path / "wallpaper02.json"
    src.write_text(json.dumps(_tuned_map()), encoding="utf-8")
    before = (fleet / "agent.json").read_text(encoding="utf-8")
    remote = fleet / "phone_ui_map.remote.json"
    status, result, detail = agent.execute(_push({
        "phone_flows_enabled": False, "phone_ui_map_json": _tuned_map(),
    }))
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert result == {}
    assert agent.phone_flows.enabled is True
    assert not remote.exists()
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before
    status, result, detail = agent.execute(_push({
        "phone_ui_map_json": _NOTE + "{not-json",
    }))
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert not remote.exists()
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before
    status, result, detail = agent.execute(_push({"phone_ui_map": str(src)}))
    assert (status, detail) == (STATUS_REJECTED, "live_stream_host")
    assert agent.phone_flows.ui_map_path == ""
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before
    status, _, detail = agent.execute(_push({"phone_ui_map_json": _tuned_map(), "adb_path": "adb"}))
    assert (status, detail) == (STATUS_REJECTED, "not_supported_in_agent_v1")
    assert (fleet / "agent.json").read_text(encoding="utf-8") == before
    assert not remote.exists()
