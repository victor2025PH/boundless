"""入站视频理解观测（进程级单例，对齐 vision/avatar_voice stats 风格）。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict


class InboundVideoStats:
    __slots__ = ("_lock", "_attempts", "_outcomes", "_started_at", "_last_ts")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempts = 0
        self._outcomes: Dict[str, int] = {}
        self._started_at = time.time()
        self._last_ts = 0.0

    def record_attempt(self) -> None:
        with self._lock:
            self._attempts += 1
            self._last_ts = time.time()

    def record_outcome(self, name: str) -> None:
        key = str(name or "unknown").strip() or "unknown"
        with self._lock:
            self._outcomes[key] = self._outcomes.get(key, 0) + 1
            self._last_ts = time.time()

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            outcomes = dict(self._outcomes)
            attempts = int(self._attempts)
            ok = int(outcomes.get("ok", 0))
            return {
                "active": attempts > 0,
                "attempts": attempts,
                "ok": ok,
                "success_rate": round(ok / attempts, 4) if attempts else 0.0,
                "outcomes": outcomes,
                "started_at": self._started_at,
                "last_ts": self._last_ts,
            }

    def dump_prom(self) -> str:
        d = self.dump()
        lines = [
            f"inbound_video_attempts_total {d['attempts']}",
            f"inbound_video_ok_total {d['ok']}",
        ]
        for k, v in sorted((d.get("outcomes") or {}).items()):
            safe = k.replace("-", "_")
            lines.append(f"inbound_video_outcome_{safe}_total {v}")
        return "\n".join(lines) + "\n"


_singleton: InboundVideoStats | None = None
_singleton_lock = threading.Lock()


def get_inbound_video_stats() -> InboundVideoStats:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = InboundVideoStats()
    return _singleton


__all__ = ["InboundVideoStats", "get_inbound_video_stats"]
