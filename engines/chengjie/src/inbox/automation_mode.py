"""会话 automation_mode 解析与首条入站 bootstrap（Phase13）。

问题：``InboxStore.get_automation_mode`` 在无显式记录时回落 ``review``，而
``auto_draft`` 用 ``get_automation_mode_if_set`` + 全局 ``automation_mode: auto_ai``——
两条口径不一致 → 新好友 UI 显示「人审」、``inbox_will_autosend`` 不让位 System Z、
与运营「好友消息全自动」方针冲突。

本模块提供单一事实源：
- ``resolve_automation_mode``：显式档位 > 全局 auto_draft.automation_mode
- ``maybe_bootstrap_automation_mode``：首条入站时把全局档位持久化（仅无显式记录时）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.inbox.store import AUTOMATION_MODES, _DEFAULT_AUTOMATION_MODE


def _auto_draft_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (((config or {}).get("inbox") or {}).get("auto_draft") or {})


def global_automation_mode_from_config(config: Optional[Dict[str, Any]]) -> str:
    """读 ``inbox.auto_draft.automation_mode``（缺省 auto_ai）。"""
    mode = str(_auto_draft_cfg(config).get("automation_mode") or "auto_ai").lower()
    return mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE


def bootstrap_enabled_from_config(config: Optional[Dict[str, Any]]) -> bool:
    """是否在新会话首条入站时持久化全局档位。"""
    ad = _auto_draft_cfg(config)
    if "bootstrap_automation_mode" in ad:
        return bool(ad.get("bootstrap_automation_mode"))
    # 默认：全局为 auto_ai 时自动 bootstrap（好友全自动方针）
    return global_automation_mode_from_config(config) == "auto_ai"


def resolve_automation_mode(
    store: Any,
    conversation_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """有效档位：坐席/UI 显式设置 > 全局 auto_draft.automation_mode。"""
    if store is not None and conversation_id:
        explicit = store.get_automation_mode_if_set(conversation_id)
        if explicit is not None:
            return explicit
    return global_automation_mode_from_config(config)


def maybe_bootstrap_automation_mode(
    store: Any,
    conversation_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """首条入站 bootstrap：无显式档位且开关开 → 持久化全局档位并返回。"""
    if not store or not conversation_id:
        return global_automation_mode_from_config(config)
    explicit = store.get_automation_mode_if_set(conversation_id)
    if explicit is not None:
        return explicit
    mode = global_automation_mode_from_config(config)
    if bootstrap_enabled_from_config(config) and mode in AUTOMATION_MODES:
        store.set_automation_mode(conversation_id, mode)
        try:
            from src.inbox.automation_mode_stats import record_bootstrap
            _plat = str(conversation_id or "").split(":", 1)[0]
            record_bootstrap(platform=_plat, conversation_id=conversation_id)
        except Exception:
            pass
    return mode


__all__ = [
    "global_automation_mode_from_config",
    "bootstrap_enabled_from_config",
    "resolve_automation_mode",
    "maybe_bootstrap_automation_mode",
]
