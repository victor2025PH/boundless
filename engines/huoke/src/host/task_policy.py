# -*- coding: utf-8 -*-
"""任务执行策略：与 config/task_execution_policy.yaml 同步，供 API / 调度器 / 健康监控读取。

缓存与热加载均委托给 `src.host._yaml_cache.YamlCache`，改完 YAML 无需重启进程。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_policy_path = config_file("task_execution_policy.yaml")

_DEFAULTS: Dict[str, Any] = {
    "manual_execution_only": False,
    "disable_db_scheduler": False,
    "disable_json_scheduled_jobs": False,
    "disable_reconnect_task_recovery": True,
    "disable_auto_wallpaper_thread_on_startup": False,
    "disable_auto_tiktok_check_inbox": False,
    "disable_executor_inbox_followup": False,
    "disable_strategy_optimizer": False,
    "disable_event_driven_auto_tasks": False,
    "normalize_task_params": True,
    "strict_task_params": False,
}


def _post_process(raw: Any) -> Dict[str, Any]:
    """合并默认值；manual_execution_only=true 时补齐各 disable_* 默认，并打印一行摘要。"""
    data = dict(_DEFAULTS)
    if isinstance(raw, dict):
        data.update(raw)

    if data.get("manual_execution_only"):
        data.setdefault("disable_db_scheduler", True)
        data.setdefault("disable_json_scheduled_jobs", True)
        data.setdefault("disable_reconnect_task_recovery", True)
        data.setdefault("disable_auto_wallpaper_thread_on_startup", True)
        data.setdefault("disable_executor_inbox_followup", True)
        data.setdefault("disable_strategy_optimizer", True)
        data.setdefault("disable_event_driven_auto_tasks", True)

    logger.info(
        "任务策略: manual_only=%s gate_mode=%s db_sched=%s json_jobs=%s recover=%s wp_thread=%s preflight=%s geo=%s",
        data.get("manual_execution_only"),
        (data.get("gate_mode") or "strict"),
        not data.get("disable_db_scheduler"),
        not data.get("disable_json_scheduled_jobs"),
        not data.get("disable_reconnect_task_recovery"),
        not data.get("disable_auto_wallpaper_thread_on_startup"),
        (data.get("manual_gate") or {}).get("enforce_preflight"),
        (data.get("manual_gate") or {}).get("enforce_geo_for_risky"),
    )
    return data


_CACHE = YamlCache(
    path=_policy_path,
    defaults=_DEFAULTS,
    post_process=_post_process,
    log_label="task_execution_policy.yaml",
    logger=logger,
)


def load_task_execution_policy(force_reload: bool = False) -> Dict[str, Any]:
    """加载当前任务执行策略，自动 mtime 热加载。"""
    return _CACHE.get(force_reload=force_reload)


def policy_blocks_db_scheduler() -> bool:
    return bool(load_task_execution_policy().get("disable_db_scheduler"))


def policy_blocks_json_scheduled_jobs() -> bool:
    return bool(load_task_execution_policy().get("disable_json_scheduled_jobs"))


def policy_blocks_reconnect_recovery() -> bool:
    return bool(load_task_execution_policy().get("disable_reconnect_task_recovery"))


def policy_blocks_auto_wallpaper_thread() -> bool:
    return bool(load_task_execution_policy().get("disable_auto_wallpaper_thread_on_startup"))


def policy_blocks_auto_tiktok_check_inbox() -> bool:
    """无人值守自动查收件箱（JSON/DB 定时、自动监控）；不影响手动批量收件箱。"""
    p = load_task_execution_policy()
    if p.get("disable_auto_tiktok_check_inbox"):
        return True
    return False


def policy_blocks_executor_inbox_followup() -> bool:
    """收件箱成功后是否禁止再派生 3 分钟跟进任务。"""
    return bool(load_task_execution_policy().get("disable_executor_inbox_followup"))


def policy_blocks_strategy_optimizer() -> bool:
    """是否禁止启动策略优化器后台线程。"""
    return bool(load_task_execution_policy().get("disable_strategy_optimizer"))


def policy_blocks_event_driven_auto_tasks() -> bool:
    """是否禁止 TikTok 事件链自动建任务（高意向/drip/跨平台等）。"""
    return bool(load_task_execution_policy().get("disable_event_driven_auto_tasks"))


def reload_policy() -> Dict[str, Any]:
    """强制清缓存重读（供 POST /task-dispatch/policy/reload 使用）。"""
    return _CACHE.reload()


def policy_mtime() -> float:
    """供 API / 前端显示「最后加载时间」。"""
    return _CACHE.mtime()
