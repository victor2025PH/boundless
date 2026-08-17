"""首启向导（/welcome）进度持久化 + 开关判定（WP-2 2026-08）。

落点＝实例数据根 config 目录（``licensing.data_paths.config_dir()``，与
``trial_claim.json`` / ``local_trial.json`` 同族）：桌面打包态可写、升级不丢、
生产双实例互不串（各自数据根）。文件 ``welcome_onboarding.json``：

    {"steps": {"license": {"done": true, "skipped": false, "ts": ...}, ...},
     "completed": false, "updated_at": ...}

设计约束：
- 读写全程绝不抛（向导是首启体验路径，状态文件坏＝当作全新，不阻塞任何页面）；
- 写入原子（tmp + replace），防半写 JSON 咬到下次启动；
- ``welcome_pending`` 是登录落地判据（auth_user_routes 消费）——flag 关 /
  已完成 / 任何异常 → False，登录流程零风险。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "welcome_onboarding.json"

#: 五步向导的步骤 id（与 /welcome 前端 STEPS、i18n 键后缀一致，勿单独改一边）
STEP_IDS = ("license", "channel", "persona", "automation", "test_message")

#: 单步附加 data 的序列化尺寸上限（防前端塞大对象撑爆状态文件）
_MAX_DATA_BYTES = 4096


def onboarding_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """``onboarding.enabled``（基线 false；桌面新装经 cloud_light 预设档播种 true）。"""
    try:
        return bool(((config or {}).get("onboarding") or {}).get("enabled"))
    except Exception:
        return False


def _state_path() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / STATE_FILENAME


def _default_state() -> Dict[str, Any]:
    return {"steps": {}, "completed": False, "updated_at": 0}


def load_state() -> Dict[str, Any]:
    """读进度（缺文件/坏 JSON/形状不对 → 全新默认态，绝不抛）。"""
    try:
        p = _state_path()
        if not p.exists():
            return _default_state()
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        steps = data.get("steps")
        return {
            "steps": dict(steps) if isinstance(steps, dict) else {},
            "completed": bool(data.get("completed")),
            "updated_at": int(data.get("updated_at") or 0),
        }
    except Exception:
        logger.debug("[onboarding] 状态文件读取失败（按全新处理）", exc_info=True)
        return _default_state()


def save_state(state: Dict[str, Any]) -> bool:
    """原子落盘（tmp + replace）。失败只记日志返回 False。"""
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        logger.warning("[onboarding] 状态落盘失败", exc_info=True)
        return False


def mark_step(
    step: str,
    *,
    done: Optional[bool] = None,
    skipped: Optional[bool] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """更新单步进度并返回最新全量状态。未知 step 抛 ValueError（路由层转 400）。"""
    if step not in STEP_IDS:
        raise ValueError(f"unknown onboarding step: {step!r}")
    st = load_state()
    entry = dict(st["steps"].get(step) or {})
    if done is not None:
        entry["done"] = bool(done)
    if skipped is not None:
        entry["skipped"] = bool(skipped)
    if isinstance(data, dict):
        try:
            if len(json.dumps(data, ensure_ascii=False)) <= _MAX_DATA_BYTES:
                entry["data"] = data
        except Exception:
            pass
    entry["ts"] = int(time.time())
    st["steps"][step] = entry
    st["updated_at"] = int(time.time())
    save_state(st)
    return st


def set_completed(flag: bool = True) -> Dict[str, Any]:
    """置完成位（完成后登录不再直达向导；页面本身仍可访问）。"""
    st = load_state()
    st["completed"] = bool(flag)
    st["updated_at"] = int(time.time())
    save_state(st)
    return st


def welcome_pending(config: Optional[Dict[str, Any]]) -> bool:
    """登录落地判据：向导启用且未完成。任何异常 → False（登录流程零风险）。"""
    try:
        if not onboarding_enabled(config):
            return False
        return not bool(load_state().get("completed"))
    except Exception:
        return False


__all__ = [
    "STATE_FILENAME",
    "STEP_IDS",
    "onboarding_enabled",
    "load_state",
    "save_state",
    "mark_step",
    "set_completed",
    "welcome_pending",
]
