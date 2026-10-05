"""节点机器标识与本地状态目录。

标识（``m-<16hex>``）在首次生成时写进 ``<state_dir>/machine_id``，之后重启不变。

0.3.5 起的生成规则（``scheme 2``）::

    sha256("v2|" + 基准 + "|" + 主机名 + "|" + 硬件信号)

* 基准：``src.licensing.machine_bridge.machine_fingerprint()``（授权绑机指纹），没有则用操作系统
  机器 GUID（Windows ``MachineGuid`` / Linux ``/etc/machine-id``）。
* 硬件信号：SMBIOS UUID、第一块非 USB 物理盘序列号、主网卡 MAC。全零 / 全 F /
  「To be filled by O.E.M.」之类的占位值丢弃。一个都读不到时改用随机 uuid（仍只生成一次）。

为什么：克隆系统盘的两台电脑 MachineGuid 和主机名完全一样，0.3.4 及以前的
``sha256(MachineGuid|主机名)`` 会算出同一个 machine_id；主控按 machine_id 复用 node_id 并换 key，
两台机器就互相把对方挤下线。克隆盘里带过去的 ``machine_id`` 缓存也是同一个值。

缓存旁边的 ``machine_id.hw`` 记录生成 / 认领时的硬件指纹（每项只存 sha256 前 16 位，不存原始序列号）：

* 指纹与本机不符（缓存是从别的机器拷来的）→ 重新生成。
* 0.3.4 留下的缓存没有指纹：服务 / 自动升级时原样认领（``origin=adopted_legacy``，
  没撞号的机器保持原 id）；重新运行安装程序（``identity --reinstall``）时不再盲目沿用，按本机硬件重新生成。
* 主控明确报告 machine_id 冲突时 ``regenerate_machine_id`` 强制换一个。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
# Built-in Users. The parent directory may grant this SID read and execute only.
_USERS_SID = "S-1-5-32-545"
# Write-ish bits a Users ACE must not carry. Read/execute/synchronize are allowed.
_USERS_WRITE_MASK = (
    0x00000002  # FILE_ADD_FILE
    | 0x00000004  # FILE_ADD_SUBDIRECTORY / FILE_APPEND_DATA
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
MACHINE_ID_NAME = "machine_id"
MACHINE_ID_HW_NAME = "machine_id.hw"
_SENSITIVE_NAMES = frozenset({"agent.json", MACHINE_ID_NAME, MACHINE_ID_HW_NAME, "room.key"})


def state_dir_acl_commands(path: Path) -> List[List[str]]:
    """Directory-only icacls for a freshly created ``fleet``. No ``/T``.

    ``/reset`` clears inherited ACEs on that new directory. ``/inheritance:r
    /grant:r`` then writes a protected SYSTEM + Administrators DACL. Nothing
    here walks children.
    """
    folder = str(path)
    return [
        ["icacls", folder, "/setowner", "*S-1-5-32-544"],
        ["icacls", folder, "/reset"],
        [
            "icacls", folder, "/inheritance:r", "/grant:r",
            "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
        ],
    ]


def state_dir_lock_plan(path: Path) -> List[tuple]:
    """Lock order for a fleet directory that is not already locked.

    ``retire-legacy`` renames the old directory to ``fleet.legacy-<id>`` before
    any icacls, so a junction is never the target of a grant. The icacls steps
    apply only to the new empty directory. ``verify-dir`` checks the locked
    predicate and that the new directory is not a reparse point.
    """
    commands = state_dir_acl_commands(path)
    return [
        ("retire-legacy", []),
        ("setowner-dir", commands[0]),
        ("reset-dir", commands[1]),
        ("grant-dir", commands[2]),
        ("verify-dir", []),
    ]


def fleet_lock_steps(was_locked: bool) -> List[str]:
    """Step names to run. An already-locked directory is only re-checked.

    There is no recursive grant. Service ``cfg.save()`` therefore cannot follow
    a junction, and it does not rename a directory that is already locked.
    """
    names = [name for name, _argv in state_dir_lock_plan(Path("."))]
    if was_locked:
        return ["verify-dir"]
    return names


def parent_dir_lock_plan(path: Path) -> List[tuple]:
    """Protected DACL for ``%ProgramData%\\ChatX``. No ``/T``.

    SYSTEM and Administrators get full control. Users (S-1-5-32-545) get
    read and execute only, so a later ``fleet`` ``/reset`` inherits nothing
    a standard user can write. ``parent-verify`` re-reads that predicate.
    """
    folder = str(path)
    return [
        ("parent-setowner", ["icacls", folder, "/setowner", "*S-1-5-32-544"]),
        ("parent-reset", ["icacls", folder, "/reset"]),
        ("parent-grant", [
            "icacls", folder, "/inheritance:r", "/grant:r",
            "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
            "*S-1-5-32-545:(OI)(CI)RX",
        ]),
        ("parent-verify", []),
    ]


def _users_rx_mask_ok(mask: int) -> bool:
    """True when an allow mask has no write, delete, or take-ownership bits."""
    return (int(mask) & _USERS_WRITE_MASK) == 0


def state_dir_acl_command(path: Path) -> List[str]:
    """The directory-only protected grant. No ``/T``."""
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


def _windows_dir_locked(path: Path, *, allow_users_rx: bool = False) -> bool:
    """True when the directory owner is SYSTEM or Administrators, the DACL is
    protected, and every ACE is only those two SIDs. Any query failure is not locked.

    ``allow_users_rx`` also accepts a Users allow ACE whose mask has no write
    bits. That is the parent directory (``%ProgramData%\\ChatX``), not ``fleet``.
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
            if sid in TRUSTED_OWNER_SIDS:
                continue
            if allow_users_rx and sid == _USERS_SID and header.AceType == 0:
                mask = ctypes.c_uint32.from_address(ace.value + ctypes.sizeof(_ACE)).value
                if _users_rx_mask_ok(mask):
                    continue
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
    """True for a symlink or, on Windows, any reparse point (including a junction).

    ``Path.is_symlink()`` does not see a Windows mount-point junction. A missing
    path is not a reparse point. Any other ``lstat`` error is treated as one,
    so a check that cannot be completed is refused.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if os.name == "nt":
        return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(st.st_mode)


def _require_state_parent(path: Path) -> None:
    """Lock ``%ProgramData%\\ChatX`` before ``fleet`` is created or renamed.

    The parent gets a protected DACL: SYSTEM and Administrators full control,
    Users read and execute only, then that predicate is checked again. An
    already-locked parent skips reset and grant. A reparse point on the parent
    is refused. A reparse point on ``fleet`` itself is left for
    ``lock_state_dir`` to rename aside; it is not granted in place.
    """
    path = Path(path)
    parent = path.parent
    if _is_reparse(parent):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    if os.name != "nt":
        return
    try:
        parent_exists = parent.lstat() is not None
    except FileNotFoundError:
        parent_exists = False
    except OSError as e:
        raise StateDirLockError("could not stat the parent directory") from e
    if not parent_exists:
        parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(parent):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    already = _windows_dir_locked(parent, allow_users_rx=True)
    for step, argv in parent_dir_lock_plan(parent):
        if step == "parent-verify":
            if not _windows_dir_locked(parent, allow_users_rx=True):
                raise StateDirLockError("parent directory ACL is not locked")
            continue
        if already:
            continue
        _run_icacls(argv)
    if _is_reparse(parent):
        raise StateDirLockError("refusing a reparse point in the state directory path")


def _require_locked_dir(path: Path) -> None:
    """Post-check: not a reparse point, and the directory-only locked predicate holds.

    The reparse check uses ``lstat`` and runs before the ACL read, which follows
    a junction.
    """
    if _is_reparse(path):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    if os.name == "nt":
        ok = _windows_dir_locked(path)
    else:
        ok = _posix_dir_locked(path)
    if not ok:
        raise StateDirLockError("state directory ACL is not limited to SYSTEM and Administrators")


def _purge_sensitive(path: Path, *, unconditional: bool) -> None:
    """Delete agent.json, machine_id, and room.key. Raise if a delete does not stick.

    On Windows ``unconditional`` is false: a SYSTEM or Administrators owner is
    kept and any other owner is removed. A failure aborts.
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


def state_dir_is_locked(path: Path) -> bool:
    """Same locked predicate ``lock_state_dir`` samples before it renames anything."""
    path = Path(path)
    if _is_reparse(path):
        return False
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if os.name == "nt":
        return _windows_dir_locked(path)
    return _posix_dir_locked(path)


RENAME_RETRIES = 5
RENAME_RETRY_SEC = 0.5


class LegacyRenameError(StateDirLockError):
    """The unlocked state directory could not be moved aside (Windows handle still open)."""


def _rename_legacy_fleet(path: Path, *, retries: int = RENAME_RETRIES,
                         sleep=None) -> Path:
    """Rename ``fleet`` to ``fleet.legacy-<id>``. ``os.rename`` does not follow a junction.

    On Windows a handle still open inside the directory (a log file, a swap
    script) makes the rename fail with WinError 5. Retry briefly, then raise
    ``StateDirLockError`` so callers keep the old directory untouched.
    """
    import time as _time

    pause = sleep or _time.sleep
    last: Optional[OSError] = None
    for attempt in range(max(1, int(retries))):
        legacy = path.with_name(f"{path.name}.legacy-{uuid.uuid4().hex}")
        try:
            os.rename(path, legacy)
            return legacy
        except FileNotFoundError:
            raise
        except OSError as e:
            last = e
            if attempt + 1 < retries:
                pause(RENAME_RETRY_SEC)
    raise LegacyRenameError(f"could not move the old state directory aside: {last}")


def lock_state_dir(path: Path) -> None:
    """Create the state dir and lock it down before machine_id / agent.json / room.key are written.

    The parent is locked first. If ``fleet`` is already locked, it is checked
    again and otherwise left alone, including by a service ``save``. If it is
    missing, unlocked, or a reparse point, it is not passed to icacls: an
    existing directory is renamed to ``fleet.legacy-<id>`` and kept, then a new
    empty directory is created. That new directory gets a directory-only
    protected DACL (or mode 0700). There is no ``/T``. The check after the
    grant refuses a reparse point and requires the locked predicate.
    """
    path = Path(path)
    _require_state_parent(path)
    if _is_reparse(path.parent):
        raise StateDirLockError("refusing a reparse point in the state directory path")
    try:
        os.lstat(path)
        present = True
    except FileNotFoundError:
        present = False
    except OSError as e:
        raise StateDirLockError("could not stat the state directory") from e
    was_locked = bool(present) and state_dir_is_locked(path)
    if present and not was_locked:
        _rename_legacy_fleet(path)
        present = False
    if not present:
        path.mkdir()
    if os.name == "nt":
        allowed = set(fleet_lock_steps(was_locked))
        for step, argv in state_dir_lock_plan(path):
            if step not in allowed or step == "retire-legacy":
                continue
            if step == "verify-dir":
                _require_locked_dir(path)
                continue
            _run_icacls(argv)
        if was_locked:
            _require_locked_dir(path)
        return
    try:
        os.chmod(path, 0o700)
    except OSError as e:
        raise StateDirLockError(f"chmod state dir failed: {e}") from e
    _require_locked_dir(path)
    if was_locked:
        _purge_sensitive(path, unconditional=False)


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


# ── 硬件指纹（克隆盘防撞号） ─────────────────────────────────────────────────
ENV_HW_PROBE = "CHATX_FLEET_HW_PROBE"   # "0" 关闭硬件探测（排障用）
ORIGIN_GENERATED = "generated"
ORIGIN_ADOPTED = "adopted_legacy"
_HW_KEYS = ("smbios_uuid", "disk_serial", "mac")
_FP_NAMES = {"smbios_uuid": "smbios", "disk_serial": "disk", "mac": "mac"}
_BOGUS_HW = frozenset({
    "NONE", "NULL", "N/A", "NA", "UNKNOWN", "DEFAULT STRING", "DEFAULT", "TO BE FILLED BY O.E.M.",
    "TO BE FILLED BY OEM", "NOT SPECIFIED", "NOT APPLICABLE", "NOT AVAILABLE", "SYSTEM SERIAL NUMBER",
    "SYSTEM PRODUCT NAME", "INVALID", "OEM", "O.E.M.", "123456789", "1234567890", "0123456789",
    # 常见的廉价主板占位 SMBIOS UUID（很多台同值）
    "03000200-0400-0500-0006-000700080009",
})
_PS_HW_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$o=[ordered]@{};"
    "$p=Get-CimInstance -ClassName Win32_ComputerSystemProduct | Select-Object -First 1;"
    "$o.smbios_uuid=[string]$p.UUID;"
    "$d=Get-CimInstance -ClassName Win32_DiskDrive | Where-Object { $_.InterfaceType -ne 'USB' -and $_.MediaType -notmatch 'Removable' }"
    " | Sort-Object Index | Select-Object -First 1;"
    "$o.disk_serial=[string]$d.SerialNumber;"
    "$n=@(Get-CimInstance -ClassName Win32_NetworkAdapter -Filter 'PhysicalAdapter=True' | Where-Object { $_.MACAddress });"
    "$pci=@($n | Where-Object { [string]$_.PNPDeviceID -like 'PCI\\*' });"
    "if($pci.Count -gt 0){ $n=$pci };"
    "$o.mac=[string](@($n | Sort-Object Index | Select-Object -First 1).MACAddress);"
    "$o | ConvertTo-Json -Compress"
)
_HW_MEMO: Optional[Dict[str, str]] = None


def clean_hw_value(value: Any, kind: str = "") -> str:
    """Normalise one hardware value. Placeholders, all-zero / all-F and malformed values → ``""``."""
    v = " ".join(str(value or "").replace("\x00", " ").split()).upper()
    if not v or v in _BOGUS_HW:
        return ""
    core = re.sub(r"[^0-9A-Z]", "", v)
    if not core or len(set(core)) == 1:
        return ""
    if kind == "mac":
        if len(core) != 12 or any(c not in "0123456789ABCDEF" for c in core):
            return ""
        if int(core[1], 16) & 1:  # multicast bit: not a burned-in address
            return ""
        return core
    if kind == "smbios_uuid" and len(core) < 8:
        return ""
    return v


def _run_quiet(argv: List[str], timeout: float) -> str:
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, **kw)
    return (proc.stdout or b"").decode("utf-8", "replace")


def _wmic_value(args: List[str]) -> str:
    try:
        lines = [ln.strip() for ln in _run_quiet(["wmic", *args], 15).splitlines() if ln.strip()]
    except Exception:
        return ""
    return lines[1] if len(lines) >= 2 else ""


def _probe_windows() -> Dict[str, str]:
    out: Dict[str, str] = {}
    ps = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                      "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        txt = _run_quiet([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-Command", _PS_HW_SCRIPT], 30)
        i, j = txt.find("{"), txt.rfind("}")
        d = json.loads(txt[i:j + 1]) if 0 <= i < j else {}
        if isinstance(d, dict):
            out = {k: str(d.get(k) or "") for k in _HW_KEYS}
    except Exception:
        logger.debug("[fleet] hardware probe via PowerShell failed", exc_info=True)
    # wmic is gone on new Windows 11 builds; only a fallback.
    if not clean_hw_value(out.get("smbios_uuid"), "smbios_uuid"):
        out["smbios_uuid"] = _wmic_value(["csproduct", "get", "uuid"])
    if not clean_hw_value(out.get("disk_serial"), "disk_serial"):
        out["disk_serial"] = _wmic_value(["diskdrive", "where", "index=0", "get", "serialnumber"])
    return out


def _probe_posix() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        out["smbios_uuid"] = Path("/sys/class/dmi/id/product_uuid").read_text(encoding="utf-8").strip()
    except Exception:
        logger.debug("[fleet] product_uuid not readable", exc_info=True)
    try:
        for dev in sorted(Path("/sys/class/net").iterdir()):
            if dev.name == "lo" or not (dev / "device").exists():
                continue
            out["mac"] = (dev / "address").read_text(encoding="utf-8").strip()
            break
    except Exception:
        logger.debug("[fleet] NIC address not readable", exc_info=True)
    return out


def _probe_hardware() -> Dict[str, str]:
    """Raw hardware strings. Replaced in tests."""
    raw = _probe_windows() if os.name == "nt" else _probe_posix()
    if not clean_hw_value(raw.get("mac"), "mac"):
        try:
            node = uuid.getnode()
            if not (node >> 40) & 1:  # uuid.getnode() falls back to a random multicast number
                raw["mac"] = f"{node:012X}"
        except Exception:
            logger.debug("[fleet] uuid.getnode failed", exc_info=True)
    return raw


def hardware_signals(*, refresh: bool = False) -> Dict[str, str]:
    """Cleaned clone-resistant signals ``{smbios_uuid, disk_serial, mac}`` (missing keys omitted).

    Probed once per process. ``CHATX_FLEET_HW_PROBE=0`` turns probing off.
    """
    global _HW_MEMO
    if _HW_MEMO is not None and not refresh:
        return dict(_HW_MEMO)
    if (os.environ.get(ENV_HW_PROBE) or "").strip() == "0":
        raw: Dict[str, str] = {}
    else:
        try:
            raw = _probe_hardware() or {}
        except Exception:
            logger.debug("[fleet] hardware probe failed", exc_info=True)
            raw = {}
    sig = {k: clean_hw_value(raw.get(k), k) for k in _HW_KEYS}
    _HW_MEMO = {k: v for k, v in sig.items() if v}
    return dict(_HW_MEMO)


def reset_hardware_cache() -> None:
    global _HW_MEMO
    _HW_MEMO = None


def hardware_fingerprint(signals: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Per-component sha256 prefixes ``{smbios, disk, mac}``. Raw serials are never stored or sent."""
    sig = hardware_signals() if signals is None else signals
    out: Dict[str, str] = {}
    for key in _HW_KEYS:
        val = str(sig.get(key) or "")
        if val:
            name = _FP_NAMES[key]
            out[name] = hashlib.sha256(f"{name}:{val}".encode("utf-8")).hexdigest()[:16]
    return out


def fingerprint_digest(fp: Optional[Dict[str, str]]) -> str:
    """One short hash of a fingerprint (sent to the controller as ``meta.hw_fp``)."""
    if not fp:
        return ""
    body = "|".join(f"{k}={fp[k]}" for k in sorted(fp) if fp.get(k))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] if body else ""


def fingerprints_match(stored: Any, current: Any) -> Optional[bool]:
    """True / False when comparable, None when nothing can be compared.

    SMBIOS UUID and disk serial decide. A MAC is only consulted when neither of
    those is present on both sides, so plugging in a USB NIC does not change the id.
    """
    a = stored if isinstance(stored, dict) else {}
    b = current if isinstance(current, dict) else {}
    strong = [k for k in ("smbios", "disk") if a.get(k) and b.get(k)]
    if strong:
        return all(a[k] == b[k] for k in strong)
    if a.get("mac") and b.get("mac"):
        return a["mac"] == b["mac"]
    return None


def short_machine_id(mid: str) -> str:
    """``m-763f4c4341fa326a`` → ``m-763f4c43`` (enough to tell two same-name PCs apart)."""
    mid = str(mid or "")
    return mid[:10] if mid.startswith("m-") else mid[:8]


def _derive_machine_id(signals: Dict[str, str], *, salt: str = "") -> Tuple[str, str]:
    base = _licensing_fingerprint()
    if not base:
        base = _os_machine_guid()
    hw = "|".join(f"{k}={signals[k]}" for k in _HW_KEYS if signals.get(k))
    if hw:
        raw, source = f"v2|{base}|{host_name()}|{hw}", "hw"
    else:
        raw, source = f"v2|{base}|{host_name()}|rand={uuid.uuid4().hex}", "random"
    if salt:
        raw += f"|salt={salt}"
    return "m-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], source


def identity_meta(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Contents of ``machine_id.hw`` (``{}`` when missing, unreadable, or untrusted)."""
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    side = sd / MACHINE_ID_HW_NAME
    try:
        discard_untrusted_secret(side)
        d = json.loads(side.read_text(encoding="utf-8"))
    except StateDirLockError:
        raise
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _write_identity_meta(sd: Path, meta: Dict[str, Any]) -> None:
    side = sd / MACHINE_ID_HW_NAME
    side.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    assign_owner_admins(side)


def _note_meta_if_locked(sd: Path, meta: Dict[str, Any]) -> None:
    """Write the sidecar on a read path only when the directory is already locked.

    ``lock_state_dir`` would rename an unlocked directory aside; a read must not do that.
    """
    try:
        if state_dir_is_locked(sd):
            _write_identity_meta(sd, meta)
    except StateDirLockError:
        raise
    except Exception:
        logger.debug("[fleet] machine_id.hw not written %s", sd, exc_info=True)


def resolve_machine_identity(state_dir: Optional[Path] = None, *, reinstall: bool = False,
                             force_new: bool = False, reason: str = "") -> Dict[str, Any]:
    """Load, adopt, or (re)generate the node machine_id.

    Returns ``{machine_id, source, origin, changed, previous, reason, hw_fp}``.
    ``reinstall`` (installer) refuses a cache without a matching hardware fingerprint.
    ``force_new`` (controller reported a conflict) always yields a different id.
    """
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    cache = sd / MACHINE_ID_NAME
    discard_untrusted_secret(cache)
    cached = ""
    try:
        cached = cache.read_text(encoding="utf-8").strip()
    except StateDirLockError:
        raise
    except Exception:
        cached = ""

    why = ""
    if cached and not force_new:
        meta = identity_meta(sd)
        bound = meta.get("machine_id") == cached and isinstance(meta.get("fp"), dict)
        if bound:
            cur = hardware_fingerprint()
            same = fingerprints_match(meta["fp"], cur)
            if same is False:
                why = "hw_mismatch"
            elif reinstall and meta.get("origin") == ORIGIN_ADOPTED:
                why = "reinstall_legacy"
            else:
                if same is None and cur and fingerprints_match(cur, cur) is not None:
                    # Recorded while the probe failed: fill it in now so a later clone is caught.
                    meta = dict(meta, fp=cur, hw_fp=fingerprint_digest(cur))
                    _note_meta_if_locked(sd, meta)
                return {"machine_id": cached, "source": str(meta.get("source") or ""),
                        "origin": str(meta.get("origin") or ORIGIN_GENERATED), "changed": False,
                        "previous": "", "reason": "cached", "hw_fp": str(meta.get("hw_fp") or "")}
        elif reinstall:
            why = "reinstall_legacy"
        else:
            # Cache from 0.3.4 or older: keep it (machines that do not collide keep their node).
            cur = hardware_fingerprint()
            meta = {"v": 2, "machine_id": cached, "origin": ORIGIN_ADOPTED, "source": "legacy",
                    "fp": cur, "hw_fp": fingerprint_digest(cur), "at": int(time.time())}
            _note_meta_if_locked(sd, meta)
            return {"machine_id": cached, "source": "legacy", "origin": ORIGIN_ADOPTED, "changed": False,
                    "previous": "", "reason": "adopted_legacy", "hw_fp": meta["hw_fp"]}
    elif cached:
        why = reason or "conflict"
    else:
        why = "new"

    signals = hardware_signals()
    mid, source = _derive_machine_id(signals)
    if cached and mid == cached:
        mid, source = _derive_machine_id(signals, salt=uuid.uuid4().hex)
    fp = hardware_fingerprint(signals)
    meta = {"v": 2, "machine_id": mid, "origin": ORIGIN_GENERATED, "source": source, "fp": fp,
            "hw_fp": fingerprint_digest(fp), "at": int(time.time()), "reason": why}
    if cached:
        meta["previous_machine_id"] = cached
    try:
        lock_state_dir(sd)
        cache.write_text(mid, encoding="utf-8")
        assign_owner_admins(cache)
        _write_identity_meta(sd, meta)
    except LegacyRenameError:
        # Only the cache write is skipped (same as before the retry wrapper);
        # the save() that follows reports the lock failure properly.
        logger.warning("[fleet] machine_id not cached: %s", sd, exc_info=True)
    except StateDirLockError:
        raise
    except Exception:
        logger.debug("[fleet] machine_id 写缓存失败 %s", cache, exc_info=True)
    if cached:
        logger.warning("[fleet] machine_id %s -> %s (source=%s reason=%s)", cached, mid, source, why)
    else:
        logger.info("[fleet] machine_id=%s (source=%s)", mid, source)
    return {"machine_id": mid, "source": source, "origin": ORIGIN_GENERATED, "changed": bool(cached),
            "previous": cached, "reason": why, "hw_fp": meta["hw_fp"]}


def node_machine_id(state_dir: Optional[Path] = None) -> str:
    """稳定的机器标识（形如 ``m-<16hex>``）。规则见模块说明。"""
    return resolve_machine_identity(state_dir)["machine_id"]


def regenerate_machine_id(state_dir: Optional[Path] = None, *, reason: str = "conflict") -> str:
    """Force a new hardware-bound id (the controller says another PC holds this one)."""
    return resolve_machine_identity(state_dir, force_new=True, reason=reason)["machine_id"]


def os_label() -> str:
    try:
        return f"{platform.system()} {platform.release()}".strip()
    except Exception:
        return "unknown"


__all__ = ["default_state_dir", "host_name", "lock_state_dir", "node_machine_id", "os_label",
           "resolve_machine_identity", "regenerate_machine_id", "identity_meta", "hardware_signals",
           "hardware_fingerprint", "fingerprint_digest", "fingerprints_match", "clean_hw_value",
           "short_machine_id", "reset_hardware_cache", "MACHINE_ID_NAME", "MACHINE_ID_HW_NAME",
           "ORIGIN_GENERATED", "ORIGIN_ADOPTED",
           "state_dir_acl_command", "state_dir_acl_commands", "state_dir_lock_plan",
           "state_dir_is_locked", "fleet_lock_steps", "parent_dir_lock_plan",
           "assign_owner_admins", "discard_untrusted_secret", "state_file_trusted",
           "StateDirLockError", "TRUSTED_OWNER_SIDS", "ENV_STATE_DIR"]
