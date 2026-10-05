"""Read-only phone inventory for the fleet heartbeat (agent 0.3.6).

The node lists the Android phones attached to its own adb server and reports
them in the heartbeat as ``phones`` plus ``phones_error``. Inventory only:

* The only collection command is ``adb devices -l`` (argument list, no shell,
  5 s timeout). Nothing is sent to any phone (no input, install or shell) and
  the adb server is never stopped, restarted, re-moded or rebooted by this code.
* ``adb devices`` starts a server when none is running, and an adb client
  whose version differs from the running server restarts that server. Both
  would disturb a machine whose adb belongs to another program, so before the
  command runs the collector asks the existing server for its version over its
  local socket (``host:version``, read-only). No server, or a version that
  does not match the client, means the command is skipped and
  ``phones_error`` says why. The server port is never changed.
* Failure never blocks the heartbeat. A result that succeeded within the last
  ``PHONES_CACHE_SEC`` seconds is reused, otherwise ``phones`` is empty.
* Phones on the exclude list are dropped before they leave the machine.
  ``phones_exclude`` in agent.json (default empty) adds entries: an exact
  serial, a prefix ending in ``*`` (``192.168.0.50:*`` = that host on any
  port), or ``model:<model>`` (matched against the ``model:`` field adb prints
  in ``devices -l``; nothing is asked of the phone). ``PROTECTED_SERIALS`` is
  always excluded, also when it appears inside an mDNS wireless-debugging name
  (``adb-<serial>-xxxx._adb-tls-connect._tcp``): it holds the live-stream phone
  (OnePlus 13, serial 3B1F...), which must never show up in any inventory.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ADB_TIMEOUT_SEC = 5
PHONES_CACHE_SEC = 60
MAX_PHONES = 64
DEFAULT_ADB_PORT = 5037
ADB_COLLECT_ARGS = ("devices", "-l")
ADB_VERSION_ARGS = ("version",)
DEFAULT_PHONES_EXCLUDE: Tuple[str, ...] = ()
# Live-stream phone on 176 (OnePlus 13 / CPH2653). Never reported, not even when
# the configured exclude list is empty or replaced.
PROTECTED_SERIALS: Tuple[str, ...] = ("3B1F4KE5MS140P4X",)
_WINDOWS_ADB_CANDIDATES = (r"C:\platform-tools\adb.exe",)

PHONE_FIELDS = ("serial", "state", "model", "transport")
_SERIAL_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
_HOST_PORT_RE = re.compile(r"^[A-Za-z0-9.\-\[\]:]+:\d{1,5}$")
_ATTR_RE = re.compile(r"^(usb|product|model|device|transport_id):(\S*)$")
_VERSION_RE = re.compile(r"Android Debug Bridge version \d+\.\d+\.(\d+)")
_KNOWN_STATES = {"device", "offline", "unauthorized", "recovery", "sideload", "bootloader",
                 "rescue", "authorizing", "connecting", "host", "no_permissions"}

RunFn = Callable[..., Any]


def _clip(value: Any, n: int) -> str:
    s = str(value or "").strip()
    return "".join(ch for ch in s if ch.isprintable())[:n]


def _transport(serial: str, attrs: Dict[str, str]) -> str:
    # adb on Windows prints no ``usb:`` attribute, so anything that is not a network
    # address (host:port / mDNS name) or an emulator counts as USB.
    if "usb" in attrs:
        return "usb"
    if serial.startswith("emulator-"):
        return "emulator"
    if _HOST_PORT_RE.match(serial) or "._adb-tls-connect." in serial or "._adb._tcp" in serial:
        return "tcp"
    return "usb"


def parse_adb_devices(text: str) -> List[Dict[str, str]]:
    """Parse ``adb devices -l`` output into [{serial, state, model, transport}]."""
    phones: List[Dict[str, str]] = []
    seen = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        if line.startswith("adb:") or line.startswith("error:"):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        serial, state = tokens[0], tokens[1].lower()
        rest = tokens[2:]
        if state == "no" and rest[:1] == ["permissions"]:
            state = "no_permissions"
        if not _SERIAL_RE.match(serial) or serial in seen:
            continue
        if state not in _KNOWN_STATES:
            state = "unknown"
        attrs: Dict[str, str] = {}
        for tok in rest:
            m = _ATTR_RE.match(tok)
            if m:
                attrs[m.group(1)] = m.group(2)
        seen.add(serial)
        phones.append({
            "serial": serial,
            "state": state,
            "model": _clip(attrs.get("model", ""), 64),
            "transport": _transport(serial, attrs),
        })
        if len(phones) >= MAX_PHONES:
            break
    return phones


def normalize_excludes(entries: Any) -> Tuple[str, ...]:
    out: List[str] = []
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        entries = []
    for e in list(DEFAULT_PHONES_EXCLUDE) + list(entries) + list(PROTECTED_SERIALS):
        s = _clip(e, 64).upper()
        if s and s not in out:
            out.append(s)
        if len(out) >= 128:
            break
    return tuple(out)


def is_excluded(serial: str, excludes: Iterable[str], model: str = "") -> bool:
    s = str(serial or "").upper()
    m = str(model or "").strip().upper()
    for p in PROTECTED_SERIALS:
        if p.upper() in s:
            return True
    for e in excludes:
        if e.startswith("MODEL:"):
            if m and m == e[6:].strip():
                return True
        elif e.endswith("*"):
            if len(e) > 1 and s.startswith(e[:-1]):
                return True
        elif s == e:
            return True
    return False


def adb_server_port() -> int:
    raw = (os.environ.get("ANDROID_ADB_SERVER_PORT") or "").strip()
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_ADB_PORT
    return port if 0 < port < 65536 else DEFAULT_ADB_PORT


def adb_server_version(port: int, timeout: float = 1.0) -> Optional[int]:
    """Ask a running adb server for its protocol version. None = no server answering.

    Read-only: one ``host:version`` request over the server's loopback socket.
    Never starts, stops, or reconfigures the server.
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            req = b"host:version"
            s.sendall(b"%04x" % len(req) + req)
            buf = b""
            while len(buf) < 12:
                chunk = s.recv(12 - len(buf))
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    if len(buf) < 12 or buf[:4] != b"OKAY":
        return None
    try:
        return int(buf[8:12].decode("ascii"), 16)
    except ValueError:
        return None


def parse_client_version(text: str) -> Optional[int]:
    m = _VERSION_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def find_adb(configured: str = "", *, which: Callable[[str], Optional[str]] = shutil.which,
             exists: Callable[[str], bool] = os.path.isfile) -> str:
    """configured adb_path (must be a file named adb/adb.exe) -> C:\\platform-tools -> PATH."""
    cand = str(configured or "").strip()
    if cand and Path(cand).name.lower() in ("adb", "adb.exe") and exists(cand):
        return cand
    if sys.platform.startswith("win"):
        for c in _WINDOWS_ADB_CANDIDATES:
            if exists(c):
                return c
    return which("adb") or ""


def _run_kwargs() -> Dict[str, Any]:
    kw: Dict[str, Any] = {"capture_output": True, "timeout": ADB_TIMEOUT_SEC, "shell": False,
                          "stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


def _decode(b: Any) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b or "")


class PhoneCollector:
    """Collect ``phones`` / ``phones_error`` for one heartbeat. Never raises."""

    def __init__(self, *, adb_path: str = "", exclude: Any = None, enabled: bool = True,
                 run: Optional[RunFn] = None, clock: Callable[[], float] = time.time,
                 server_version: Optional[Callable[[int], Optional[int]]] = None,
                 locate: Optional[Callable[[str], str]] = None) -> None:
        self.adb_path = str(adb_path or "")
        self.excludes = normalize_excludes(exclude)
        self.enabled = bool(enabled)
        self._run = run
        self._clock = clock
        self._server_version = server_version
        self._locate = locate
        self._client_versions: Dict[str, Optional[int]] = {}
        self._last_ok: Optional[Tuple[float, List[Dict[str, str]]]] = None

    def _adb(self, adb: str, args: Sequence[str]) -> str:
        if tuple(args) not in (ADB_COLLECT_ARGS, ADB_VERSION_ARGS):
            raise RuntimeError("adb_args_not_allowed")
        cmd = [adb, *args]
        proc = (self._run or subprocess.run)(cmd, **_run_kwargs())
        if int(getattr(proc, "returncode", 1) or 0) != 0:
            raise RuntimeError(f"adb_exit_{getattr(proc, 'returncode', '?')}")
        return _decode(getattr(proc, "stdout", b""))

    def _client_version(self, adb: str) -> Optional[int]:
        if adb not in self._client_versions:
            self._client_versions[adb] = parse_client_version(self._adb(adb, ADB_VERSION_ARGS))
        return self._client_versions[adb]

    def _collect_once(self) -> List[Dict[str, str]]:
        # Module-level lookups at call time (not bound at construction) so a test
        # harness can patch them once for every collector.
        adb = (self._locate or find_adb)(self.adb_path)
        if not adb:
            raise RuntimeError("adb_not_found")
        server = (self._server_version or adb_server_version)(adb_server_port())
        if server is None:
            raise RuntimeError("adb_server_not_running")
        client = self._client_version(adb)
        if client is None:
            raise RuntimeError("adb_version_unknown")
        if client != server:
            raise RuntimeError(f"adb_version_mismatch client={client} server={server}")
        phones = parse_adb_devices(self._adb(adb, ADB_COLLECT_ARGS))
        return [p for p in phones if not is_excluded(p["serial"], self.excludes, p.get("model", ""))]

    def collect(self) -> Tuple[List[Dict[str, str]], str]:
        if not self.enabled:
            return [], "disabled"
        now = self._clock()
        try:
            phones = self._collect_once()
        except subprocess.TimeoutExpired:
            err = "adb_timeout"
        except RuntimeError as e:
            err = str(e)[:120] or "adb_error"
        except Exception as e:  # noqa: BLE001 - the heartbeat must still go out
            err = f"adb_error: {type(e).__name__}"
        else:
            self._last_ok = (now, phones)
            return [dict(p) for p in phones], ""
        if self._last_ok is not None and now - self._last_ok[0] <= PHONES_CACHE_SEC:
            return [dict(p) for p in self._last_ok[1]], err
        return [], err


def sanitize_phones(value: Any) -> List[Dict[str, str]]:
    """Controller side: keep only the four fields, bounded, from a heartbeat ``phones`` value."""
    out: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return out
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        serial = _clip(item.get("serial"), 64)
        if not serial or not _SERIAL_RE.match(serial) or serial in seen:
            continue
        seen.add(serial)
        state = _clip(item.get("state"), 24).lower()
        out.append({
            "serial": serial,
            "state": state if state in _KNOWN_STATES else "unknown",
            "model": _clip(item.get("model"), 64),
            "transport": _clip(item.get("transport"), 16).lower() or "unknown",
        })
        if len(out) >= MAX_PHONES:
            break
    return out


def sanitize_phones_error(value: Any) -> str:
    return _clip(value, 200) if isinstance(value, str) else ""


__all__ = [
    "ADB_TIMEOUT_SEC", "PHONES_CACHE_SEC", "MAX_PHONES", "PHONE_FIELDS", "PROTECTED_SERIALS",
    "DEFAULT_PHONES_EXCLUDE", "PhoneCollector", "parse_adb_devices", "parse_client_version",
    "normalize_excludes", "is_excluded", "adb_server_version", "adb_server_port", "find_adb",
    "sanitize_phones", "sanitize_phones_error",
]