"""0.3.5 节点身份：克隆盘（同 MachineGuid + 同主机名）不再共用一个 machine_id / node。

根因回归：0.3.4 的 machine_id = sha256(MachineGuid|主机名)，克隆的两台算出同一个值；主控按
machine_id 复用 node_id 并换 key，两台电脑互相把对方挤下线、控制台只剩一行。
"""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from src.fleet import agent as agent_mod
from src.fleet import identity
from src.fleet.agent import AgentConfig, NodeAgent
from src.fleet.local_status import build_local_status
from src.fleet.panel import PAGE_HTML
from src.fleet.store import FleetStore

ENGINE = Path(__file__).resolve().parents[1]
CTL = "https://ctl.test/fleet"
GUID = "6f1c1b62-cloned-image-guid"
HOST = "PC-20240123AORY"
HW_A = {"smbios_uuid": "4C4C4544-0042-3510-8051-B4C04F4A3132", "disk_serial": "S4EWNX0R123456A",
        "mac": "D8-BB-C1-11-22-33"}
HW_B = {"smbios_uuid": "4C4C4544-0042-3510-8051-B4C04F4A9999", "disk_serial": "S4EWNX0R654321B",
        "mac": "D8-BB-C1-44-55-66"}


def _es(tag: str) -> str:
    return "es_" + tag + "-" + "sec" + "ret" + "-0123456789"


def _legacy_id(guid: str = GUID, host: str = HOST) -> str:
    """What agent 0.3.4 cached: sha256(MachineGuid|hostname)."""
    return "m-" + hashlib.sha256(f"{guid}|{host}".encode("utf-8")).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No real icacls / ACL reads; each test sets its own fake hardware."""
    def _mk(p):
        Path(p).mkdir(parents=True, exist_ok=True)

    for mod in (identity, agent_mod):
        monkeypatch.setattr(mod, "lock_state_dir", _mk, raising=False)
        monkeypatch.setattr(mod, "assign_owner_admins", lambda p: None, raising=False)
        monkeypatch.setattr(mod, "discard_untrusted_secret", lambda p, **k: False, raising=False)
        monkeypatch.setattr(mod, "state_dir_is_locked", lambda p: True, raising=False)
    monkeypatch.setattr(identity, "_licensing_fingerprint", lambda: "")
    monkeypatch.delenv(identity.ENV_HW_PROBE, raising=False)
    identity.reset_hardware_cache()
    yield
    identity.reset_hardware_cache()


def _machine(monkeypatch, hw, *, guid=GUID, host=HOST):
    monkeypatch.setattr(identity, "_os_machine_guid", lambda: guid)
    monkeypatch.setattr(identity, "host_name", lambda: host)
    monkeypatch.setattr(agent_mod, "host_name", lambda: host)
    monkeypatch.setattr(identity, "_probe_hardware", lambda: dict(hw))
    identity.reset_hardware_cache()


def _store_http(st, calls):
    """Route agent HTTP calls straight into a FleetStore (the controller's storage logic)."""
    def http(method, url, body, headers, timeout):
        calls.append({"url": url, "body": json.loads(json.dumps(body)) if body is not None else None})
        path = url[len(CTL):]
        if path == "/api/fleet/enroll":
            common = dict(machine_id=body["machine_id"], host_name=body.get("host_name", ""),
                          proto_version=body.get("proto_version"), agent_version=body.get("agent_version", ""),
                          os_label=body.get("os", ""), meta=body.get("meta"), enroll_secret=body.get("enroll_secret", ""),
                          instances=body.get("instances"))
            if body.get("room_key"):
                res = st.redeem_room_key(body["room_key"], client_ip="198.51.100.9", **common)
            else:
                res = st.request_pending(client_ip="198.51.100.9", **common)
            return (200, res) if res.get("ok") else (403, {"detail": res.get("error")})
        return 404, {"detail": "not routed"}
    return http


@pytest.fixture
def st(tmp_path):
    s = FleetStore(tmp_path / "fleet.db", offline_after_sec=120)
    yield s
    s.close()


# ── 根因 ────────────────────────────────────────────────────────────────────
def test_root_cause_same_machine_id_reuses_node_and_kills_other_key(st):
    """Two clones with one machine_id: the second room-key enroll takes over the first PC's node."""
    rk = st.create_room_key(group_name="机房", max_uses=5, ttl_hours=1)["room_key"]
    mid = _legacy_id()
    first = st.redeem_room_key(rk, machine_id=mid, host_name=HOST, proto_version=1, enroll_secret=_es("a"))
    second = st.redeem_room_key(rk, machine_id=mid, host_name=HOST, proto_version=1, enroll_secret=_es("b"))
    assert first["node_id"] == second["node_id"]
    assert st.authenticate(first["node_key"]) is None      # PC 1 is now locked out
    assert st.authenticate(second["node_key"]) is not None


# ── 新的派生规则 ───────────────────────────────────────────────────────────
def test_same_guid_and_hostname_different_smbios_give_different_ids(monkeypatch, tmp_path):
    _machine(monkeypatch, HW_A)
    a = identity.node_machine_id(tmp_path / "a")
    _machine(monkeypatch, dict(HW_B, disk_serial=HW_A["disk_serial"], mac=HW_A["mac"]))  # only SMBIOS differs
    b = identity.node_machine_id(tmp_path / "b")
    assert a != b
    for mid in (a, b):
        assert mid.startswith("m-") and len(mid) == 18 and int(mid[2:], 16) >= 0
        assert mid != _legacy_id()
    # stable across restarts
    _machine(monkeypatch, HW_A)
    assert identity.node_machine_id(tmp_path / "a") == a


def test_clones_enroll_as_two_nodes_and_both_keys_stay_valid(monkeypatch, tmp_path, st):
    rk = st.create_room_key(group_name="机房", max_uses=5, ttl_hours=1)["room_key"]
    nodes = []
    for tag, hw in (("a", HW_A), ("b", HW_B)):
        _machine(monkeypatch, hw)
        calls = []
        cfg = AgentConfig(tmp_path / tag)
        ag = NodeAgent(cfg, http=_store_http(st, calls), app_version="t")
        res = ag.enroll(room_key=rk, controller_url=CTL)
        assert res["status"] == "active"
        meta = calls[0]["body"]["meta"]
        assert meta["hw_fp"] and meta["id_source"] == "hw" and meta["id_scheme"] == 2
        blob = json.dumps(calls[0]["body"])
        assert hw["smbios_uuid"] not in blob and hw["disk_serial"] not in blob   # only hashes leave the PC
        nodes.append((ag.machine_id, cfg.node_id, cfg.node_key))
    assert nodes[0][0] != nodes[1][0] and nodes[0][1] != nodes[1][1]
    assert st.authenticate(nodes[0][2]) is not None and st.authenticate(nodes[1][2]) is not None


@pytest.mark.parametrize("raw,kind", [
    ("00000000-0000-0000-0000-000000000000", "smbios_uuid"),
    ("FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF", "smbios_uuid"),
    ("03000200-0400-0500-0006-000700080009", "smbios_uuid"),
    ("To be filled by O.E.M.", "smbios_uuid"),
    ("Default string", "disk_serial"),
    ("   ", "disk_serial"),
    ("0000_0000_0000_0000", "disk_serial"),
    ("00:00:00:00:00:00", "mac"),
    ("FF-FF-FF-FF-FF-FF", "mac"),
    ("01:00:5E:00:00:01", "mac"),
    ("not-a-mac", "mac"),
])
def test_bogus_hardware_values_are_ignored(raw, kind):
    assert identity.clean_hw_value(raw, kind) == ""


def test_real_hardware_values_are_normalised():
    assert identity.clean_hw_value("d8-bb-c1-11-22-33", "mac") == "D8BBC1112233"
    assert identity.clean_hw_value(" 4c4c4544-0042 ", "smbios_uuid") == "4C4C4544-0042"


def test_all_bogus_hardware_falls_back_to_random_not_to_the_clone_id(monkeypatch, tmp_path):
    bogus = {"smbios_uuid": "To be filled by O.E.M.", "disk_serial": "", "mac": "00:00:00:00:00:00"}
    _machine(monkeypatch, bogus)
    monkeypatch.setattr(identity.uuid, "getnode", lambda: 0x010000000000)  # random multicast → ignored
    identity.reset_hardware_cache()
    a = identity.resolve_machine_identity(tmp_path / "a")
    b = identity.resolve_machine_identity(tmp_path / "b")
    assert a["source"] == b["source"] == "random"
    assert a["machine_id"] != b["machine_id"] != _legacy_id()


# ── 缓存 + 指纹 ─────────────────────────────────────────────────────────────
def test_stale_cache_with_mismatched_fingerprint_is_regenerated(monkeypatch, tmp_path):
    """A 0.3.5 state dir copied with a cloned disk: the fingerprint belongs to the other PC."""
    sd = tmp_path / "fleet"
    _machine(monkeypatch, HW_A)
    original = identity.node_machine_id(sd)
    assert json.loads((sd / "machine_id.hw").read_text(encoding="utf-8"))["machine_id"] == original
    _machine(monkeypatch, HW_B)                        # same files, other hardware
    info = identity.resolve_machine_identity(sd)
    assert info["changed"] and info["reason"] == "hw_mismatch" and info["previous"] == original
    assert info["machine_id"] != original
    assert (sd / "machine_id").read_text(encoding="utf-8") == info["machine_id"]
    meta = identity.identity_meta(sd)
    assert meta["previous_machine_id"] == original and meta["origin"] == "generated"
    assert HW_B["disk_serial"] not in json.dumps(meta)  # hashes only
    assert identity.node_machine_id(sd) == info["machine_id"]  # stable afterwards


def test_non_colliding_legacy_cache_is_kept(monkeypatch, tmp_path):
    """CHINAMI-style 0.3.4 node upgraded in place: same id, fingerprint recorded for next time."""
    sd = tmp_path / "fleet"
    sd.mkdir()
    (sd / "machine_id").write_text("m-de4cfcb92e16d1b5", encoding="utf-8")
    _machine(monkeypatch, HW_A, host="CHINAMI-P5HO781")
    info = identity.resolve_machine_identity(sd)
    assert info["machine_id"] == "m-de4cfcb92e16d1b5" and not info["changed"]
    assert info["origin"] == "adopted_legacy"
    meta = identity.identity_meta(sd)
    assert meta["machine_id"] == "m-de4cfcb92e16d1b5" and meta["fp"]["smbios"]
    assert identity.node_machine_id(sd) == "m-de4cfcb92e16d1b5"


def test_matching_fingerprint_survives_a_nic_change(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    _machine(monkeypatch, HW_A)
    mid = identity.node_machine_id(sd)
    _machine(monkeypatch, dict(HW_A, mac="00-E0-4C-68-00-01"))   # USB NIC plugged in
    assert identity.node_machine_id(sd) == mid
    _machine(monkeypatch, {})                                     # probe failed this boot
    assert identity.node_machine_id(sd) == mid


def test_reinstall_rederives_legacy_cache_but_keeps_hardware_bound_cache(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    sd.mkdir()
    (sd / "machine_id").write_text(_legacy_id(), encoding="utf-8")
    _machine(monkeypatch, HW_A)
    assert identity.node_machine_id(sd) == _legacy_id()          # service run: adopted
    info = identity.resolve_machine_identity(sd, reinstall=True)  # installer run
    assert info["changed"] and info["reason"] == "reinstall_legacy" and info["previous"] == _legacy_id()
    again = identity.resolve_machine_identity(sd, reinstall=True)
    assert not again["changed"] and again["machine_id"] == info["machine_id"]


def test_regenerate_always_returns_a_different_id(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    _machine(monkeypatch, HW_A)
    mid = identity.node_machine_id(sd)
    new = identity.regenerate_machine_id(sd)
    assert new != mid and identity.node_machine_id(sd) == new


# ── Agent：换号后不再沿用旧 node_key ────────────────────────────────────────
def _enrolled_legacy_state(sd: Path, mid: str) -> None:
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "machine_id").write_text(mid, encoding="utf-8")
    (sd / "agent.json").write_text(json.dumps({
        "controller_url": CTL, "node_id": "n_shared", "node_key": "nk_" + "x" * 48, "heartbeat_sec": 30,
        "instances": [{"name": "player", "base_url": "http://127.0.0.1:18797", "auth_token": "",
                       "config_path": "", "domain": "player_care", "restart_cmd": ""}],
    }), encoding="utf-8")


def test_upgrade_in_place_keeps_enrollment_of_a_legacy_node(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    _enrolled_legacy_state(sd, "m-de4cfcb92e16d1b5")
    _machine(monkeypatch, HW_A, host="CHINAMI-P5HO781")
    ag = NodeAgent(AgentConfig(sd), http=lambda *a: (500, {}), app_version="t")
    assert ag.machine_id == "m-de4cfcb92e16d1b5" and not ag.identity_reset
    saved = json.loads((sd / "agent.json").read_text(encoding="utf-8"))
    assert saved["node_key"] and saved["node_id"] == "n_shared" and saved["machine_id"] == "m-de4cfcb92e16d1b5"


def test_reinstall_on_clone_drops_shared_key_and_requests_pending(monkeypatch, tmp_path, st):
    sd = tmp_path / "fleet"
    _enrolled_legacy_state(sd, _legacy_id())
    _machine(monkeypatch, HW_B)
    info = identity.resolve_machine_identity(sd, reinstall=True)
    calls = []
    cfg = AgentConfig(sd)
    ag = NodeAgent(cfg, http=_store_http(st, calls), app_version="t")
    assert ag.identity_reset and ag.machine_id == info["machine_id"] != _legacy_id()
    assert not cfg.node_key and not cfg.node_id
    assert cfg.data["previous_machine_id"] == _legacy_id() and cfg.data["machine_id"] == ag.machine_id
    assert cfg.instances and cfg.instances[0]["name"] == "player"   # local instances are kept
    res = ag.enroll("", controller_url=CTL)
    assert res["status"] == "pending" and res["pairing_code"]
    body = calls[-1]["body"]
    assert body["mode"] == "pending" and body["machine_id"] == ag.machine_id
    assert body["meta"]["previous_machine_id"] == _legacy_id()
    pend = st.list_pending()
    assert len(pend) == 1 and pend[0]["machine_id"] == ag.machine_id and not pend[0]["existing_node_id"]


def test_controller_conflict_regenerates_and_requests_pending_without_rotating(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    _machine(monkeypatch, HW_A)
    calls = []

    def http(method, url, body, headers, timeout):
        calls.append(json.loads(json.dumps(body)))
        if len(calls) == 1:
            return 409, {"detail": "machine_id_conflict"}
        return 200, {"ok": True, "status": "pending", "request_id": "req_test", "pairing_code": "ABCD-1234"}

    cfg = AgentConfig(sd)
    ag = NodeAgent(cfg, http=http, app_version="t")
    old = ag.machine_id
    res = ag.enroll(room_key="rk_" + "k" * 40, controller_url=CTL)
    assert res["status"] == "pending"
    assert calls[0]["room_key"] and calls[0]["machine_id"] == old
    assert "room_key" not in calls[1] and "code" not in calls[1] and calls[1]["mode"] == "pending"
    assert calls[1]["machine_id"] == ag.machine_id != old
    assert identity.identity_meta(sd)["reason"] == "conflict"


def test_heartbeat_conflict_drops_key_and_reenrolls(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    _machine(monkeypatch, HW_A)
    seen = []

    def http(method, url, body, headers, timeout):
        seen.append(url[len(CTL):])
        if url.endswith("/heartbeat"):
            return 409, {"error": "machine_id_conflict"}
        return 200, {"ok": True, "status": "pending", "request_id": "req_x", "pairing_code": "WXYZ-0001"}

    cfg = AgentConfig(sd)
    ag = NodeAgent(cfg, http=http, app_version="t")
    old = ag.machine_id
    cfg.data.update({"controller_url": CTL, "node_id": "n_1", "node_key": "nk_" + "y" * 48, "machine_id": old})
    cfg.save()
    ag.run_forever(sleep=lambda s: None)
    assert seen[0] == "/api/fleet/heartbeat" and "/api/fleet/enroll" in seen
    assert not cfg.node_key and cfg.data["pending_request_id"] == "req_x" and ag.machine_id != old


def test_cli_identity_reinstall_reports_change(monkeypatch, tmp_path):
    sd = tmp_path / "fleet"
    _enrolled_legacy_state(sd, _legacy_id())
    _machine(monkeypatch, HW_A)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = agent_mod.main(["--state-dir", str(sd), "identity", "--reinstall"])
    assert rc == 0
    out = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert out["changed"] and out["reason"] == "reinstall_legacy" and out["enrollment_reset"]
    assert out["short_id"] == out["machine_id"][:10] and out["host_name"] == HOST
    assert "node_key" not in json.dumps(out)


# ── 本机页 / 安装器 ─────────────────────────────────────────────────────────
def test_local_status_shows_hostname_and_short_machine_id(tmp_path):
    cfg = AgentConfig(tmp_path / "fleet")
    snap = build_local_status(cfg, machine_id="m-763f4c4341fa326a", host=HOST, probe=lambda u: False,
                              service_status_fn=lambda: {"installed": False})
    assert snap["machine_id_short"] == "m-763f4c43"
    assert snap["node_display"] == "PC-20240123AORY · m-763f4c43"
    assert snap["agent_version"] == "0.3.21"
    assert "本机标识" in PAGE_HTML and "node_display" in PAGE_HTML


def test_installer_rederives_identity_before_enroll():
    boot = (ENGINE / "fleet_agent" / "setup" / "bootstrap.ps1").read_text(encoding="utf-8")
    assert boot.isascii()
    i = boot.index("'identity', '--reinstall'")
    assert i < boot.index("@('--state-dir', $StateDir, 'status')")
    assert "[switch]$KeepIdentity" in boot
    iss = (ENGINE / "fleet_agent" / "setup" / "ChatXAgent.iss").read_text(encoding="utf-8")
    assert "/KEEPIDENTITY=" in iss and "-KeepIdentity" in iss
