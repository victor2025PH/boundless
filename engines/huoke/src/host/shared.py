# -*- coding: utf-8 -*-
"""共享工具函数和常量 — 供路由模块使用，避免循环导入。"""

import logging

from .device_registry import (
    DEFAULT_DEVICES_YAML,
    PROJECT_ROOT,
    data_dir,
    scripts_dir,
)

logger = logging.getLogger(__name__)

# 项目路径（与 device_registry 单一事实源一致）
CONFIG_PATH = DEFAULT_DEVICES_YAML
SCRIPTS_DIR = scripts_dir()
DATA_DIR = data_dir()


def get_device_manager(config_path: str = None):
    """延迟获取 DeviceManager 单例。"""
    from ..device_control.device_manager import get_device_manager as _gdm
    return _gdm(config_path or CONFIG_PATH)


def get_task_store():
    """延迟获取 task_store 模块。"""
    from . import task_store
    return task_store


def get_worker_pool():
    """延迟获取 WorkerPool。"""
    from .worker_pool import get_worker_pool as _gwp
    return _gwp()


import threading as _threading
_device_recovery_locks: dict = {}
_locks_lock = _threading.Lock()


def get_device_recovery_lock(device_id: str) -> _threading.Lock:
    """获取设备级恢复互斥锁，避免 Watchdog 和 HealthMonitor 竞争。"""
    with _locks_lock:
        if device_id not in _device_recovery_locks:
            _device_recovery_locks[device_id] = _threading.Lock()
        return _device_recovery_locks[device_id]


def audit(action: str, target: str = "", detail: str = "", source: str = "api"):
    """记录审计日志。"""
    try:
        from .database import get_db
        import time
        db = get_db()
        db.execute(
            "INSERT INTO audit_logs (timestamp, action, target, detail, source) VALUES (?,?,?,?,?)",
            (time.time(), action, target, detail, source)
        )
        db.commit()
    except Exception as e:
        logger.debug("审计日志写入失败: %s", e)
