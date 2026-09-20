# -*- coding: utf-8 -*-
"""assistant 限频：进程内每用户滑动窗计数（纯函数核心 + 极薄状态壳）。

刻意不用 send_dedup（那是幂等语义）也不用外部存储——助手问答是
进程内低频调用，重启清零可接受；防的是「单人刷屏烧 LLM 成本」。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class RateVerdict:
    allowed: bool
    reason: str = ""  # "" | "per_min" | "per_day"
    retry_after_sec: int = 0


def evaluate_window(
    stamps: list[float],
    now: float,
    per_min: int,
    per_day: int,
) -> RateVerdict:
    """纯函数：给定该用户历史时间戳与限额，判定本次是否放行。

    limits <=0 表示该维度不限。stamps 无需预清理（本函数自行按窗过滤）。
    """
    minute_ago = now - 60.0
    day_ago = now - 86400.0
    in_min = [t for t in stamps if t > minute_ago]
    in_day = [t for t in stamps if t > day_ago]
    if per_min > 0 and len(in_min) >= per_min:
        oldest = min(in_min)
        return RateVerdict(False, "per_min", max(1, int(oldest + 60.0 - now) + 1))
    if per_day > 0 and len(in_day) >= per_day:
        oldest = min(in_day)
        return RateVerdict(False, "per_day", max(1, int(oldest + 86400.0 - now) + 1))
    return RateVerdict(True)


class AssistantRateLimiter:
    """进程级状态壳：user_key → 最近 24h 时间戳（自动修剪防膨胀）。"""

    _MAX_USERS = 500  # 防恶意撑爆：超限时剔除最久未活跃用户

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamps: dict[str, list[float]] = {}

    def check_and_record(
        self,
        user_key: str,
        per_min: int,
        per_day: int,
        now: float | None = None,
    ) -> RateVerdict:
        ts = time.time() if now is None else now
        key = str(user_key or "anon")
        with self._lock:
            stamps = self._stamps.get(key, [])
            verdict = evaluate_window(stamps, ts, per_min, per_day)
            if verdict.allowed:
                day_ago = ts - 86400.0
                stamps = [t for t in stamps if t > day_ago]
                stamps.append(ts)
                self._stamps[key] = stamps
                if len(self._stamps) > self._MAX_USERS:
                    victim = min(
                        self._stamps.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0
                    )[0]
                    if victim != key:
                        self._stamps.pop(victim, None)
            return verdict


_LIMITER: AssistantRateLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def get_rate_limiter() -> AssistantRateLimiter:
    global _LIMITER
    if _LIMITER is None:
        with _LIMITER_LOCK:
            if _LIMITER is None:
                _LIMITER = AssistantRateLimiter()
    return _LIMITER
