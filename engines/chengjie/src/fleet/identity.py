"""节点机器标识与本地状态目录。

标识优先级（越前越优）：
1. ``src.licensing.machine_bridge.machine_fingerprint()``——与授权绑机同一个指纹，一台机器
   在授权体系与主控里是同一个身份（方案 P0-3 的「machine_id 统一」）。
2. 操作系统机器 GUID（Windows ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``、
   Linux ``/etc/machine-id``）+ 主机名 → sha256 短码。
3. 状态目录里持久化的随机 uuid（首次生成后固定）。

``platform/licensing/machine_id.py`` 不在本仓（母仓 boundless 才有），源码态多半走 2/3；
三条路都会把结果缓存进 ``<state_dir>/machine_id`` 保证重启后不变。
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import socket
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

ENV_STATE_DIR = "CHATX_FLEET_STATE_DIR"


def default_state_dir() -> Path:
    """节点状态目录（node_key / machine_id / 任务日志）。Windows → %ProgramData%\\ChatX\\fleet，
    否则 ~/.chatx/fleet；可用 ``CHATX_FLEET_STATE_DIR`` 覆盖（测试 / 多 Agent 并存）。"""
    override = (os.environ.get(ENV_STATE_DIR) or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("ProgramData") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "ChatX" / "fleet"
    return Path.home() / ".chatx" / "fleet"


def host_name() -> str:
    try:
        return socket.gethostname() or platform.node() or "unknown-host"
    except Exception:
        return "unknown-host"


def _os_machine_guid() -> str:
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                val, _ = winreg.QueryValueEx(k, "MachineGuid")
                return str(val or "").strip()
        except Exception:
            return ""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            v = Path(p).read_text(encoding="utf-8").strip()
            if v:
                return v
        except Exception:
            continue
    return ""


def _licensing_fingerprint() -> str:
    try:
        from src.licensing.machine_bridge import machine_fingerprint

        return str(machine_fingerprint() or "").strip()
    except Exception:
        return ""


class StateDirLockError(RuntimeError):
    """State directory could not be locked. Callers must not write secrets after this."""


# SYSTEM and the built-in Administrators group. A planted file with any other owner is refused.
TRUSTED_OWNER_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
_SENSITIVE_NAMES = frozenset({"agent.json", "machine_id", "room.key"})


def state_dir_acl_commands(path: Path) -> List[List[str]]:
    """Owner Administrators, reset children, then SYSTEM + Administrators only.

    ``/reset`` targets ``<dir>\\*`` so it does not wipe the directory grant. The
    grant is last and carries ``/T`` so files that already exist pick up that
    DACL instead of the ACL they inherited before the directory was locked.
    An empty directory skips the reset (icacls has nothing to match).
    """
    folder = str(path)
    return [
        ["icacls", folder, "/setowner", "*S-1-5-32-544", "/T", "/C"],
        ["icacls", str(path / "*"), "/reset", "/T", "/C"],
        [
            "icacls", folder, "/inheritance:r", "/grant:r",
            "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/T", "/C",
        ],
    ]


def state_dir_acl_command(path: Path) -> List[str]:
    """The inheritance-removed grant (last step of ``state_dir_acl_commands``)."""
    return state_dir_acl_commands(path)[2]


def _run_icacls(argv: List[str]) -> None:
    proc = subprocess.run(argv, check=False, capture_output=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[:300]
        raise StateDirLockError(f"icacls exited {proc.returncode}: {err}")


def lock_state_dir(path: Path) -> None:
    """Create the state dir and lock it down before machine_id / agent.json / room.key are written.

    On Windows every icacls step must succeed. A failure raises ``StateDirLockError``
    and the caller must not write a secret. On POSIX the directory is mode 0700;
    a chmod failure raises the same error.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        commands = state_dir_acl_commands(path)
        _run_icacls(commands[0])
        if any(path.iterdir()):
            _run_icacls(commands[1])
        _run_icacls(commands[2])
        return
    try:
        os.chmod(path, 0o700)
    except OSError as e:
        raise StateDirLockError(f"chmod state dir failed: {e}") from e


def file_owner_sid(path: Path) -> str:
    """Windows owner SID. Empty on other platforms."""
    if os.name != "nt":
        return ""
    import ctypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(
        ctypes.c_wchar_p(str(path)),
        1,  # SE_FILE_OBJECT
        0x1,  # OWNER_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(sd),
    )
    if rc != 0:
        raise StateDirLockError(f"GetNamedSecurityInfo failed: {rc}")
    try:
        out = ctypes.c_wchar_p()
        if not adv.ConvertSidToStringSidW(owner, ctypes.byref(out)):
            raise StateDirLockError("ConvertSidToStringSid failed")
        return str(out.value or "")
    finally:
        if sd:
            kernel.LocalFree(sd)


def discard_untrusted_secret(path: Path, *, owner_sid: Optional[str] = None) -> bool:
    """Delete a pre-existing agent.json, machine_id, or room.key with an untrusted owner.

    Windows trusts only SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544).
    This runs before ``icacls /setowner``, which would otherwise adopt a planted
    file. ``restart_cmd`` in agent.json is executed with ``shell=True`` as SYSTEM.
    Returns True when the file was removed. Raises if it cannot be removed.
    """
    path = Path(path)
    if not path.is_file() or path.name not in _SENSITIVE_NAMES:
        return False
    if os.name == "nt" or owner_sid is not None:
        sid = file_owner_sid(path) if owner_sid is None else owner_sid
        if sid in TRUSTED_OWNER_SIDS:
            return False
    else:
        st = path.stat()
        if st.st_uid in {0, os.getuid()} and (st.st_mode & 0o022) == 0:
            return False
    path.unlink()
    if path.exists():
        raise StateDirLockError(f"could not delete untrusted {path.name}")
    logger.warning("[fleet] removed untrusted %s", path.name)
    return True


def node_machine_id(state_dir: Optional[Path] = None) -> str:
    """稳定的机器标识（形如 ``m-<16hex>``）。"""
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    cache = sd / "machine_id"
    discard_untrusted_secret(cache)
    try:
        cached = cache.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    except StateDirLockError:
        raise
    except Exception:
        pass

    raw = _licensing_fingerprint()
    source = "licensing"
    if not raw:
        guid = _os_machine_guid()
        if guid:
            raw = f"{guid}|{host_name()}"
            source = "os_guid"
    if not raw:
        raw = uuid.uuid4().hex
        source = "random"
    mid = "m-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    try:
        lock_state_dir(sd)
        cache.write_text(mid, encoding="utf-8")
    except StateDirLockError:
        raise
    except Exception:
        logger.debug("[fleet] machine_id 写缓存失败 %s", cache, exc_info=True)
    logger.info("[fleet] machine_id=%s (source=%s)", mid, source)
    return mid


def os_label() -> str:
    try:
        return f"{platform.system()} {platform.release()}".strip()
    except Exception:
        return "unknown"


__all__ = ["default_state_dir", "host_name", "lock_state_dir", "node_machine_id", "os_label",
           "state_dir_acl_command", "state_dir_acl_commands", "discard_untrusted_secret",
           "StateDirLockError", "TRUSTED_OWNER_SIDS", "ENV_STATE_DIR"]
