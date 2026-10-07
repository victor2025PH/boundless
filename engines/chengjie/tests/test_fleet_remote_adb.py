"""Remote installer upgrade and enable_phone_adb. No device, no real installer."""
from __future__ import annotations

import hashlib
import json
import os
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


def _schedule(tmp_path, payload, **kw):
    data = _blob()
    sha = _sha(data)
    spawned: List[List[str]] = []

    def fetch(url, dest):
        dest.write_bytes(data)

    body = dict(payload)
    body["setup_sha256"] = sha
    st, res, detail = upd.apply_upgrade(
        body, tmp_path, fetch=fetch, spawn=spawned.append, frozen=True, windows=True,
        live_stream=False, pid=42, **kw)
    assert (st, detail) == (STATUS_DONE, "setup_scheduled")
    text = Path(spawned[0][-1]).read_text(encoding="utf-8")
    return res, text


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
    assert "/KEEPIDENTITY=1" not in text and res["keep_identity"] is False
    assert spawned[0][:5] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    assert res["rollback_after_sec"] == upd.HEALTH_TIMEOUT_SEC
    _assert_bundled_adb_stop(text)
    _assert_setup_failsafe(text)
    _assert_setup_watchdog(text, upd.HEALTH_TIMEOUT_SEC)


def _assert_bundled_adb_stop(text: str) -> None:
    """The generated script stops only {app}\\platform-tools\\adb.exe, after the live-stream exit."""
    assert text.isascii()
    assert text.index("live-stream.flag") < text.index("kill-server") < text.index("Start-Process")
    assert text.rindex("exit 2") < text.index("kill-server")
    assert "& $adb kill-server" in text
    assert "$_.ExecutablePath -eq $adb" in text
    assert "\\platform-tools\\adb.exe" in text
    for bad in ("taskkill", "/IM", "Stop-Process -Name", "Get-Process", "$_.Name",
                "Name -eq", "-eq 'adb.exe'", '-eq "adb.exe"'):
        assert bad not in text, bad


def _assert_setup_failsafe(text: str) -> None:
    assert "$task = 'ChatX Fleet Agent'" in text
    exit_at = text.index("ExitCode -ne 0")
    window = text[exit_at:exit_at + 160]
    assert "Relaunch-Agent 'setup exit non-zero'" in window
    assert 'schtasks /Run /TN "$task"' in text
    assert "Restore-AgentJson" in text and "ReadAllBytes" in text and "WriteAllBytes" in text


def _assert_setup_watchdog(text: str, timeout: int) -> None:
    assert f"$timeout = {timeout}" in text
    assert text.index("$t0 = ") < text.index("Start-Job") < text.index("Start-Process")
    assert "last_heartbeat.json" in text
    assert "setup watchdog: no heartbeat; agent task relaunched" in text
    assert "Relaunch-Agent 'no heartbeat'" in text
    assert text.count("{") == text.count("}")


def test_setup_script_stops_bundled_adb_by_full_path_and_watches_health(tmp_path):
    room = tmp_path / "room"
    room.mkdir()
    app = Path("C:/Program Files/ChatX Agent/chatx-agent.exe")
    res, text = _schedule(room, _payload(health_timeout_sec=120), current_exe=app)
    assert res["rollback_after_sec"] == 120
    assert r"$adb = 'C:\Program Files\ChatX Agent\platform-tools\adb.exe'" in text
    _assert_bundled_adb_stop(text)
    _assert_setup_failsafe(text)
    _assert_setup_watchdog(text, 120)

    off = tmp_path / "timeout-off"
    off.mkdir()
    res0, text0 = _schedule(off, _payload(health_timeout_sec=0))
    assert res0["rollback_after_sec"] == 0
    assert "last_heartbeat.json" not in text0 and "Start-Job" not in text0
    assert "kill-server" in text0 and "Relaunch-Agent 'setup exit non-zero'" in text0
    assert text0.rindex("exit 2") < text0.index("kill-server")


def test_installer_stops_bundled_adb_before_copy_and_relaunches_on_failure():
    iss = (Path(__file__).resolve().parents[1] / "fleet_agent/setup/ChatXAgent.iss").read_text(encoding="utf-8")
    assert iss.startswith("\ufeff")
    wild = next(ln for ln in iss.splitlines() if ln.startswith('Source: "{#PlatformToolsDir}\\*"'))
    assert "ignoreversion" in wild and "restartreplace" in wild
    assert 'Excludes: "adb.exe,AdbWinApi.dll,AdbWinUsbApi.dll"' in wild
    adb_line = next(ln for ln in iss.splitlines() if ln.startswith('Source: "{#PlatformToolsDir}\\adb.exe"'))
    assert "restartreplace" in adb_line and "skipifsourcedoesntexist" in adb_line
    assert "ignoreversion" not in adb_line
    for name in ("AdbWinApi.dll", "AdbWinUsbApi.dll"):
        line = next(ln for ln in iss.splitlines() if ln.startswith(f'Source: "{{#PlatformToolsDir}}\\{name}"'))
        assert "restartreplace" in line and "skipifsourcedoesntexist" in line and "ignoreversion" not in line

    live = iss.split("function IsLiveStreamHost")[1].split("procedure StopBundledAdb")[0]
    stop = iss.split("procedure StopBundledAdb")[1].split("function PrepareToInstall")[0]
    relaunch = iss.split("procedure RelaunchOurAgent")[1].split("procedure DeinitializeSetup")[0]
    deinit = iss.split("procedure DeinitializeSetup")[1].split("procedure FailInstall")[0]
    for block in (live, stop, relaunch, deinit):
        block.encode("ascii")
    assert "CHATX_FLEET_LIVE_STREAM" in live and "live-stream.flag" in live
    assert stop.index("IsLiveStreamHost()") < stop.index("kill-server")
    assert "platform-tools\\adb.exe" in stop and "$_.ExecutablePath -eq $adb" in stop
    for bad in ("taskkill", "/IM", "Get-ChildItem", "Get-Process", "$_.Name", "Stop-Process -Name"):
        assert bad not in stop, bad
    prep = iss.split("function PrepareToInstall")[1].split("function RunIcacls")[0]
    assert prep.index("StopOurAgent()") < prep.index("AgentWasStopped := True") < prep.index("StopBundledAdb()")
    assert '/Run /TN "ChatX Fleet Agent"' in relaunch
    assert "AgentWasStopped" in relaunch and "SetupFinishedOk" in deinit
    fail = iss.split("procedure FailInstall")[1].split("procedure StopOurAgent")[0]
    assert fail.index("RelaunchOurAgent()") < fail.index("WizardSilent") < fail.index("ExitProcess(1)")
    step = iss.split("procedure CurStepChanged")[1]
    assert step.index("ResultCode <> 0") < step.index("RelaunchOurAgent()") < step.index("WizardSilent")
    assert step.index("WizardSilent") < step.index("ExitProcess(1)")
    assert "SetupFinishedOk := True" in step
    uninst = iss.split("procedure CurUninstallStepChanged")[1].split("procedure InitializeWizard")[0]
    assert "StopBundledAdb" not in uninst and "RelaunchOurAgent" not in uninst


def test_enrolled_remote_setup_keeps_identity_fresh_enroll_does_not(tmp_path):
    enrolled = tmp_path / "enrolled"
    enrolled.mkdir()
    (enrolled / "agent.json").write_text(
        json.dumps({"node_id": "n_room", "node_key": "nk-keep"}), encoding="utf-8")
    res, text = _schedule(enrolled, _payload(manage_adb_server=True))
    assert res["keep_identity"] is True and res["manage_adb_server"] is True
    assert "/KEEPIDENTITY=1" in text and "/MANAGEADBSERVER=1" in text
    switches = upd.bootstrap_switches_for_setup(upd.setup_installer_flags(manage_adb_server=True, keep_identity=True))
    assert switches == ["-KeepIdentity", "-ManageAdbServer"]
    iss = (Path(__file__).resolve().parents[1] / "fleet_agent/setup/ChatXAgent.iss").read_text(encoding="utf-8")
    step = iss.split("procedure CurStepChanged")[1]
    assert step.index("CmdParam('/KEEPIDENTITY=') = '1'") < step.index("params := params + ' -KeepIdentity'")
    assert step.index("params := params + ' -KeepIdentity'") < step.index("Exec(")
    boot = (Path(__file__).resolve().parents[1] / "fleet_agent/setup/bootstrap.ps1").read_text(encoding="utf-8")
    assert "if (-not $KeepIdentity)" in boot and "'identity', '--reinstall'" in boot

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "agent.json").write_text(json.dumps({"node_id": "", "node_key": ""}), encoding="utf-8")
    res2, text2 = _schedule(fresh, _payload(manage_adb_server=True))
    assert res2["keep_identity"] is False
    assert "/KEEPIDENTITY=1" not in text2 and "/MANAGEADBSERVER=1" in text2
    assert upd.bootstrap_switches_for_setup(
        upd.setup_installer_flags(manage_adb_server=True, keep_identity=False)) == ["-ManageAdbServer"]

    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "agent.json").write_text(json.dumps({"pending_request_id": "req"}), encoding="utf-8")
    _, text3 = _schedule(pending, _payload())
    assert "/KEEPIDENTITY=1" not in text3


def test_enrolled_setup_on_live_host_is_still_refused(tmp_path):
    enrolled = tmp_path / "enrolled"
    enrolled.mkdir()
    (enrolled / "agent.json").write_text(json.dumps({"node_key": "nk", "node_id": "n_1"}), encoding="utf-8")
    fetched: List[str] = []
    spawned: List[Any] = []
    st, _, detail = upd.apply_upgrade(
        _payload(), enrolled, fetch=lambda url, dest: fetched.append(url), spawn=spawned.append,
        frozen=True, windows=True, live_stream=True)
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert fetched == [] and spawned == []
    assert not (enrolled / "updates").exists()


def test_nginx_serves_versioned_manifest_with_exe_and_sha256():
    nginx = (Path(__file__).resolve().parents[1] / "deploy/fleet/nginx-fleet.conf").read_text(encoding="utf-8")
    block = nginx.split('location ~ "^/downloads/fleet/', 1)[1].split("}", 1)[0]
    assert "chatx-agent-[0-9][0-9.]*\\.exe(\\.sha256)?" in block
    assert "ChatXAgentSetup-[0-9][0-9.]*\\.exe(\\.sha256)?" in block
    assert "manifest-[0-9][0-9.]*\\.json" in block
    assert "root /var/www/dl-mirror;" in nginx
    assert "manifest.json" not in block


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
    cfg = AgentConfig(tmp_path / "fleet")
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
    # Not tmp_path itself: conftest keeps files open there, so Windows cannot
    # rename that directory (WinError 5). A child directory can be locked in place.
    fleet = tmp_path / "fleet"
    cfg = AgentConfig(fleet)
    cfg.data["phones_exclude"] = ["ABC123"]
    cfg.save()
    ag = NodeAgent(cfg, http=lambda *a: (200, {}))
    task = {"task_id": "t", "kind": TASK_ENABLE_PHONE_ADB,
            "payload": {"serial": PROTECTED, "address": "192.168.0.148"}}
    st, res, detail = ag.execute(task)
    assert (st, detail) == (STATUS_DONE, "adb_enabled")
    assert res["idempotent"] is False and ag.exit_requested is False
    assert ag.phones.manage_server is True and ag.phone_ops.manage_server is True
    disk = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert disk["adb_manage_server"] is True and disk["phones_exclude"] == ["ABC123"]
    st2, res2, detail2 = ag.execute(task)
    assert (st2, detail2) == (STATUS_DONE, "adb_enabled") and res2["idempotent"] is True
    assert calls == [[str(adb), "start-server"], [str(adb), "start-server"]]
    for cmd in calls:
        assert PROTECTED not in " ".join(cmd) and "192.168.0.148" not in " ".join(cmd)


def test_agent_enable_phone_adb_refuses_live_host(tmp_path, monkeypatch):
    monkeypatch.setattr("src.fleet.detect.is_live_stream_host", lambda state_dir=None: True)
    fleet = tmp_path / "fleet"
    cfg = AgentConfig(fleet)
    ag = NodeAgent(cfg, http=lambda *a: (200, {}))
    st, _, detail = ag.execute({"task_id": "t", "kind": TASK_ENABLE_PHONE_ADB, "payload": {}})
    assert (st, detail) == (STATUS_REJECTED, "live_stream_host")
    assert ag.exit_requested is False and ag.phones.manage_server is False
    raw = (fleet / "agent.json").read_text(encoding="utf-8") if (fleet / "agent.json").is_file() else ""
    assert "adb_manage_server" not in raw


def test_lock_falls_back_to_in_place_when_rename_is_denied(tmp_path, monkeypatch):
    from src.fleet import identity as ident

    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "agent.json").write_text('{"node_key": "nk"}', encoding="utf-8")

    def denied(path, **kwargs):
        raise ident.LegacyRenameError("could not move the old state directory aside: [WinError 5] Access is denied")

    monkeypatch.setattr(ident, "_rename_legacy_fleet", denied)
    ident.lock_state_dir(fleet)
    assert fleet.is_dir() and (fleet / "agent.json").is_file()
    assert list(tmp_path.glob("fleet.legacy-*")) == []
    tmp = fleet / "agent.json.tmp"
    tmp.write_text('{"node_key": "nk", "adb_manage_server": true}', encoding="utf-8")
    os.replace(tmp, fleet / "agent.json")
    saved = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert saved["adb_manage_server"] is True and saved["node_key"] == "nk"


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
