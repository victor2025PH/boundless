"""Fleet agent 0.3.6: read-only phone inventory in the heartbeat.

Covers the adb ``devices -l`` parser, the collector's safety gates (no server ->
no command; version mismatch -> no command), timeout / failure fallback with the
60 s cache, the exclude list (3B1F live phone), the controller whitelist, and
the node list exposing ``phones`` / ``phones_error`` (old nodes -> empty).
No real adb is run: every subprocess call goes to a recording fake.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from src.fleet import phones as ph
from src.fleet.protocol import HEARTBEAT_KEYS, PROTO_VERSION, sanitize_heartbeat
from src.fleet.store import FleetStore

ADB = r"C:\platform-tools\adb.exe"
# conftest swaps the module attribute for a no-server stub; the socket tests need the real probe.
_REAL_SERVER_VERSION = ph.adb_server_version

SAMPLE = """List of devices attached
E6FYAAAA1111 device usb:1-4 product:gale_global model:23106RN0DA device:gale transport_id:3
HQ79BBBB2222 offline usb:1-5 transport_id:4
Z95XCCCC3333 unauthorized usb:1-6 transport_id:5
192.168.0.50:5555 device product:CPH2653 model:CPH2653 device:OP5D0DL1 transport_id:7
emulator-5554 device product:sdk model:sdk_gphone64 device:emu64 transport_id:9

"""


class FakeRun:
    def __init__(self, devices_out=SAMPLE, version_out="Android Debug Bridge version 1.0.41\nVersion 35.0.2\n",
                 exc=None, rc=0):
        self.calls = []
        self.devices_out, self.version_out, self.exc, self.rc = devices_out, version_out, exc, rc

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if cmd[1:] == ["version"]:
            return subprocess.CompletedProcess(cmd, 0, self.version_out.encode(), b"")
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.rc, self.devices_out.encode(), b"")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def collector(run, *, server=41, clock=None, exclude=None, enabled=True):
    return ph.PhoneCollector(run=run, clock=clock or Clock(), exclude=exclude, enabled=enabled,
                             server_version=lambda port: server, locate=lambda cfg: ADB)


# ── parser ──────────────────────────────────────────────────────────────────
def test_parse_device_offline_unauthorized_and_models():
    out = ph.parse_adb_devices(SAMPLE)
    assert [p["serial"] for p in out] == ["E6FYAAAA1111", "HQ79BBBB2222", "Z95XCCCC3333",
                                         "192.168.0.50:5555", "emulator-5554"]
    by = {p["serial"]: p for p in out}
    assert by["E6FYAAAA1111"] == {"serial": "E6FYAAAA1111", "state": "device", "model": "23106RN0DA",
                                  "transport": "usb"}
    assert by["HQ79BBBB2222"]["state"] == "offline" and by["HQ79BBBB2222"]["model"] == ""
    assert by["Z95XCCCC3333"]["state"] == "unauthorized"
    assert by["192.168.0.50:5555"]["transport"] == "tcp" and by["192.168.0.50:5555"]["model"] == "CPH2653"
    assert by["emulator-5554"]["transport"] == "emulator"
    assert all(set(p) == set(ph.PHONE_FIELDS) for p in out)


def test_parse_plain_devices_line_without_l_attributes():
    assert ph.parse_adb_devices("List of devices attached\nABCDEF\tdevice\n") == [
        {"serial": "ABCDEF", "state": "device", "model": "", "transport": "unknown"}]


@pytest.mark.parametrize("text", ["", "List of devices attached\n\n",
                                  "* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
                                  "List of devices attached\n"])
def test_parse_empty_list(text):
    assert ph.parse_adb_devices(text) == []


def test_parse_no_permissions_and_garbage():
    text = ("List of devices attached\n"
            "0123456789 no permissions (missing udev rules?); see [http://developer.android.com/x] usb:1-1\n"
            "adb: error: something\nlonelytoken\nWEIRD123 sideways\n")
    out = ph.parse_adb_devices(text)
    assert out == [{"serial": "0123456789", "state": "no_permissions", "model": "", "transport": "usb"},
                   {"serial": "WEIRD123", "state": "unknown", "model": "", "transport": "unknown"}]


# ── collector ───────────────────────────────────────────────────────────────
def test_collect_runs_only_version_and_devices_l_without_shell():
    run = FakeRun()
    phones, err = collector(run).collect()
    assert err == "" and len(phones) == 5
    cmds = [c for c, _ in run.calls]
    assert cmds == [[ADB, "version"], [ADB, "devices", "-l"]]
    for _, kw in run.calls:
        assert kw["shell"] is False and kw["timeout"] == 5
    # client version is cached: the next heartbeat runs only devices -l
    collector_obj = collector(run)
    collector_obj.collect()
    collector_obj.collect()
    assert [c for c, _ in run.calls][-1] == [ADB, "devices", "-l"]


def test_collect_timeout_reports_error_and_keeps_heartbeat_shape():
    run = FakeRun(exc=subprocess.TimeoutExpired(cmd="adb", timeout=5))
    phones, err = collector(run).collect()
    assert phones == [] and err == "adb_timeout"


def test_collect_failure_reuses_success_within_60s_then_drops():
    clock = Clock(1000.0)
    run = FakeRun()
    c = collector(run, clock=clock)
    ok, err = c.collect()
    assert err == "" and len(ok) == 5
    run.exc = subprocess.TimeoutExpired(cmd="adb", timeout=5)
    clock.t = 1059.0
    cached, err = c.collect()
    assert err == "adb_timeout" and cached == ok
    clock.t = 1061.0
    gone, err = c.collect()
    assert err == "adb_timeout" and gone == []


def test_collect_nonzero_exit():
    phones, err = collector(FakeRun(rc=1)).collect()
    assert phones == [] and err == "adb_exit_1"


def test_no_server_means_no_adb_command():
    run = FakeRun()
    phones, err = collector(run, server=None).collect()
    assert (phones, err) == ([], "adb_server_not_running")
    assert run.calls == []


def test_version_mismatch_never_runs_devices():
    run = FakeRun(version_out="Android Debug Bridge version 1.0.39\n")
    phones, err = collector(run, server=41).collect()
    assert phones == [] and err.startswith("adb_version_mismatch")
    assert [c for c, _ in run.calls] == [[ADB, "version"]]


def test_adb_not_found():
    c = ph.PhoneCollector(run=FakeRun(), server_version=lambda p: 41, locate=lambda cfg: "")
    assert c.collect() == ([], "adb_not_found")


def test_disabled():
    run = FakeRun()
    assert collector(run, enabled=False).collect() == ([], "disabled")
    assert run.calls == []


def test_only_allowed_adb_arguments():
    c = collector(FakeRun())
    with pytest.raises(RuntimeError):
        c._adb(ADB, ("shell", "input", "tap", "1", "1"))


# ── exclude list ────────────────────────────────────────────────────────────
def test_default_exclude_list_is_empty_but_live_phone_is_protected():
    assert ph.DEFAULT_PHONES_EXCLUDE == ()
    assert ph.normalize_excludes(None) == ("3B1F4KE5MS140P4X",)
    out = SAMPLE.replace("Z95XCCCC3333 unauthorized usb:1-6 transport_id:5",
                         "3B1F4KE5MS140P4X device usb:1-7 model:CPH2653 transport_id:6")
    phones, err = collector(FakeRun(devices_out=out)).collect()
    assert err == "" and "3B1F4KE5MS140P4X" not in [p["serial"] for p in phones]


def test_configured_exclude_exact_and_prefix():
    phones, _ = collector(FakeRun(), exclude=["e6fyaaaa1111", "192.168.0.*"]).collect()
    assert [p["serial"] for p in phones] == ["HQ79BBBB2222", "Z95XCCCC3333", "emulator-5554"]


def test_model_exclude_and_mdns_protected_serial():
    out = SAMPLE + ("adb-3B1F4KE5MS140P4X-AbCdEf._adb-tls-connect._tcp device product:x model:CPH2653 transport_id:11\n")
    phones, _ = collector(FakeRun(devices_out=out), exclude=["model:cph2653"]).collect()
    serials = [p["serial"] for p in phones]
    assert "192.168.0.50:5555" not in serials                     # model:CPH2653
    assert not any("3B1F4KE5MS140P4X" in s for s in serials)       # protected inside mDNS name
    assert "E6FYAAAA1111" in serials
    phones, _ = collector(FakeRun(devices_out=out)).collect()
    serials = [p["serial"] for p in phones]
    assert "192.168.0.50:5555" in serials and not any("3B1F" in s for s in serials)


def test_bare_star_excludes_nothing():
    phones, _ = collector(FakeRun(), exclude=["*"]).collect()
    assert len(phones) == 5


# ── socket version probe (read-only host:version) ───────────────────────────
def test_adb_server_version_no_listener_returns_none():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _REAL_SERVER_VERSION(port, timeout=0.3) is None


def test_adb_server_version_reads_okay_reply():
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def serve():
        conn, _ = srv.accept()
        got["req"] = conn.recv(64)
        conn.sendall(b"OKAY00040029")
        conn.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    assert _REAL_SERVER_VERSION(srv.getsockname()[1], timeout=2) == 41
    th.join(2)
    srv.close()
    assert got["req"] == b"000chost:version"


# ── controller ──────────────────────────────────────────────────────────────
def test_heartbeat_whitelist_keeps_and_trims_phones():
    assert "phones" in HEARTBEAT_KEYS and "phones_error" in HEARTBEAT_KEYS
    hb = sanitize_heartbeat({
        "agent_version": "0.3.6",
        "phones": [{"serial": "E6FY1", "state": "device", "model": "M", "transport": "usb", "chat": "x"},
                   {"serial": "E6FY1", "state": "device"}, "junk", {"serial": ""},
                   {"serial": "Q1", "state": "WEIRD", "model": "m" * 200}],
        "phones_error": 123,
        "evil": "dropped",
    })
    assert "evil" not in hb and hb["phones_error"] == ""
    assert hb["phones"] == [{"serial": "E6FY1", "state": "device", "model": "M", "transport": "usb"},
                            {"serial": "Q1", "state": "unknown", "model": "m" * 64, "transport": "unknown"}]


def _store_with_node(tmp_path):
    st = FleetStore(tmp_path / "fleet.db")
    code = st.create_enroll_code(label="T", group_name="g")["code"]
    res = st.enroll(code=code, machine_id="m-test-0001", host_name="HOST-A", proto_version=PROTO_VERSION)
    assert res["ok"], res
    return st, res["node_id"]


def test_store_saves_phones_and_node_list_returns_them(tmp_path):
    st, nid = _store_with_node(tmp_path)
    st.heartbeat(nid, {"agent_version": "0.3.6",
                       "phones": [{"serial": "E6FY1", "state": "device", "model": "M", "transport": "usb"}],
                       "phones_error": ""})
    node = [n for n in st.list_nodes() if n["node_id"] == nid][0]
    assert node["phones"] == [{"serial": "E6FY1", "state": "device", "model": "M", "transport": "usb"}]
    assert node["phones_error"] == ""
    assert node["last_heartbeat"]["phones"] == node["phones"]
    assert st.heartbeat_history(nid)[0]["phones"] == 1


def test_old_node_without_phones_is_empty_list(tmp_path):
    st, nid = _store_with_node(tmp_path)
    st.heartbeat(nid, {"agent_version": "0.3.5", "accounts": {"total": 0}})
    node = st.get_node(nid)
    assert node["phones"] == [] and node["phones_error"] == ""
    st2, nid2 = _store_with_node(tmp_path / "b")
    assert st2.get_node(nid2)["phones"] == []      # never heartbeated


# ── agent wiring ────────────────────────────────────────────────────────────
def test_agent_heartbeat_carries_phones(monkeypatch, tmp_path):
    from src.fleet import agent as agent_mod

    assert agent_mod.AGENT_VERSION == "0.3.6"
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data.update({"controller_url": "http://127.0.0.1:1", "instances": []})
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    ag.phones = collector(FakeRun())
    hb = ag.build_heartbeat()
    assert hb["phones_error"] == "" and len(hb["phones"]) == 5
    assert set(sanitize_heartbeat(hb)) >= {"phones", "phones_error"}


def test_agent_heartbeat_without_adb_server_still_builds(tmp_path):
    from src.fleet import agent as agent_mod

    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.data.update({"controller_url": "http://127.0.0.1:1", "instances": []})
    ag = agent_mod.NodeAgent(cfg, http=lambda *a, **k: (200, {}), app_version="t")
    hb = ag.build_heartbeat()   # conftest: no adb server answers -> no command runs
    assert hb["phones"] == [] and hb["phones_error"] in ("adb_server_not_running", "adb_not_found")


# ── source guard ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
_CHANGED = ["src/fleet/phones.py", "src/fleet/agent.py", "src/fleet/protocol.py", "src/fleet/store.py"]


@pytest.mark.parametrize("rel", _CHANGED)
def test_source_has_no_forbidden_adb_verbs(rel):
    text = (_ROOT / rel).read_text(encoding="utf-8")
    for bad in ("kill-server", "start-server", "tcpip"):
        assert bad not in text, (rel, bad)


def test_phones_module_never_mentions_port_9000():
    # agent.py already names 9000 in its live-port block list; the new module must not.
    assert "9000" not in (_ROOT / "src/fleet/phones.py").read_text(encoding="utf-8")


def test_phones_module_runs_only_devices_l_and_version():
    text = (_ROOT / "src/fleet/phones.py").read_text(encoding="utf-8")
    assert 'ADB_COLLECT_ARGS = ("devices", "-l")' in text
    assert re.findall(r'"(reboot|install|input|root|remount|disconnect|connect)"', text) == []
    assert "shell=True" not in text and '"shell": False' in text