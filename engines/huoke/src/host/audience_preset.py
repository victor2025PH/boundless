# -*- coding: utf-8 -*-
"""人群预设 — 合并到任务 params（显式字段覆盖预设）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_PRESET_PATH = config_file("audience_presets.yaml")

_PRESET_KEYS = frozenset({"audience_preset", "preset_id", "audience_preset_id"})


def _post_process(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {"version": 1, "presets": []}


_CACHE = YamlCache(
    path=_PRESET_PATH,
    defaults={"version": 1, "presets": []},
    post_process=_post_process,
    log_label="audience_presets.yaml",
    logger=logger,
)


def load_presets(force_reload: bool = False) -> Dict[str, Any]:
    return _CACHE.get(force_reload=force_reload)


def audience_presets_etag() -> Tuple[str, int, float]:
    """
    用于客户端条件请求：YAML version + 文件 mtime，避免配置未改时重复传输 presets 列表。
    返回 (etag, yaml_version, mtime_seconds)。
    """
    data = load_presets()
    ver = int(data.get("version") or 1)
    mtime = _CACHE.mtime()
    etag = f"{ver}:{int(mtime * 1000)}"
    return etag, ver, mtime


def list_presets() -> List[Dict[str, Any]]:
    data = load_presets()
    out = []
    for p in data.get("presets") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        out.append({
            "id": p["id"],
            "label": p.get("label", p["id"]),
            "description": p.get("description", ""),
            "tags": p.get("tags") or [],
            "apply_keys": list((p.get("apply") or {}).keys()),
        })
    return out


def _get_preset_by_id(preset_id: str) -> Optional[Dict[str, Any]]:
    pid = (preset_id or "").strip().lower()
    if not pid:
        return None
    for p in load_presets().get("presets") or []:
        if isinstance(p, dict) and str(p.get("id", "")).lower() == pid:
            return p
    return None


def _resolve_apply_for_task(apply: Dict[str, Any], task_type: str) -> Dict[str, Any]:
    """apply 下最长前缀匹配 task_type。"""
    if not apply or not task_type:
        return {}
    if task_type in apply and isinstance(apply[task_type], dict):
        return dict(apply[task_type])
    best_k = ""
    best_d: Dict[str, Any] = {}
    best_len = -1
    for prefix, val in apply.items():
        if not isinstance(val, dict):
            continue
        if task_type.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best_k = prefix
            best_d = dict(val)
    if best_k:
        return best_d
    return {}


def merge_audience_preset(task_type: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    若 params 含 audience_preset / preset_id，则合并预设字段后移除保留键。
    返回 (新 params, 信息性说明列表)。
    """
    notes: List[str] = []
    raw = dict(params)
    pid = None
    for k in _PRESET_KEYS:
        if k in raw and raw[k] not in (None, ""):
            pid = raw[k]
            break
    if pid is None:
        return raw, notes

    preset = _get_preset_by_id(str(pid))
    if not preset:
        notes.append(f"audience_preset 未找到: {pid}")
        logger.warning("[audience_preset] unknown id=%s", pid)
        for k in _PRESET_KEYS:
            raw.pop(k, None)
        return raw, notes

    apply = preset.get("apply") or {}
    fragment = _resolve_apply_for_task(apply, task_type)
    if not fragment:
        notes.append(f"预设 {preset.get('id')} 对任务 {task_type} 无适用片段")
    # 显式 params 覆盖预设
    merged = {**fragment, **{k: v for k, v in raw.items() if k not in _PRESET_KEYS}}
    for k in _PRESET_KEYS:
        merged.pop(k, None)
    merged["_audience_preset"] = preset.get("id")
    notes.append(f"已应用预设: {preset.get('id')}")
    logger.info("[audience_preset] task_type=%s preset=%s keys=%s", task_type, preset.get("id"), list(fragment.keys()))
    return merged, notes
