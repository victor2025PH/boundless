# -*- coding: utf-8 -*-
"""出口档案加载 — 与 config/exit_profiles.yaml 对应。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_DEFAULT_PATH = config_file("exit_profiles.yaml")


def _post_process(raw: Any) -> List[Dict[str, Any]]:
    """提取 profiles 列表（只保留包含 id 的项）。"""
    profiles: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        items = raw.get("profiles")
        if isinstance(items, list):
            for p in items:
                if isinstance(p, dict) and p.get("id"):
                    profiles.append(p)
    return profiles


_CACHE = YamlCache(
    path=_DEFAULT_PATH,
    defaults=[],
    post_process=_post_process,
    log_label="exit_profiles.yaml",
    logger=logger,
)


def load_exit_profiles(force_reload: bool = False) -> List[Dict[str, Any]]:
    return _CACHE.get(force_reload=force_reload)


def get_profile_by_id(profile_id: str) -> Optional[Dict[str, Any]]:
    pid = (profile_id or "").strip().lower()
    for p in load_exit_profiles():
        if str(p.get("id", "")).lower() == pid:
            return p
    return None


def summary_for_api() -> Dict[str, Any]:
    """供 /task-dispatch/exit-profiles 使用。"""
    return {
        "ok": True,
        "profiles": load_exit_profiles(),
        "config_path": str(_DEFAULT_PATH),
    }
