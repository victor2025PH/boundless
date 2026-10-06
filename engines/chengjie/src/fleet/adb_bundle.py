"""Bundled Android platform-tools for phone-room nodes (agent 0.3.8).

The live-stream machine already has its own adb server and must be left alone.
This module only knows how to find the adb.exe shipped with the agent, and how
to bring that copy's server up when ``adb_manage_server`` is on and nothing is
answering. It never stops a server, never changes the port, and never runs on
a live-stream host.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("fleet.adb_bundle")

START_TIMEOUT_SEC = 30
_ADB_NAMES = ("adb.exe", "adb")
_AGENT_EXE_NAMES = ("chatx-agent.exe", "chatx-agent")

RunFn = Callable[..., Any]


def bundled_adb_candidates() -> Tuple[str, ...]:
    """Windows locations the installer fills. Empty on any other platform.

    Order: directory of chatx-agent.exe, ``%ProgramData%\\ChatX\\platform-tools``,
    ``%ProgramFiles%\\ChatX Agent\\platform-tools``. A ``python.exe`` next to a
    source checkout is not treated as an install directory.
    """
    if not sys.platform.startswith("win"):
        return ()
    out = []
    exe = Path(sys.executable)
    if exe.name.lower() in _AGENT_EXE_NAMES:
        out.append(str(exe.parent / "platform-tools" / "adb.exe"))
    program_data = os.environ.get("PROGRAMDATA") or os.environ.get("ProgramData") or r"C:\ProgramData"
    out.append(str(Path(program_data) / "ChatX" / "platform-tools" / "adb.exe"))
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    out.append(str(Path(program_files) / "ChatX Agent" / "platform-tools" / "adb.exe"))
    seen = set()
    uniq = []
    for c in out:
        key = os.path.normcase(os.path.normpath(c))
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return tuple(uniq)


def is_bundled_adb(path: str, candidates: Optional[Sequence[str]] = None) -> bool:
    """True when ``path`` is one of the adb.exe copies this agent shipped."""
    raw = str(path or "").strip()
    if not raw or Path(raw).name.lower() not in _ADB_NAMES:
        return False
    seq = tuple(candidates) if candidates is not None else bundled_adb_candidates()
    key = os.path.normcase(os.path.normpath(raw))
    return any(os.path.normcase(os.path.normpath(c)) == key for c in seq)


def _run_kwargs(timeout: float) -> dict:
    kw: dict = {"capture_output": True, "timeout": timeout, "shell": False, "stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


def ensure_bundled_server(adb: str, *, run: Optional[RunFn] = None, live_stream: Optional[bool] = None,
                          state_dir: Optional[Path] = None,
                          candidates: Optional[Sequence[str]] = None) -> bool:
    """Bring the bundled adb server up. False when skipped or the start failed.

    The only adb invocation added here is that bundled executable with one fixed
    argument. Anything else (a PATH adb, ``C:\\platform-tools``, a configured SDK
    path) is refused. A live-stream host is refused before any process is spawned.
    """
    if not is_bundled_adb(adb, candidates):
        logger.debug("[adb] %s is not the bundled adb; server left untouched", adb)
        return False
    if live_stream is None:
        from .detect import is_live_stream_host

        live_stream = bool(is_live_stream_host(state_dir))
    if live_stream:
        logger.info("[adb] live-stream host: not bringing an adb server up")
        return False
    cmd = [str(adb), "start-server"]
    try:
        proc = (run or subprocess.run)(cmd, **_run_kwargs(START_TIMEOUT_SEC))
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[adb] bundled server did not come up: %s", e)
        return False
    rc = int(getattr(proc, "returncode", 1) or 0)
    if rc != 0:
        logger.warning("[adb] bundled server exited %s", rc)
        return False
    logger.info("[adb] bundled server is up (%s)", adb)
    return True


def enable_phone_adb(state_dir: Path, *, persist: Callable[[], None], already: bool = False,
                     run: Optional[RunFn] = None, exists: Callable[[str], bool] = os.path.isfile,
                     candidates: Optional[Sequence[str]] = None,
                     live_stream: Optional[bool] = None) -> Tuple[str, Dict[str, Any], str]:
    """Turn bundled-adb management on and bring that server up. Idempotent.

    A live-stream host is refused before any write or process. Only an adb.exe
    from ``bundled_adb_candidates`` is used. A missing bundle does not set the flag.
    """
    if live_stream is None:
        from .detect import is_live_stream_host

        live_stream = bool(is_live_stream_host(state_dir))
    if live_stream:
        logger.info("[adb] live-stream host: enable_phone_adb refused")
        return "rejected", {}, "live_stream_host"
    seq = tuple(candidates) if candidates is not None else bundled_adb_candidates()
    adb = ""
    for cand in seq:
        if exists(cand) and is_bundled_adb(cand, seq):
            adb = cand
            break
    if not adb:
        return "rejected", {}, "bundled_adb_missing"
    persist()
    started = ensure_bundled_server(adb, run=run, live_stream=False, state_dir=state_dir, candidates=seq)
    if not started:
        return "failed", {"adb_manage_server": True, "adb": adb, "server_started": False}, "adb_server_start_failed"
    return ("done", {"adb_manage_server": True, "adb": adb, "server_started": True,
                     "idempotent": bool(already)}, "adb_enabled")


__all__ = [
    "START_TIMEOUT_SEC", "bundled_adb_candidates", "is_bundled_adb", "ensure_bundled_server",
    "enable_phone_adb",
]
