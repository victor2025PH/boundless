# -*- coding: utf-8 -*-
"""主控 ``devices.yaml`` 与 ``DeviceManager.get_device_info`` 的单一解析入口。

供 TikTok / Facebook 路由复用，避免各处重复 ``get_device_manager`` 路径与容错语义漂移。

另导出 ``PROJECT_ROOT``、:func:`config_dir`、:func:`data_dir`、:func:`config_file`、:func:`data_file`、
:func:`scripts_dir`、:func:`templates_dir`、:func:`logs_dir`、:func:`plugins_dir`、:func:`tools_dir`，
统一项目根下常用目录（替代各处手写 ``Path(__file__).parent×3`` 与 cwd 相对路径）。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.device_control.device_manager import DeviceInfo

# ``src/host/device_registry.py`` → 上溯 3 层到 OpenClaw 项目根（与历史各模块自行数 parent 一致）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DEVICES_YAML = str(PROJECT_ROOT / "config" / "devices.yaml")


def config_dir() -> Path:
    """项目根下 ``config/`` 目录。"""
    return PROJECT_ROOT / "config"


def data_dir() -> Path:
    """项目根下 ``data/`` 目录。"""
    return PROJECT_ROOT / "data"


def config_file(name: str) -> Path:
    """``config/<name>``（``name`` 可含子路径，如 ``clash_backups/x.yaml`` — 慎用 ``..``）。"""
    return config_dir() / name


def data_file(name: str) -> Path:
    """``data/<name>``。"""
    return data_dir() / name


def scripts_dir() -> Path:
    """项目根下 ``scripts/``。"""
    return PROJECT_ROOT / "scripts"


def templates_dir() -> Path:
    """项目根下 ``templates/``。"""
    return PROJECT_ROOT / "templates"


def logs_dir() -> Path:
    """项目根下 ``logs/``。"""
    return PROJECT_ROOT / "logs"


def plugins_dir() -> Path:
    """项目根下 ``plugins/``。"""
    return PROJECT_ROOT / "plugins"


def tools_dir() -> Path:
    """项目根下 ``tools/``。"""
    return PROJECT_ROOT / "tools"


def get_device_row_strict(
    device_id: str,
    *,
    devices_yaml: Optional[str] = None,
) -> Optional["DeviceInfo"]:
    """返回 ``DeviceInfo`` 或 ``None``（无该行）；配置/IO 异常向上抛。"""
    from src.device_control.device_manager import get_device_manager

    path = devices_yaml or DEFAULT_DEVICES_YAML
    return get_device_manager(path).get_device_info(device_id)


def get_device_row_safe(
    device_id: str,
    *,
    devices_yaml: Optional[str] = None,
) -> Optional["DeviceInfo"]:
    """与 :func:`get_device_row_strict` 相同，但吞异常返回 ``None``（适合 HTTP 路由软失败）。"""
    try:
        return get_device_row_strict(device_id, devices_yaml=devices_yaml)
    except Exception:
        return None


def is_device_in_local_registry(
    device_id: str,
    *,
    devices_yaml: Optional[str] = None,
) -> bool:
    """设备是否出现在本地注册表；**管理器异常时返回 ``True``**（与历史 ``_is_local_device`` 保守语义一致）。"""
    try:
        return get_device_row_strict(device_id, devices_yaml=devices_yaml) is not None
    except Exception:
        return True
