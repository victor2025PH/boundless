"""PR #38 审查：离线告警多进程只推一次（文件锁）；换文件脚本路径含单引号也能跑。"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

import pytest

from src.fleet import offline_alert as oa
from src.fleet import updater as upd

NOW = 1_800_000_000.0

_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[2])
from src.fleet.offline_alert import try_lock
fh = try_lock(sys.argv[1])
print("LOCKED" if fh else "BUSY", flush=True)
time.sleep(float(sys.argv[3]))
"""


def test_try_lock_is_exclusive_across_processes(tmp_path):
    lock = str(tmp_path / "watch.lock")
    eng = str(Path(__file__).resolve().parents[1])
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, lock, eng, "30"], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "LOCKED"
        assert oa.try_lock(lock) is None                            # 另一个进程拿着锁
    finally:
        p.kill()
        p.wait()
    fh = oa.try_lock(lock)                                          # 持锁进程退出 → 接手
    assert fh is not None
    fh.close()


def test_two_watchers_only_one_alerts(tmp_path):
    lock = str(tmp_path / "watch.lock")
    gone = time.time() - 3600
    hits = []
    nodes = lambda: [{"node_id": "z", "state": "offline", "status": "active", "last_seen": gone, "label": "Z"}]  # noqa: E731
    a1 = oa.OfflineAlerter(10, notify=lambda t, n, d: hits.append(("w1", n)))
    a2 = oa.OfflineAlerter(10, notify=lambda t, n, d: hits.append(("w2", n)))
    oa.start_watch(nodes, a1, interval_sec=0.01, lock_path=lock)
    oa.start_watch(nodes, a2, interval_sec=0.01, lock_path=lock)
    deadline = time.time() + 5
    while not hits and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    assert len(hits) == 1                                          # 同一节点只告警一次


def test_default_lock_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(oa.ENV_ALERT_LOCK, str(tmp_path / "x.lock"))
    assert oa._default_lock_path() == str(tmp_path / "x.lock")


# ── build_swap_script：路径含单引号 / 弯引号 ──────────────────────────────────
def test_ps_literal_escapes_straight_and_curly_quotes():
    assert upd._ps_lit("O'Brien") == "O''Brien"
    assert upd._ps_lit("a\u2019b") == "a\u2019\u2019b"


def test_swap_script_never_inlines_raw_paths():
    cur, new = Path(r"C:\Users\O'Brien\chatx-agent.exe"), Path(r"C:\T\it's\new.exe")
    body, _ = upd.build_swap_script(cur, new, 1, windows=True)
    assert "$cur = 'C:\\Users\\O''Brien\\chatx-agent.exe'" in body
    assert "O'Brien" not in body and "it's" not in body
    body, _ = upd.build_swap_script(PurePosixPath("/opt/o'b/chatx-agent"), PurePosixPath("/tmp/n'x"), 1, windows=False)
    assert "'/opt/o'\\''b/chatx-agent'" in body and "'/tmp/n'\\''x'" in body


def _can_run() -> bool:
    return shutil.which("powershell" if os.name == "nt" else "sh") is not None


@pytest.mark.skipif(not _can_run(), reason="no shell")
def test_swap_script_really_runs_with_quote_in_path(tmp_path):
    d = tmp_path / "O'Brien \u2019x\u2019"
    d.mkdir()
    cur = d / ("chatx-agent.exe" if os.name == "nt" else "chatx-agent")
    new = d / "new'one.exe"
    cur.write_bytes(b"old")
    new.write_bytes(b"new")
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    bogus = "ChatX Fleet Agent TEST-" + uuid.uuid4().hex[:8]
    body, suf = upd.build_swap_script(cur, new, p.pid, task_name=bogus, swap_task_name=bogus + " Upgrade")
    script = tmp_path / ("swap" + suf)
    script.write_text(body, encoding="utf-8-sig" if suf == ".ps1" else "utf-8")
    env = dict(os.environ)
    if suf == ".sh":
        stub = tmp_path / "stubbin"
        stub.mkdir()
        sc = stub / "systemctl"
        sc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        sc.chmod(sc.stat().st_mode | stat.S_IEXEC)
        env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    subprocess.run(upd.swap_command(script), env=env, capture_output=True, timeout=120)
    assert cur.read_bytes() == b"new" and not new.exists()
    assert (d / (cur.name + ".bak")).read_bytes() == b"old"
