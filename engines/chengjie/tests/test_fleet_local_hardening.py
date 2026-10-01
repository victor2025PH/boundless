"""Local hardening for the fleet agent (FLEET_ISSUES_FOR_DEVIN f, g1-g4).

1. Remote upgrade on an unlocked 0.2.x dir: agent.json is not deleted before the
   lock succeeds and is restored when it fails.
2. detect skips backup folders (``*_bak*``) and prefers the newest config.
3. Migration keeps each instance's auth_token (never restart_cmd / config_path).
4. One ``run`` per state dir; installer stops stray ``run`` processes.
5. An enrolled node never sends a pending request without a code / room key.
6. Live-stream ports (7910/7916/7920/8000/8080/8766/9000) are never probed or
   added by detect; a machine tagged live-stream is not probed at all.
7. ``remove-instance`` CLI.

Nothing here runs icacls or touches real ACLs: lock / ACL helpers are faked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.fleet import agent as agent_mod
from src.fleet import identity as ident
from src.fleet import service as svc
from src.fleet.agent import AgentConfig, NodeAgent, migrated_agent_data
from src.fleet import detect as detect_mod
from src.fleet.detect import (
    LIVE_STREAM_PORTS, detect_instances, find_chatx_configs, is_backup_dir_name, is_live_stream_host,
)
from src.fleet.identity import StateDirLockError

ENGINE = Path(__file__).resolve().parents[1]
TOKEN = "tok-" + "legacy-" + "0123456789abcdef"


def _legacy_agent_json(fleet: Path) -> dict:
    fleet.mkdir(parents=True, exist_ok=True)
    data = {
        "controller_url": "https://ctl.test/fleet",
        "node_id": "n_legacy",
        "node_key": "nk_" + "legacy",
        "heartbeat_sec": 30,
        "instances": [
            {"name": "chatx", "base_url": "http://127.0.0.1:18799", "auth_token": TOKEN,
             "config_path": "", "domain": "conversion", "restart_cmd": "calc"},
            {"name": "evil", "base_url": "http://10.0.0.5:18799", "auth_token": "x"},
        ],
    }
    (fleet / "agent.json").write_text(json.dumps(data), encoding="utf-8")
    return data


class _FakeLock:
    """Stands in for lock_state_dir / state_dir_is_locked without icacls."""

    def __init__(self, *, fail_after_rename: bool = False, fail_before_rename: bool = False,
                 lock_new_dir: bool = True) -> None:
        self.locked = set()
        self.fail_after_rename = fail_after_rename
        self.fail_before_rename = fail_before_rename
        self.lock_new_dir = lock_new_dir
        self.calls = 0

    def is_locked(self, path) -> bool:
        return str(Path(path)) in self.locked

    def lock(self, path) -> None:
        self.calls += 1
        path = Path(path)
        if self.is_locked(path):
            return
        if self.fail_before_rename:
            raise StateDirLockError("could not move the old state directory aside: [WinError 5]")
        if path.exists():
            os.rename(path, path.with_name(path.name + ".legacy-test"))
        path.mkdir()
        if self.lock_new_dir:
            self.locked.add(str(path))
        if self.fail_after_rename:
            raise StateDirLockError("icacls grant failed")


@pytest.fixture
def fake_lock(monkeypatch):
    def install(**kw):
        fl = _FakeLock(**kw)
        monkeypatch.setattr(agent_mod, "lock_state_dir", fl.lock)
        monkeypatch.setattr(agent_mod, "state_dir_is_locked", fl.is_locked)
        monkeypatch.setattr(agent_mod, "assign_owner_admins", lambda p: None)
        monkeypatch.setattr(agent_mod, "node_machine_id", lambda sd=None: "m-test")
        # Real ACL checks would call a Windows temp dir "unlocked" and delete the file.
        monkeypatch.setattr(agent_mod, "discard_untrusted_secret", lambda p, **k: False)
        return fl
    return install


# ── 1. identity survives a failed lock ──────────────────────────────────────
def test_load_does_not_delete_unlocked_agent_json(tmp_path, fake_lock, monkeypatch):
    fake_lock()
    fleet = tmp_path / "fleet"
    _legacy_agent_json(fleet)
    deleted = []
    monkeypatch.setattr(agent_mod, "discard_untrusted_secret", lambda p, **k: deleted.append(p) or True)
    cfg = AgentConfig(fleet)
    assert cfg.migrating and cfg.node_key == "nk_legacy"
    assert deleted == []
    assert (fleet / "agent.json").is_file()


def test_rename_failure_keeps_old_agent_json(tmp_path, fake_lock):
    fake_lock(fail_before_rename=True)
    fleet = tmp_path / "fleet"
    original = _legacy_agent_json(fleet)
    cfg = AgentConfig(fleet)
    with pytest.raises(StateDirLockError) as ei:
        NodeAgent(cfg, http=lambda *a: (200, {}))
    assert "old agent.json kept" in str(ei.value)
    on_disk = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert on_disk == original


def test_lock_failure_after_rename_restores_identity(tmp_path, fake_lock):
    # The directory was moved aside and a new (unlocked) one created, then icacls failed.
    fake_lock(fail_after_rename=True, lock_new_dir=False)
    fleet = tmp_path / "fleet"
    original = _legacy_agent_json(fleet)
    cfg = AgentConfig(fleet)
    with pytest.raises(StateDirLockError):
        NodeAgent(cfg, http=lambda *a: (200, {}))
    restored = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert restored == original                     # same trust level as before: unlocked
    assert (tmp_path / "fleet.legacy-test" / "agent.json").is_file()


def test_restore_into_a_locked_dir_writes_identity_only(tmp_path, fake_lock):
    fake_lock(fail_after_rename=True, lock_new_dir=True)
    fleet = tmp_path / "fleet"
    _legacy_agent_json(fleet)
    cfg = AgentConfig(fleet)
    with pytest.raises(StateDirLockError):
        NodeAgent(cfg, http=lambda *a: (200, {}))
    restored = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert restored["node_key"] == "nk_legacy" and restored["node_id"] == "n_legacy"
    assert "calc" not in json.dumps(restored)       # never promote the unlocked original


def test_successful_migration_keeps_identity_and_token(tmp_path, fake_lock):
    fl = fake_lock()
    fleet = tmp_path / "fleet"
    _legacy_agent_json(fleet)
    cfg = AgentConfig(fleet)
    NodeAgent(cfg, http=lambda *a: (200, {}))
    assert fl.is_locked(fleet) and not cfg.migrating
    saved = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert saved["node_key"] == "nk_legacy"
    assert [i["name"] for i in saved["instances"]] == ["chatx"]
    assert saved["instances"][0]["auth_token"] == TOKEN
    assert saved["instances"][0]["restart_cmd"] == "" and saved["instances"][0]["config_path"] == ""
    assert "calc" in (tmp_path / "fleet.legacy-test" / "agent.json").read_text(encoding="utf-8")


def test_run_service_lock_failure_exits_1_keeps_file_and_logs_outside(tmp_path, fake_lock, monkeypatch, capsys):
    fake_lock(fail_before_rename=True)

    class _G:
        released = False

        def release(self):
            _G.released = True

    monkeypatch.setattr(agent_mod, "acquire_single_instance", lambda sd: _G())
    fleet = tmp_path / "fleet"
    original = _legacy_agent_json(fleet)
    rc = agent_mod.main(["--state-dir", str(fleet), "run", "--service"])
    assert rc == 1 and _G.released
    assert json.loads((fleet / "agent.json").read_text(encoding="utf-8")) == original
    # The file log was not opened inside the unlocked dir (its handle blocks the rename).
    assert not (fleet / "logs").exists()
    note = (tmp_path / agent_mod.MIGRATE_LOG_NAME).read_text(encoding="utf-8")
    assert "state dir lock failed" in note and "nk_legacy" not in note
    err = capsys.readouterr().err
    assert "Could not lock the fleet state directory" in err and "nk_legacy" not in err


def test_rename_retries_then_raises_state_dir_lock_error(tmp_path, monkeypatch):
    target = tmp_path / "fleet"
    target.mkdir()
    tries = []

    def deny(src, dst):
        tries.append(dst)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(ident.os, "rename", deny)
    with pytest.raises(StateDirLockError, match="could not move the old state directory aside"):
        ident._rename_legacy_fleet(target, retries=3, sleep=lambda s: None)
    assert len(tries) == 3 and target.is_dir()


# ── 2. detect skips backups ─────────────────────────────────────────────────
def _chatx_cfg(root: Path, folder: str, port: int = 18799) -> Path:
    p = root / folder / "data" / "config" / "config.local.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"domain: conversion\nweb_admin:\n  port: {port}\n  auth_token: t\n", encoding="utf-8")
    return p


def test_backup_folder_names():
    for name in ("telegram-ai-desktop_bak20260811_0443", "app.bak", "Backup", "backups",
                 "fleet.legacy-0123", "ChatX - 副本", "ChatX_old", "ChatX 备份"):
        assert is_backup_dir_name(name), name
    for name in ("telegram-ai-desktop", "config", "data", "bakery", "oldschool", "holder", "chatx-player"):
        assert not is_backup_dir_name(name), name


def test_detect_skips_bak_dir_and_prefers_live_config(tmp_path):
    bak = _chatx_cfg(tmp_path, "telegram-ai-desktop_bak20260811_0443")
    live = _chatx_cfg(tmp_path, "telegram-ai-desktop")
    old = time.time() - 3600
    os.utime(live, (old, old))                     # even when the backup is newer
    assert find_chatx_configs([tmp_path]) == [live]
    found = detect_instances(probe=lambda url: True, search=True, roots=[tmp_path], avatar_url="")
    assert [i["config_path"] for i in found] == [str(live)]
    assert str(bak) not in json.dumps(found)


def test_detect_prefers_the_newest_config_for_one_port(tmp_path):
    a = _chatx_cfg(tmp_path, "chatx-a")
    b = _chatx_cfg(tmp_path, "chatx-b")
    old = time.time() - 3600
    os.utime(a, (old, old))
    found = detect_instances(probe=lambda url: True, search=True, roots=[tmp_path], avatar_url="")
    assert len(found) == 1 and found[0]["config_path"] == str(b)


# ── 3. migration keeps the instance token ───────────────────────────────────
def test_migrated_data_keeps_token_drops_restart_cmd_and_config_path():
    src = {"node_id": "n1", "node_key": "k", "instances": [
        {"name": "chatx", "base_url": "http://127.0.0.1:18799/", "auth_token": TOKEN,
         "config_path": "C:/Users/x/cfg.yaml", "domain": "conversion", "restart_cmd": "calc", "role": ""},
        {"name": "far", "base_url": "http://192.168.1.2:18799", "auth_token": "x"},
        {"name": "chatx", "base_url": "http://127.0.0.1:18797", "auth_token": "dup"},
        {"base_url": "http://127.0.0.1:1"},
        "junk",
    ]}
    out = migrated_agent_data("https://ctl.test/fleet", src)
    assert out["instances"] == [{
        "name": "chatx", "base_url": "http://127.0.0.1:18799", "auth_token": TOKEN,
        "config_path": "", "domain": "conversion", "restart_cmd": "", "role": "",
    }]
    assert migrated_agent_data("u", {"instances": "nope"})["instances"] == []


def test_detect_after_migration_keeps_token_and_fills_config_path(tmp_path, fake_lock, monkeypatch):
    fake_lock()
    fleet = tmp_path / "fleet"
    _legacy_agent_json(fleet)
    agent = NodeAgent(AgentConfig(fleet), http=lambda *a: (200, {}))
    monkeypatch.setattr(agent_mod, "detect_instances", lambda **k: [
        {"name": "chatx", "base_url": "http://127.0.0.1:18799", "domain": "conversion", "role": "",
         "config_path": "C:/live/config.local.yaml", "up": "1"},
        {"name": "avatarhub", "base_url": "http://127.0.0.1:9100", "domain": "avatar_hub", "role": "health",
         "config_path": "", "up": "1"},
    ])
    agent._detect_and_add()
    saved = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    by = {i["name"]: i for i in saved["instances"]}
    assert by["chatx"]["auth_token"] == TOKEN
    assert by["chatx"]["config_path"] == "C:/live/config.local.yaml"
    assert by["avatarhub"]["role"] == "health"
    assert agent_mod._instance_token(by["chatx"]) == TOKEN


# ── 4. single instance ──────────────────────────────────────────────────────
def _probe_guard(state_dir: Path) -> str:
    code = ("import sys; from src.fleet.service import acquire_single_instance as a; "
            "g = a(sys.argv[1], wait_sec=0); print('busy' if g is None else 'got')")
    out = subprocess.run([sys.executable, "-c", code, str(state_dir)], cwd=str(ENGINE),
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def test_single_instance_guard_blocks_a_second_process(tmp_path):
    state = tmp_path / "fleet"
    guard = svc.acquire_single_instance(state, wait_sec=0)
    assert guard is not None
    try:
        assert _probe_guard(state) == "busy"
        assert _probe_guard(tmp_path / "other") == "got"   # scoped per state dir
    finally:
        guard.release()
    assert _probe_guard(state) == "got"
    assert not state.exists()                               # never creates a handle inside fleet


def test_guard_waits_then_gives_up(monkeypatch, tmp_path):
    now = [0.0]
    naps = []
    monkeypatch.setattr(svc, "_try_mutex", lambda name: None)
    monkeypatch.setattr(svc, "_try_flock", lambda sd, name: None)
    got = svc.acquire_single_instance(tmp_path, wait_sec=3, sleep=lambda s: (naps.append(s), now.__setitem__(0, now[0] + s)),
                                      clock=lambda: now[0])
    assert got is None and len(naps) == 3


def test_second_run_exits_with_already_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent_mod, "acquire_single_instance", lambda sd: None)
    built = []
    monkeypatch.setattr(agent_mod, "NodeAgent", lambda *a, **k: built.append(1))
    rc = agent_mod.main(["--state-dir", str(tmp_path / "fleet"), "run", "--service"])
    assert rc == agent_mod.EXIT_ALREADY_RUNNING == 4
    assert built == []
    assert "already active" in capsys.readouterr().err


def test_installers_stop_stray_runs_and_read_stdout_only():
    boot = (ENGINE / "fleet_agent/setup/bootstrap.ps1").read_text(encoding="utf-8")
    assert "function NativeOut" in boot and "2>$null" in boot
    assert "$stRaw = NativeOut $target @('--state-dir', $StateDir, 'status')" in boot
    assert "$out = NativeOut $target $enrollArgs" in boot
    assert boot.index("Stop-StrayAgent $target") < boot.index("'install-service')")
    assert "/T" not in boot
    ps1 = (ENGINE / "fleet_agent/Install-ChatXAgent.ps1").read_text(encoding="utf-8")
    assert "$stRaw = NativeOut $target @('--state-dir', $stateDir, 'status')" in ps1
    assert all(ord(ch) < 128 for ch in boot)          # bootstrap.ps1 is ASCII only


# ── 5. no redundant pending request ─────────────────────────────────────────
def test_enrolled_node_does_not_request_pending(tmp_path, fake_lock):
    fl = fake_lock()
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    fl.locked.add(str(fleet))
    cfg = AgentConfig(fleet)
    cfg.data.update({"controller_url": "https://ctl.test/fleet", "node_id": "n1", "node_key": "nk_1"})
    cfg.save()
    calls = []

    def http(method, url, body, headers, timeout):
        calls.append(url)
        return 200, {"ok": True, "request_id": "req_dup", "pairing_code": "K2LYNF"}

    agent = NodeAgent(AgentConfig(fleet), http=http)
    res = agent.enroll("", controller_url="https://ctl.test/fleet")
    assert res == {"node_id": "n1", "status": "active", "already_enrolled": True}
    assert calls == []
    saved = json.loads((fleet / "agent.json").read_text(encoding="utf-8"))
    assert "pending_request_id" not in saved and saved["node_key"] == "nk_1"
    # An explicit code still re-enrolls.
    agent.enroll("ABCD-EFGH", controller_url="https://ctl.test/fleet")
    assert calls and calls[0].endswith("/api/fleet/enroll")


# ── 6. live-stream ports and live-stream machines ───────────────────────────
LIVE = (7910, 7916, 7920, 8000, 8080, 8766, 9000)


def test_live_port_set_is_exact():
    assert LIVE_STREAM_PORTS == frozenset(LIVE)
    assert detect_mod.AVATAR_URL == ""


def test_detect_never_probes_or_adds_live_ports(tmp_path, monkeypatch):
    probed = []
    cfgs = []
    for port in LIVE:
        p = tmp_path / f"inst{port}" / "config" / "config.local.yaml"
        p.parent.mkdir(parents=True)
        p.write_text(f"web_admin:\n  port: {port}\n  auth_token: t\n", encoding="utf-8")
        cfgs.append(p)

    def probe(url):
        probed.append(url)
        return True

    for avatar in ("http://127.0.0.1:9000", "http://localhost:8080", ""):
        found = detect_instances(probe=probe, config_paths=cfgs, search=False, avatar_url=avatar, live_stream=False)
        assert all(detect_mod.url_port(i["base_url"]) not in LIVE for i in found), found
        assert not any(i["name"] == "avatarhub" for i in found)
    assert probed and all(detect_mod.url_port(u) not in LIVE for u in probed), probed

    # the real socket probe refuses live ports without connecting
    def boom(*a, **k):
        raise AssertionError("connected to a live port")

    monkeypatch.setattr(detect_mod.urllib.request, "urlopen", boom)
    for port in LIVE:
        assert detect_mod.probe_loopback(f"http://127.0.0.1:{port}/health") is False


def test_live_stream_machine_is_not_probed_at_all(tmp_path, monkeypatch):
    cfg = _chatx_cfg(tmp_path, "telegram-ai-desktop")
    touched = []
    monkeypatch.delenv(detect_mod.ENV_LIVE_STREAM, raising=False)
    state = tmp_path / "ChatX" / "fleet"
    state.mkdir(parents=True)
    assert is_live_stream_host(state) is False
    (state.parent / detect_mod.LIVE_STREAM_FLAG).write_text("176\n", encoding="utf-8")
    assert is_live_stream_host(state) is True
    assert detect_instances(probe=lambda u: touched.append(u) or True, config_paths=[cfg], search=True,
                            roots=[tmp_path], live_stream=True) == []
    assert touched == []
    monkeypatch.setenv(detect_mod.ENV_LIVE_STREAM, "1")
    assert is_live_stream_host(tmp_path / "elsewhere") is True
    assert detect_instances(probe=lambda u: touched.append(u) or True, config_paths=[cfg]) == []
    assert touched == []


def test_agent_detect_on_live_machine_adds_nothing(tmp_path, fake_lock, monkeypatch):
    fake_lock()
    state = tmp_path / "ChatX" / "fleet"
    state.mkdir(parents=True)
    (state.parent / detect_mod.LIVE_STREAM_FLAG).write_text("", encoding="utf-8")
    seen = {}

    def fake_detect(**kw):
        seen.update(kw)
        return [] if kw.get("live_stream") else [{"name": "chatx", "base_url": "http://127.0.0.1:18799"}]

    monkeypatch.setattr(agent_mod, "detect_instances", fake_detect)
    agent = NodeAgent(AgentConfig(state), http=lambda *a: (200, {}))
    assert agent._detect_and_add() == [] and seen["live_stream"] is True
    assert agent.cfg.instances == []


def test_add_instance_refuses_live_ports_unless_explicit(tmp_path, fake_lock):
    fake_lock()
    cfg = AgentConfig(tmp_path / "fleet")
    for port in LIVE:
        with pytest.raises(agent_mod.AgentError, match="live-stream port"):
            cfg.add_instance("x", f"http://127.0.0.1:{port}")
    cfg.add_instance("hub", "http://127.0.0.1:9000", domain="avatar_hub", allow_live_port=True)
    assert cfg.instances[0]["allow_live_port"] is True


def test_existing_live_port_instance_is_not_probed(tmp_path, fake_lock):
    fake_lock()
    cfg = AgentConfig(tmp_path / "fleet")
    # what an older detect left on 176
    cfg.data["instances"] = [{"name": "avatarhub", "base_url": "http://127.0.0.1:9000", "domain": "avatar_hub",
                              "role": "health"}]
    calls = []

    def http(method, url, body, headers, timeout):
        calls.append(url)
        return 200, {"ok": True}

    hb = NodeAgent(cfg, http=http).build_heartbeat()
    assert not any(":9000" in u for u in calls)
    assert hb["instances"][0]["up"] is False
    assert any("live-stream port 9000 not probed" in e for e in hb["errors"])


def test_migration_drops_live_port_instances():
    out = migrated_agent_data("u", {"instances": [
        {"name": "avatarhub", "base_url": "http://127.0.0.1:9000", "allow_live_port": True},
        {"name": "chatx", "base_url": "http://127.0.0.1:18799", "auth_token": "t"}]})
    assert [i["name"] for i in out["instances"]] == ["chatx"]


# ── 7. remove-instance ──────────────────────────────────────────────────────
def test_remove_instance_cli(tmp_path, fake_lock, capsys):
    fake_lock()
    state = tmp_path / "fleet"
    cfg = AgentConfig(state)
    cfg.data["instances"] = [
        {"name": "chatx", "base_url": "http://127.0.0.1:18799", "auth_token": TOKEN},
        {"name": "avatarhub", "base_url": "http://127.0.0.1:9000", "role": "health"},
    ]
    cfg.save()
    assert agent_mod.main(["--state-dir", str(state), "remove-instance", "avatarhub"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == {"ok": True, "removed": "avatarhub", "instances": ["chatx"]}
    saved = json.loads((state / "agent.json").read_text(encoding="utf-8"))
    assert [i["name"] for i in saved["instances"]] == ["chatx"]
    assert saved["instances"][0]["auth_token"] == TOKEN          # others untouched
    assert agent_mod.main(["--state-dir", str(state), "remove-instance", "nope"]) == 1
    captured = capsys.readouterr()
    assert "no instance named 'nope'" in captured.err and TOKEN not in captured.out + captured.err


def test_add_instance_cli_live_port_needs_flag(tmp_path, fake_lock, capsys):
    fake_lock()
    state = tmp_path / "fleet"
    assert agent_mod.main(["--state-dir", str(state), "add-instance", "hub=http://127.0.0.1:9000"]) == 1
    assert "live-stream port" in capsys.readouterr().err
    assert agent_mod.main(["--state-dir", str(state), "add-instance", "hub=http://127.0.0.1:9000",
                           "--allow-live-port"]) == 0
