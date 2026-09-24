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
    """icacls steps for ``state_dir_lock_plan``.

    ``/inheritance:r /grant:r`` does not remove explicit ACEs for other SIDs.
    ``reset-dir`` is ``icacls <dir> /reset`` with no ``/T``, so the directory
    itself loses those ACEs before the protected grant. ``/reset`` of children
    still targets ``<dir>\\*``. An empty directory skips that child reset.
    """
    folder = str(path)
    grant = [
        "icacls", folder, "/inheritance:r", "/grant:r",
        "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
    ]
    return [
        ["icacls", folder, "/setowner", "*S-1-5-32-544"],
        ["icacls", folder, "/reset"],
        list(grant),
        ["icacls", folder, "/setowner", "*S-1-5-32-544", "/T", "/C"],
        ["icacls", str(path / "*"), "/reset", "/T", "/C"],
        grant + ["/T", "/C"],
    ]


def state_dir_lock_plan(path: Path) -> List[tuple]:
    """Lock order. Verify the directory after the fresh DACL and again after ``/T``.

    ``reset-dir`` clears explicit ACEs on the directory itself. ``verify-dir``
    and ``verify-tree`` re-read the locked predicate and abort when it fails.
    ``purge-sensitive`` runs only after ``verify-dir`` and only drops a child
    whose owner is not SYSTEM or Administrators, so a previously unlocked but
    already trusted file survives an upgrade.
    """
    commands = state_dir_acl_commands(path)
    return [
        ("setowner-dir", commands[0]),
        ("reset-dir", commands[1]),
        ("grant-dir", commands[2]),
        ("verify-dir", []),
        ("purge-sensitive", []),
        ("setowner-tree", commands[3]),
        ("reset-children", commands[4]),
        ("grant-tree", commands[5]),
        ("verify-tree", []),
    ]


def state_dir_acl_command(path: Path) -> List[str]:
    """The final inheritance-removed grant (last step of ``state_dir_lock_plan``)."""
    return state_dir_acl_commands(path)[-1]


def _run_icacls(argv: List[str]) -> None:
    proc = subprocess.run(argv, check=False, capture_output=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[:300]
        raise StateDirLockError(f"icacls exited {proc.returncode}: {err}")


def assign_owner_admins(path: Path) -> None:
    """Give a newly written secret to Administrators. No-op off Windows.

    ``os.replace`` leaves the creator as owner. A non-UAC admin's user SID is
    not trusted, so the SYSTEM service would otherwise delete ``agent.json``
    and lose ``node_key``. A failed ``icacls`` deletes the secret and raises.
    """
    if os.name != "nt":
        return
    try:
        _run_icacls(["icacls", str(path), "/setowner", "*S-1-5-32-544"])
    except StateDirLockError:
        try:
            Path(path).unlink()
        except OSError:
            pass
        raise


def _posix_dir_locked(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_uid in {0, os.getuid()} and (st.st_mode & 0o077) == 0


def _windows_dir_locked(path: Path) -> bool:
    """True when the directory owner is SYSTEM or Administrators, the DACL is
    protected, and every ACE is only those two SIDs. Any query failure is not locked.
    """
    import ctypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    adv.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ulong),
    ]
    adv.GetSecurityDescriptorControl.restype = ctypes.c_int
    adv.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetAce.restype = ctypes.c_int
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(
        str(path), 1, 0x1 | 0x4,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd),
    )
    if rc != 0 or not sd.value or not owner.value:
        if sd.value:
            kernel.LocalFree(sd)
        return False
    sid_buf = ctypes.c_void_p()
    try:
        if not adv.ConvertSidToStringSidW(owner, ctypes.byref(sid_buf)) or not sid_buf.value:
            return False
        if ctypes.wstring_at(sid_buf.value) not in TRUSTED_OWNER_SIDS:
            return False
        kernel.LocalFree(sid_buf)
        sid_buf = ctypes.c_void_p()
        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        if not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            return False
        if not (control.value & 0x1000):  # SE_DACL_PROTECTED
            return False
        if not dacl.value:
            return False

        class _ACL(ctypes.Structure):
            _fields_ = [
                ("AclRevision", ctypes.c_ubyte),
                ("Sbz1", ctypes.c_ubyte),
                ("AclSize", ctypes.c_ushort),
                ("AceCount", ctypes.c_ushort),
                ("Sbz2", ctypes.c_ushort),
            ]

        class _ACE(ctypes.Structure):
            _fields_ = [
                ("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", ctypes.c_ushort),
            ]

        acl = _ACL.from_address(dacl.value)
        if acl.AceCount < 1:
            return False
        for index in range(int(acl.AceCount)):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                return False
            header = _ACE.from_address(ace.value)
            if header.AceType not in (0, 1):
                return False
            sid_addr = ace.value + ctypes.sizeof(_ACE) + 4
            if not adv.ConvertSidToStringSidW(sid_addr, ctypes.byref(sid_buf)) or not sid_buf.value:
                return False
            sid = ctypes.wstring_at(sid_buf.value)
            kernel.LocalFree(sid_buf)
            sid_buf = ctypes.c_void_p()
            if sid not in TRUSTED_OWNER_SIDS:
                return False
        return True
    finally:
        if sid_buf.value:
            kernel.LocalFree(sid_buf)
        kernel.LocalFree(sd)


def state_file_trusted(*, dir_locked: bool, owner_sid: str) -> bool:
    """A Windows secret is trusted only when both checks pass.

    The directory must be locked (protected DACL, only SYSTEM and Administrators)
    and the file owner must be one of those SIDs. An Administrators-owned file
    in a directory that still has an Everyone ACE is not trusted: the user can
    rewrite ``restart_cmd`` and the service would run it as SYSTEM.
    """
    return bool(dir_locked) and owner_sid in TRUSTED_OWNER_SIDS


def _is_reparse(path: Path) -> bool:
    """True for a symlink or, on Windows, a junction. False when the path is absent."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def _require_state_parent(path: Path) -> None:
    """Refuse a reparse point on the state dir or its parent.

    On Windows the parent (``%ProgramData%\\ChatX``) must be owned by SYSTEM or
    Administrators. A parent we just created is given to Administrators. An
    existing parent with any other owner is refused, not adopted.
    """
    path = Path(path)
    parent = path.parent
    if _is_reparse(parent) or _is_reparse(path):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    if os.name != "nt":
        return
    created = False
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        created = True
    if _is_reparse(parent) or _is_reparse(path):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    if created:
        _run_icacls(["icacls", str(parent), "/setowner", "*S-1-5-32-544"])
    if file_owner_sid(parent) not in TRUSTED_OWNER_SIDS:
        raise StateDirLockError("parent directory owner is not SYSTEM or Administrators")


def _require_locked_dir(path: Path) -> None:
    """Post-check used after the directory grant and again after the ``/T`` grant."""
    if os.name == "nt":
        ok = _windows_dir_locked(path)
    else:
        ok = _posix_dir_locked(path)
    if not ok:
        raise StateDirLockError("state directory ACL is not limited to SYSTEM and Administrators")


def _purge_sensitive(path: Path, *, unconditional: bool) -> None:
    """Delete agent.json, machine_id, and room.key. Raise if a delete does not stick.

    On Windows ``unconditional`` is false: the directory DACL was just replaced
    and verified, so a SYSTEM or Administrators owner is kept and any other
    owner is removed. A failure aborts before ``/setowner /T`` can adopt the file.
    """
    for name in ("agent.json", "machine_id", "room.key"):
        child = path / name
        if not child.is_file():
            continue
        if unconditional:
            try:
                child.unlink()
            except OSError as e:
                raise StateDirLockError(f"could not delete {name}: {e}") from e
            if child.exists():
                raise StateDirLockError(f"could not delete {name}")
            logger.warning("[fleet] removed %s because the state dir was not locked", name)
            continue
        discard_untrusted_secret(child)


def lock_state_dir(path: Path) -> None:
    """Create the state dir and lock it down before machine_id / agent.json / room.key are written.

    On Windows the directory owner is set, explicit ACEs are cleared with
    ``/reset`` (no ``/T``), and a protected SYSTEM+Administrators DACL is
    written. That result is checked before any child is inspected. Children
    whose owner is not SYSTEM or Administrators are deleted. Then ``/setowner
    /T``, child reset, and grant ``/T`` run, and the locked check runs again.
    On POSIX the directory is mode 0700. A symlink for the directory or its
    parent is refused. A POSIX directory that was not already 0700 and owned
    by root or the current user loses the three secret files.
    """
    path = Path(path)
    _require_state_parent(path)
    existed = path.is_dir() and not _is_reparse(path)
    path.mkdir(parents=True, exist_ok=True)
    if _is_reparse(path) or _is_reparse(path.parent):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    if os.name == "nt":
        for step, argv in state_dir_lock_plan(path):
            if step in ("verify-dir", "verify-tree"):
                _require_locked_dir(path)
                continue
            if step == "purge-sensitive":
                _purge_sensitive(path, unconditional=False)
                continue
            if step == "reset-children" and not any(path.iterdir()):
                continue
            _run_icacls(argv)
        return
    already = _posix_dir_locked(path) if existed else False
    try:
        os.chmod(path, 0o700)
    except OSError as e:
        raise StateDirLockError(f"chmod state dir failed: {e}") from e
    _require_locked_dir(path)
    _purge_sensitive(path, unconditional=not already)


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
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    pstr = ctypes.c_void_p()
    try:
        if not adv.ConvertSidToStringSidW(owner, ctypes.byref(pstr)):
            raise StateDirLockError("ConvertSidToStringSid failed")
        return ctypes.wstring_at(pstr.value) if pstr.value else ""
    finally:
        if pstr.value:
            kernel.LocalFree(pstr)
        if sd.value:
            kernel.LocalFree(sd)


def discard_untrusted_secret(path: Path, *, owner_sid: Optional[str] = None,
                             dir_locked: Optional[bool] = None) -> bool:
    """Delete a pre-existing agent.json, machine_id, or room.key that is not trusted.

    On Windows the file is kept only when ``state_file_trusted`` is true: the
    parent directory is locked and the owner is SYSTEM or Administrators.
    Passing ``dir_locked`` forces that rule, including on other platforms.
    An explicit ``owner_sid`` without ``dir_locked`` is the SID half of the
    check used by tests. ``restart_cmd`` runs with ``shell=True`` as SYSTEM.
    Returns True when the file was removed. Raises if it cannot be removed.
    """
    path = Path(path)
    if not path.is_file() or path.name not in _SENSITIVE_NAMES:
        return False
    if os.name == "nt" or dir_locked is not None:
        sid = file_owner_sid(path) if owner_sid is None else owner_sid
        locked = _windows_dir_locked(path.parent) if dir_locked is None else bool(dir_locked)
        trusted = state_file_trusted(dir_locked=locked, owner_sid=sid)
    elif owner_sid is not None:
        trusted = owner_sid in TRUSTED_OWNER_SIDS
    else:
        st = path.stat()
        trusted = st.st_uid in {0, os.getuid()} and (st.st_mode & 0o022) == 0
    if trusted:
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
        assign_owner_admins(cache)
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
           "state_dir_acl_command", "state_dir_acl_commands", "state_dir_lock_plan",
           "assign_owner_admins", "discard_untrusted_secret", "state_file_trusted",
           "StateDirLockError", "TRUSTED_OWNER_SIDS", "ENV_STATE_DIR"]
