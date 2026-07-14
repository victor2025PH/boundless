"""companion proactive_topic 派发观测（Phase14）。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

_lock = threading.Lock()
_ticks = 0
_planned_sum = 0
_sent_sum = 0
_voice_sent = 0
_voice_foreign_sent = 0
_photo_sent = 0
_last: Dict[str, Any] = {}


def record_voice(*, foreign: bool = False) -> None:
    global _voice_sent, _voice_foreign_sent
    with _lock:
        if foreign:
            _voice_foreign_sent += 1
        else:
            _voice_sent += 1


def record_photo() -> None:
    """主动生活照发出计数（Phase16）。"""
    global _photo_sent
    with _lock:
        _photo_sent += 1


def record_tick(*, planned: int, sent: int, dry_run: bool = False) -> None:
    global _ticks, _planned_sum, _sent_sum, _last
    with _lock:
        _ticks += 1
        _planned_sum += max(0, int(planned))
        _sent_sum += max(0, int(sent))
        _last = {
            "planned": int(planned),
            "sent": int(sent),
            "dry_run": bool(dry_run),
            "ts": time.time(),
        }


def metrics_snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "ticks": _ticks,
            "planned_sum": _planned_sum,
            "sent_sum": _sent_sum,
            "voice_sent": _voice_sent,
            "voice_foreign_sent": _voice_foreign_sent,
            "photo_sent": _photo_sent,
            "last_tick": dict(_last),
        }


__all__ = ["record_tick", "record_voice", "record_photo", "metrics_snapshot"]
