"""automation_mode bootstrap 观测（Phase14，进程内计数）。"""
from __future__ import annotations

import threading
from typing import Any, Dict

_lock = threading.Lock()
_bootstrap_total = 0
_last_bootstrap: Dict[str, Any] = {}


def record_bootstrap(*, platform: str = "", conversation_id: str = "") -> None:
    global _bootstrap_total, _last_bootstrap
    with _lock:
        _bootstrap_total += 1
        _last_bootstrap = {
            "platform": str(platform or ""),
            "conversation_id": str(conversation_id or ""),
        }


def metrics_snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "bootstrap_total": _bootstrap_total,
            "last": dict(_last_bootstrap),
        }


__all__ = ["record_bootstrap", "metrics_snapshot"]
