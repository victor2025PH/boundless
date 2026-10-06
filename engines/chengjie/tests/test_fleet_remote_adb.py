"""Remote installer upgrade and enable_phone_adb. No device, no real installer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, List

import pytest

from src.fleet import admin as admin_mod
from src.fleet import adb_bundle
from src.fleet import updater as upd
from src.fleet.agent import AgentConfig, NodeAgent
from src.fleet.phones import PROTECTED_ADDRESSES, PROTECTED_SERIALS
from src.fleet.protocol import (
    LEGACY_ALLOWED_KINDS, REMOTE_PHONE_KINDS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_ENABLE_PHONE_ADB, TASK_KINDS, TASK_UPGRADE,
)

SHA = "ab" * 32
HTTPS = "https://bd2026.cc/downloads/fleet/ChatXAgentSetup-0.3.8.exe"
PROTECTED = "3B1F4KE5MS140P4X"


def _blob() -> bytes:
    return b"MZ fake ChatXAgentSetup"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload(**extra: Any) -> dict:
    body = {"setup_url": HTTPS, "setup_sha256": SHA, "version": "0.3.8"}
    body.update(extra)
    return body


def test_setup_sha_mismatch_is_refused_and_not_spawned(tmp_path):
    spawned: List[List[str]] = []
    fetched: List[str] = []

    def fetch(url, dest):
        fetched.append(url)
        dest.write_bytes(b"not the pinned bytes")

    st, res, detail = upd.apply_upgrade(
        _payload(), tmp_path, fetch=fetch, spawn=spawned.append, frozen=True, windows=True, live_stream=False)
    assert (st, detail) == (STATUS_REJECTED, "sha256_mismatch")
    assert "sha256 mismatch" in res["error"]
    assert spawned == [] and fetched == [HTTPS]
    dest = tmp_path / "updates" / "ChatXAgentSetup-0.3.8.exe"
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()
    assert not (tmp_path / "updates" / "install-setup.ps1").exists()


def test_setup_live_host_refuses_before_download(tmp_path, monkeypatch):
    spawned: List[Any] = []
    fetched: List[str] = []

    def fetch(url, dest):
        fetched.append(url)

    st, _, detail = upd.apply_upgrade(
        _payload(), tmp_path, fetch=fetch, spawn=spawned.append, frozen=True, windows=True, live_stream=True)
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert fetched == [] and spawned == []

    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: True)
    st2, _, detail2 = upd.apply_upgrade(
        _payload(), tmp_path, fetch=fetch, spawn=spawned.append, frozen=False, windows=True)
    assert (st2, detail2) == (STATUS_REJECTED, "live_stream_host")
    assert fetched == [] and spawned == []


def test_setup_apply_verifies_then_schedules_silent_install(tmp_path):
    data = _blob()
    sha = _sha(data)
    spawned: List[List[str]] = []

    def fetch(url, dest):
        assert url == HTTPS
        dest.write_bytes(data)

    st, res, detail = upd.apply_upgrade(
        _payload(setup_sha256=sha, manage_adb_server=True), tmp_path, fetch=fetch, spawn=spawned.append,
        frozen=True, windows=True, live_stream=False, pid=42)
    assert (st, detail) == (STATUS_DONE, "setup_scheduled")
    staged = Path(res["staged"])
    assert staged == tmp_path / "updates" / "ChatXAgentSetup-0.3.8.exe"
    assert staged.read_bytes() == data
    assert res["exit"] is True and res["setup"] is True and res["manage_adb_server"] is True
    assert len(spawned) == 1
    script = Path(spawned[0][-1])
    text = script.read_text(encoding="utf-8")
    assert text.index("live-stream.flag") < text.index("Get-FileHash") < text.index("Start-Process")
    for flag in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/MANAGEADBSERVER=1"):
        assert flag in text
    assert sha in text
    assert "CHATX_FLEET_LIVE_STREAM" in text
    assert spawned[0][:5] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]


def test_setup_manage_flag_is_only_json_true(tmp_path):
    data = _blob()
    sha = _sha(data)
    spawned: List[List[str]] = []

    def fetch(url, dest):
        dest.write_bytes(data)

    st, res, detail = upd.apply_upgrade(
        _payload(setup_sha256=sha, manage_adb_server="true"), tmp_path, fetch=fetch, spawn=spawned.append,
        frozen=True, windows=True, live_stream=False)
    assert (st, detail) == (STATUS_DONE, "setup_scheduled")
    assert res["manage_adb_server"] is False
    text = Path(spawned[0][-1]).read_text(encoding="utf-8")
    assert "/VERYSILENT" in text and "/MANAGEADBSERVER=1" not in text


@pytest.mark.parametrize("payload,detail", [
    ({"setup_sha256": SHA}, "setup_url_and_sha256_required"),
    ({"setup_url": HTTPS, "setup_sha256": "abc"}, "setup_sha256_invalid"),
    ({"setup_url": "http://bd2026.cc/setup.exe", "setup_sha256": SHA}, "setup_url_must_be_https"),
    ({"setup_url": "https://user:pw@bd2026.cc/setup.exe", "setup_sha256": SHA}, "setup_url_must_be_https"),
])
def test_setup_rejects_bad_url_or_sha_before_fetch(tmp_path, payload, detail):
    fetched: List[str] = []
    st, _, got = upd.apply_upgrade(
        payload, tmp_path, fetch=lambda url, dest: fetched.append(url), spawn=lambda cmd: None,
        frozen=True, windows=True, live_stream=False)
    assert (st, got) == (STATUS_REJECTED, detail)
    assert fetched == []


def test_setup_refuses_non_windows_and_keeps_exe_upgrade(tmp_path):
    fetched: List[str] = []
    st, _, detail = upd.apply_upgrade(
        _payload(), tmp_path, fetch=lambda url, dest: fetched.append(url), frozen=True, windows=False,
        live_stream=False)
    assert (st, detail) == (STATUS_REJECTED, "not_windows") and fetched == []

    data = b"MZ agent"
    sha = _sha(data)
    spawned: List[List[str]] = []

    def fetch(url, dest):
        dest.write_bytes(data)

    st, res, detail = upd.apply_upgrade(
        {"url": "https://bd2026.cc/downloads/fleet/chatx-agent-0.3.8.exe", "sha256": sha, "version": "0.3.8",
         "setup_url": "", "setup_sha256": ""},
        tmp_path, current_exe=tmp_path / "chatx-agent.exe", fetch=fetch, spawn=spawned.append, frozen=True, pid=7)
    assert (st, detail) == (STATUS_DONE, "swap_scheduled")
    assert res["exit"] is True and "setup" not in res
    assert "swap" in Path(spawned[0][-1]).name


def test_enable_phone_adb_starts_bundled_server_and_is_idempotent(tmp_path):
    adb = tmp_path / "ChatX" / "platform-tools" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.write_bytes(b"MZ")
    decoy = tmp_path / "platform-tools" / "adb.exe"
    decoy.parent.mkdir()
    decoy.write_bytes(b"MZ")
    calls: List[List[str]] = []
    persisted: List[bool] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return type("P", (), {"returncode": 0})()

    def exists(path):
        return path in {str(adb), str(decoy)}

    kwargs = dict(run=run, exists=exists, candidates=(str(adb),), live_stream=False)
    st, res, detail = adb_bundle.enable_phone_adb(
        tmp_path, persist=lambda: persisted.append(True), already=False, **kwargs)
    assert (st, detail) == (STATUS_DONE, "adb_enabled")
    assert res["idempotent"] is False and res["server_started"] is True and res["adb"] == str(adb)
    st2, res2, detail2 = adb_bundle.enable_phone_adb(
        tmp_path, persist=lambda: persisted.append(True), already=True, **kwargs)
    assert (st2, detail2) == (STATUS_DONE, "adb_enabled")
    assert res2["idempotent"] is True and persisted == [True, True]
    assert calls == [[str(adb), "start-server"], [str(adb), "start-server"]]
    for cmd in calls:
        assert cmd[-1] == "start-server" and len(cmd) == 2
        blob = " ".join(cmd)
        assert PROTECTED not in blob and "192.168.0.148" not in blob
    assert PROTECTED_SERIALS == (PROTECTED,)
    assert "192.168.0.148" in PROTECTED_ADDRESSES


def test_enable_phone_adb_refuses_live_host_and_missing_bundle(tmp_path, monkeypatch):
    persisted: List[bool] = []
    calls: List[List[str]] = []
    adb = str(tmp_path / "adb.exe")

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return type("P", (), {"returncode": 0})()

    st, _, detail = adb_bundle.enable_phone_adb(
        tmp_path, persist=lambda: persisted.append(True), run=run, exists=lambda p: True,
        candidates=(adb,), live_stream=True)
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert persisted == [] and calls == []

    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: True)
    st, _, detail = adb_bundle.enable_phone_adb(
        tmp_path, persist=lambda: persisted.append(True), run=run, exists=lambda p: True, candidates=(adb,))
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert persisted == [] and calls == []

    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: False)
    st, _, detail = adb_bundle.enable_phone_adb(
        tmp_path, persist=lambda: persisted.append(True), run=run, exists=lambda p: False, candidates=(adb,))
    assert (st, detail) == (STATUS_REJECTED, "bundled_adb_missing")
    assert persisted == [] and calls == []

    st, res, detail = adb_bundle.enable_phone_adb(
        tmp_path, persist=lambda: persisted.append(True), run=lambda cmd, **k: type("P", (), {"returncode": 1})(),
        exists=lambda p: True, candidates=(adb,), live_stream=False)
    assert (st, detail) == (STATUS_FAILED, "adb_server_start_failed")
    assert persisted == [True] and res["adb_manage_server"] is True and res["server_started"] is False


def test_protocol_keeps_enable_phone_adb_off_the_phone_remote_gate():
    assert TASK_ENABLE_PHONE_ADB in TASK_KINDS
    assert TASK_ENABLE_PHONE_ADB not in REMOTE_PHONE_KINDS
    assert TASK_ENABLE_PHONE_ADB not in LEGACY_ALLOWED_KINDS
    assert TASK_KINDS[0] == "ping"


def test_agent_setup_on_live_host_does_not_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: True)
    cfg = AgentConfig(tmp_path)
    ag = NodeAgent(cfg, http=lambda *a: (200, {}))
    st, _, detail = ag.execute({"task_id": "t", "kind": TASK_UPGRADE, "payload": _payload()})
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert ag.exit_requested is False


def test_agent_enable_phone_adb_persists_and_is_idempotent(tmp_path, monkeypatch):
    adb = tmp_path / "platform-tools" / "adb.exe"
    adb.parent.mkdir()
    adb.write_bytes(b"MZ")
    calls: List[List[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: False)
    monkeypatch.setattr(adb_bundle, "bundled_adb_candidates", lambda: (str(adb),))
    monkeypatch.setattr(adb_bundle.subprocess, "run", run)
    cfg = AgentConfig(tmp_path)
    cfg.data["phones_exclude"] = ["ABC123"]
    cfg.save()
    ag = NodeAgent(cfg, http=lambda *a: (200, {}))
    task = {"task_id": "t", "kind": TASK_ENABLE_PHONE_ADB,
            "payload": {"serial": PROTECTED, "address": "192.168.0.148"}}
    st, res, detail = ag.execute(task)
    assert (st, detail) == (STATUS_DONE, "adb_enabled")
    assert res["idempotent"] is False and ag.exit_requested is False
    assert ag.phones.manage_server is True and ag.phone_ops.manage_server is True
    disk = json.loads((tmp_path / "agent.json").read_text(encoding="utf-8"))
    assert disk["adb_manage_server"] is True and disk["phones_exclude"] == ["ABC123"]
    st2, res2, detail2 = ag.execute(task)
    assert (st2, detail2) == (STATUS_DONE, "adb_enabled") and res2["idempotent"] is True
    assert calls == [[str(adb), "start-server"], [str(adb), "start-server"]]
    for cmd in calls:
        assert PROTECTED not in " ".join(cmd) and "192.168.0.148" not in " ".join(cmd)


def test_agent_enable_phone_adb_refuses_live_host(tmp_path, monkeypatch):
    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: True)
    cfg = AgentConfig(tmp_path)
    ag = NodeAgent(cfg, http=lambda *a: (200, {}))
    st, _, detail = ag.execute({"task_id": "t", "kind": TASK_ENABLE_PHONE_ADB, "payload": {}})
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert ag.exit_requested is False and ag.phones.manage_server is False
    raw = (tmp_path / "agent.json").read_text(encoding="utf-8") if (tmp_path / "agent.json").is_file() else ""
    assert "adb_manage_server" not in raw


def test_admin_setup_payload_and_enable_command():
    calls: List[Any] = []

    def http(method, url, body):
        calls.append(body)
        return {"ok": True, "task_id": "t"}

    adm = admin_mod.Admin("https://bd2026.cc/fleet", "T", http=http)
    manifest = {"url": "https://bd2026.cc/downloads/fleet/chatx-agent-0.3.8.exe", "sha256": SHA,
                "version": "0.3.8", "setup_url": HTTPS, "setup_sha256": "cd" * 32}
    adm.upgrade(manifest, node_ids=["n1"])
    assert calls[-1]["kind"] == TASK_UPGRADE
    assert calls[-1]["payload"] == {"url": manifest["url"], "sha256": SHA, "version": "0.3.8"}
    assert "setup_url" not in calls[-1]["payload"]
    adm.upgrade(manifest, node_ids=["n1"], setup=True, manage_adb_server=True)
    assert calls[-1]["payload"] == {
        "setup_url": HTTPS, "setup_sha256": "cd" * 32, "version": "0.3.8", "manage_adb_server": True}
    assert "url" not in calls[-1]["payload"]
    with pytest.raises(SystemExit):
        adm.upgrade({"version": "0.3.8", "url": manifest["url"], "sha256": SHA}, node_ids=["n1"], setup=True)
    adm.enable_phone_adb("n-room")
    assert calls[-1]["kind"] == TASK_ENABLE_PHONE_ADB
    assert calls[-1]["payload"] == {} and calls[-1]["ttl_sec"] == 600


def test_admin_manage_adb_flag_requires_setup(tmp_path, monkeypatch):
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"version": "0.3.8", "url": "https://x/a.exe", "sha256": SHA}), encoding="utf-8")
    rc = admin_mod.main(["--controller", "https://c", "--token", "t", "upgrade", "--manifest", str(mf),
                         "--node", "n1", "--manage-adb-server", "--yes"])
    assert rc == 2
