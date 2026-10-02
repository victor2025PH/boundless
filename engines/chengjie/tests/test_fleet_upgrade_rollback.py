"""Upgrade auto-rollback (FLEET_ISSUES c / f item 3).

The swap script waits for the new version's first heartbeat stamp. Without it
the old binary (.bak) and the pre-upgrade state (legacy dir / agent.json) come
back and the service is started again.

The scripts are really executed in a temp dir. Task names are unique bogus
names, so schtasks /Run, /End and /Delete touch nothing real; no process
matches the temp exe path.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from src.fleet import updater as upd

OLD, NEW = b"MZ old agent 0.2.1", b"MZ new agent 0.3.3"


def test_health_timeout_is_clamped_and_can_be_disabled():
    assert upd.health_timeout_of({}) == upd.HEALTH_TIMEOUT_SEC == 300
    assert upd.health_timeout_of({"health_timeout_sec": 5}) == 60
    assert upd.health_timeout_of({"health_timeout_sec": 99999}) == 1800
    assert upd.health_timeout_of({"health_timeout_sec": "bad"}) == 300
    assert upd.health_timeout_of({"health_timeout_sec": 0}) == 0


def test_script_without_state_dir_is_the_old_plain_swap():
    body, _ = upd.build_swap_script(Path(r"C:\a\chatx-agent.exe"), Path(r"C:\b\new.exe"), 1, windows=True)
    assert "last_heartbeat.json" not in body and "rollback" not in body


def test_windows_script_shape():
    body, suf = upd.build_swap_script(Path(r"C:\Program Files\ChatX Agent\chatx-agent.exe"),
                                      Path(r"C:\ProgramData\ChatX\fleet\updates\chatx-agent-0.3.3.exe"), 42,
                                      windows=True, state_dir=Path(r"C:\ProgramData\ChatX\fleet"), version="0.3.3")
    assert suf == ".ps1"
    # snapshot and legacy list are taken before the old process is even gone
    assert body.index("ReadAllBytes($aj)") < body.index("Wait-Process -Id 42") < body.index("Move-Item")
    assert body.index("$t0 =") < body.index('schtasks /Run /TN "ChatX Fleet Agent"')
    assert "chatx-agent.exe.failed-0.3.3" in body and "$timeout = 300" in body
    assert "Copy-Item -LiteralPath $bak -Destination $cur" in body
    assert body.rstrip().endswith('schtasks /Delete /TN "ChatX Fleet Agent Upgrade" /F 2>$null | Out-Null')
    assert "WriteAllBytes($aj, $snap)" in body and "Out-File" not in body


def test_apply_upgrade_passes_state_dir_and_timeout(tmp_path):
    import hashlib

    spawned = []

    def fetch(url, dest):
        dest.write_bytes(NEW)

    st, res, detail = upd.apply_upgrade(
        {"url": "https://x/chatx-agent.exe", "sha256": hashlib.sha256(NEW).hexdigest(), "version": "0.3.3",
         "health_timeout_sec": 120},
        tmp_path, current_exe=tmp_path / "bin" / "chatx-agent.exe", fetch=fetch, spawn=spawned.append,
        frozen=True, pid=99)
    assert detail == "swap_scheduled" and res["rollback_after_sec"] == 120
    text = Path(spawned[0][-1]).read_text(encoding="utf-8")
    assert str(tmp_path / "last_heartbeat.json") in text or "last_heartbeat.json" in text
    assert ("$timeout = 120" in text) or ("+120))" in text)


# ── real runs ───────────────────────────────────────────────────────────────
def _layout(tmp_path: Path):
    bin_dir = tmp_path / "Program Files" / "ChatX Agent"
    bin_dir.mkdir(parents=True)
    cur = bin_dir / ("chatx-agent.exe" if os.name == "nt" else "chatx-agent")
    cur.write_bytes(OLD)
    state = tmp_path / "ProgramData" / "ChatX" / "fleet"
    (state / "updates").mkdir(parents=True)
    new = state / "updates" / "chatx-agent-0.3.3.exe"
    new.write_bytes(NEW)
    original = {"node_id": "n_old", "node_key": "nk_old", "instances": [{"name": "chatx", "restart_cmd": "x"}]}
    (state / "agent.json").write_text(json.dumps(original), encoding="utf-8")
    return cur, new, state, original


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _start(cur, new, state, *, timeout=8):
    bogus = "ChatX Fleet Agent TEST-" + uuid.uuid4().hex[:8]
    body, suf = upd.build_swap_script(cur, new, _dead_pid(), task_name=bogus, state_dir=state, version="0.3.3",
                                      health_timeout=timeout, poll_sec=1, swap_task_name=bogus + " Upgrade")
    # Same place as production (<state>/updates/swap.ps1): the running script must not
    # keep a handle that blocks the new version from renaming the state dir.
    script = state / "updates" / ("swap" + suf)
    script.write_text(body, encoding="utf-8")
    env = dict(os.environ)
    if suf == ".sh":
        stub = state.parent / "stubbin"
        stub.mkdir()
        sc = stub / "systemctl"
        sc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        sc.chmod(sc.stat().st_mode | stat.S_IEXEC)
        env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    cmd = upd.swap_command(script)
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _wait_for(pred, secs=30.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _can_run() -> bool:
    if os.name == "nt":
        return shutil.which("powershell") is not None
    return shutil.which("sh") is not None


needs_shell = pytest.mark.skipif(not _can_run(), reason="needs powershell (Windows) or sh")


@needs_shell
def test_no_heartbeat_rolls_back_binary_and_restores_migrated_state(tmp_path):
    cur, new, state, original = _layout(tmp_path)
    proc = _start(cur, new, state)
    assert _wait_for(lambda: cur.is_file() and cur.read_bytes() == NEW), "swap did not happen"
    if os.name == "nt":
        # the new version "migrated": old dir renamed aside, new dir without agent.json (issue f)
        assert _wait_for(lambda: not new.exists())
        legacy = state.with_name("fleet.legacy-" + uuid.uuid4().hex)
        _wait_for(lambda: _try_rename(state, legacy), 10)
        (state).mkdir()
    else:
        (state / "agent.json").unlink()
    out, _ = proc.communicate(timeout=90)
    assert cur.read_bytes() == OLD, out
    assert cur.with_name(cur.name + ".failed-0.3.3").read_bytes() == NEW
    assert json.loads((state / "agent.json").read_text(encoding="utf-8")) == original
    log = (state.parent / upd.UPGRADE_LOG_NAME).read_text(encoding="utf-8", errors="replace")
    assert "rollback" in log and "nk_old" not in log
    if os.name == "nt":
        assert list(state.parent.glob("fleet.failed-*")), "new dir should be kept aside"
        assert not list(state.parent.glob("fleet.legacy-*"))


def _try_rename(src: Path, dst: Path) -> bool:
    try:
        os.rename(src, dst)
        return True
    except OSError:
        return False


@needs_shell
def test_heartbeat_after_swap_keeps_new_binary(tmp_path):
    cur, new, state, original = _layout(tmp_path)
    proc = _start(cur, new, state, timeout=20)
    assert _wait_for(lambda: cur.is_file() and cur.read_bytes() == NEW)
    time.sleep(1.5)
    (state / "last_heartbeat.json").write_text(json.dumps({"at": round(time.time() + 2, 3)}), encoding="utf-8")
    out, _ = proc.communicate(timeout=90)
    assert cur.read_bytes() == NEW, out
    assert not cur.with_name(cur.name + ".failed-0.3.3").exists()
    assert json.loads((state / "agent.json").read_text(encoding="utf-8")) == original
    log = (state.parent / upd.UPGRADE_LOG_NAME).read_text(encoding="utf-8", errors="replace")
    assert "upgrade ok" in log and "rollback" not in log
