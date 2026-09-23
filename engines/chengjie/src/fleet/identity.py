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
import uuid
from pathlib import Path
from typing import Optional

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


def node_machine_id(state_dir: Optional[Path] = None) -> str:
    """稳定的机器标识（形如 ``m-<16hex>``）。"""
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    cache = sd / "machine_id"
    try:
        cached = cache.read_text(encoding="utf-8").strip()
        if cached:
            return cached
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
        sd.mkdir(parents=True, exist_ok=True)
        cache.write_text(mid, encoding="utf-8")
    except Exception:
        logger.debug("[fleet] machine_id 写缓存失败 %s", cache, exc_info=True)
    logger.info("[fleet] machine_id=%s (source=%s)", mid, source)
    return mid


def os_label() -> str:
    try:
        return f"{platform.system()} {platform.release()}".strip()
    except Exception:
        return "unknown"


__all__ = ["default_state_dir", "host_name", "node_machine_id", "os_label", "ENV_STATE_DIR"]
