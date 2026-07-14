# -*- coding: utf-8 -*-
"""分析数据快照存储 — 设备/任务趋势数据收集与查询。"""
import json
import time
import logging

from .device_registry import DEFAULT_DEVICES_YAML, config_file

logger = logging.getLogger(__name__)

_analytics_history_path = config_file("analytics_history.json")
_analytics_cache: dict = {"device_snapshots": [], "task_snapshots": []}


def load_analytics_history():
    global _analytics_cache
    if _analytics_history_path.exists():
        try:
            with open(_analytics_history_path, "r", encoding="utf-8") as f:
                _analytics_cache = json.load(f)
        except Exception:
            pass


def _save_analytics_history():
    try:
        _analytics_history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_analytics_history_path, "w", encoding="utf-8") as f:
            json.dump(_analytics_cache, f, ensure_ascii=False)
    except Exception:
        pass


def record_analytics_snapshot():
    """Called periodically to record device/task statistics for trend charts."""
    from src.device_control.device_manager import get_device_manager
    from . import task_store

    _config_path = DEFAULT_DEVICES_YAML
    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        manager = get_device_manager(_config_path)
        devices = manager.get_all_devices()
        online = sum(1 for d in devices if d.is_online)
        total = len(devices)
        _analytics_cache.setdefault("device_snapshots", []).append(
            {"ts": ts, "online": online, "total": total}
        )
        if len(_analytics_cache["device_snapshots"]) > 2016:
            _analytics_cache["device_snapshots"] = _analytics_cache["device_snapshots"][-2016:]
    except Exception:
        pass
    try:
        tasks = task_store.list_tasks(limit=9999)
        success = sum(1 for t in tasks if getattr(t, "status", "") in ("completed", "success"))
        failed = sum(1 for t in tasks if getattr(t, "status", "") == "failed")
        _analytics_cache.setdefault("task_snapshots", []).append(
            {"ts": ts, "total": len(tasks), "success": success, "failed": failed}
        )
        if len(_analytics_cache["task_snapshots"]) > 2016:
            _analytics_cache["task_snapshots"] = _analytics_cache["task_snapshots"][-2016:]
    except Exception:
        pass
    _save_analytics_history()


def range_to_count(range_str: str) -> int:
    if range_str == "1h":
        return 12
    if range_str == "6h":
        return 72
    if range_str == "24h":
        return 288
    if range_str == "7d":
        return 2016
    return 288


def get_analytics_cache() -> dict:
    """Return the analytics cache dict (for use by trend endpoints)."""
    return _analytics_cache
