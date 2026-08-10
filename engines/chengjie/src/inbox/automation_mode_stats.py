"""automation_mode bootstrap / 接管 / 自动接回 观测（进程内计数）。"""
from __future__ import annotations

import threading
from typing import Any, Dict

_lock = threading.Lock()
_bootstrap_total = 0
_last_bootstrap: Dict[str, Any] = {}
_takeover_total = 0
_rearm_total = 0
_last_takeover_cid = ""


def record_bootstrap(*, platform: str = "", conversation_id: str = "") -> None:
    global _bootstrap_total, _last_bootstrap
    with _lock:
        _bootstrap_total += 1
        _last_bootstrap = {
            "platform": str(platform or ""),
            "conversation_id": str(conversation_id or ""),
        }


def record_takeover(*, conversation_id: str = "") -> None:
    """坐席手动出站触发「接管即静音」一次（takeover_rearm.record_agent_takeover）。"""
    global _takeover_total, _last_takeover_cid
    with _lock:
        _takeover_total += 1
        _last_takeover_cid = str(conversation_id or "")


def record_rearm(*, count: int = 1) -> None:
    """自动接回恢复了 N 个会话（takeover_rearm.sweep_takeover_rearm）。"""
    global _rearm_total
    with _lock:
        _rearm_total += max(0, int(count or 0))


def metrics_snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "bootstrap_total": _bootstrap_total,
            "last": dict(_last_bootstrap),
            "takeover_total": _takeover_total,
            "rearm_total": _rearm_total,
            "last_takeover_cid": _last_takeover_cid,
        }


__all__ = [
    "record_bootstrap",
    "record_takeover",
    "record_rearm",
    "metrics_snapshot",
]
