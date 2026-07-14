"""Telegram 原生通话观测（进程级单例，风格对齐 realtime_voice_stats）。

回答运营四问：
  - 有人打进来吗、接了多少（attempts / accepted / connect_rate）；
  - 拒接都因为啥（by_decline_reason：stranger/low_intimacy/busy/quiet_hours…）；
  - 通得顺吗（时长分布 / 挂断原因 / 当前与峰值并发）；
  - 拟人与安全动作（filler/backchannel 次数、危机升级次数）。

**绝不记录音频/转写原文**，只记计数与时长。best-effort：任何异常吞掉，绝不阻塞通话。
``dump`` 供 /api/workspace/metrics，``dump_prom`` 供 Prometheus，``reset`` 供测试。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

# 拒接原因白名单（对齐 core.decide_incoming_call 的 reason；防维度爆炸）。
_DECLINE_REASONS = (
    "disabled", "not_private", "kill_switch", "not_auto_ai", "stranger",
    "language_unsupported", "low_intimacy", "quiet_hours", "host_cold", "busy",
    # 账号级通话预算/健康闸（evaluate_call_budget）
    "account_unhealthy", "daily_calls_exhausted", "daily_minutes_exhausted",
    "other",
)
_END_REASONS = ("normal", "relay_error", "answer_failed", "brain_failed", "other")
_SAFETY_LEVELS = ("elevated", "severe")


class CallStats:
    """原生通话计数聚合（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_attempts", "_accepted", "_declined", "_by_decline", "_connected",
        "_active", "_peak_active", "_dur_total", "_dur_count", "_dur_max", "_last_dur",
        "_by_end_reason", "_filler", "_backchannel", "_safety", "_compensated",
        "_started_at", "_last_call_ts", "_last_end_ts",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempts = 0
        self._accepted = 0
        self._declined = 0
        self._by_decline: Dict[str, int] = {}
        self._connected = 0
        self._active = 0
        self._peak_active = 0
        self._dur_total = 0.0
        self._dur_count = 0
        self._dur_max = 0.0
        self._last_dur = 0.0
        self._by_end_reason: Dict[str, int] = {}
        self._filler = 0
        self._backchannel = 0
        self._safety: Dict[str, int] = {}
        self._compensated = 0        # 拒接后发出补偿消息的次数（绝不冷场的量化）
        self._started_at = time.time()
        self._last_call_ts = 0.0
        self._last_end_ts = 0.0

    def incoming(self) -> None:
        """一通来电到达（已过 enabled 闸）。"""
        with self._lock:
            self._attempts += 1
            self._last_call_ts = time.time()

    def decided(self, action: str, reason: str, *, compensated: bool = False) -> None:
        """记一次接听决策结果。action ∈ accept|decline_compensate|decline_silent。"""
        with self._lock:
            if action == "accept":
                self._accepted += 1
            else:
                self._declined += 1
                r = reason if reason in _DECLINE_REASONS else "other"
                self._by_decline[r] = self._by_decline.get(r, 0) + 1
                if compensated:
                    self._compensated += 1

    def connected(self) -> None:
        with self._lock:
            self._connected += 1
            self._active += 1
            if self._active > self._peak_active:
                self._peak_active = self._active

    def ended(self, reason: str = "normal", *, was_connected: bool = False,
              duration_sec: float = 0.0) -> None:
        r = reason if reason in _END_REASONS else "other"
        with self._lock:
            self._by_end_reason[r] = self._by_end_reason.get(r, 0) + 1
            self._last_end_ts = time.time()
            if was_connected:
                self._active = max(0, self._active - 1)
                d = float(duration_sec or 0.0)
                if d > 0:
                    self._dur_total += d
                    self._dur_count += 1
                    self._last_dur = d
                    if d > self._dur_max:
                        self._dur_max = d

    def humanize(self, *, filler: int = 0, backchannel: int = 0) -> None:
        with self._lock:
            self._filler += max(0, int(filler))
            self._backchannel += max(0, int(backchannel))

    def safety_escalation(self, level: str) -> None:
        if level in _SAFETY_LEVELS:
            with self._lock:
                self._safety[level] = self._safety.get(level, 0) + 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            att = self._attempts
            return {
                "started_at": self._started_at,
                "last_call_ts": self._last_call_ts,
                "last_end_ts": self._last_end_ts,
                "attempts": int(att),
                "accepted": int(self._accepted),
                "declined": int(self._declined),
                "accept_rate": round(self._accepted / att, 4) if att else 0,
                "connected": int(self._connected),
                "compensated": int(self._compensated),
                "by_decline_reason": dict(sorted(self._by_decline.items())),
                "active": int(self._active),
                "peak_active": int(self._peak_active),
                "avg_duration_sec": round(self._dur_total / self._dur_count, 1) if self._dur_count else 0,
                "max_duration_sec": round(self._dur_max, 1),
                "last_duration_sec": round(self._last_dur, 1),
                "by_end_reason": dict(sorted(self._by_end_reason.items())),
                "filler_count": int(self._filler),
                "backchannel_count": int(self._backchannel),
                "safety_escalations": dict(sorted(self._safety.items())),
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP tg_call_attempts_total Native call incoming attempts",
            "# TYPE tg_call_attempts_total counter",
            "# HELP tg_call_accepted_total Native calls accepted",
            "# TYPE tg_call_accepted_total counter",
            "# HELP tg_call_declined_total Native calls declined by reason",
            "# TYPE tg_call_declined_total counter",
            "# HELP tg_call_active Native calls in progress",
            "# TYPE tg_call_active gauge",
            "# HELP tg_call_ended_total Native calls ended by reason",
            "# TYPE tg_call_ended_total counter",
            "# HELP tg_call_humanize_total Humanizer actions by kind",
            "# TYPE tg_call_humanize_total counter",
            "# HELP tg_call_safety_escalation_total Crisis escalations by level",
            "# TYPE tg_call_safety_escalation_total counter",
        ]
        with self._lock:
            lines.append(f"tg_call_attempts_total {self._attempts}")
            lines.append(f"tg_call_accepted_total {self._accepted}")
            for reason, n in sorted(self._by_decline.items()):
                lines.append(f'tg_call_declined_total{{reason="{_esc(reason)}"}} {int(n)}')
            lines.append(f"tg_call_active {self._active}")
            for reason, n in sorted(self._by_end_reason.items()):
                lines.append(f'tg_call_ended_total{{reason="{_esc(reason)}"}} {int(n)}')
            lines.append(f'tg_call_humanize_total{{kind="filler"}} {self._filler}')
            lines.append(f'tg_call_humanize_total{{kind="backchannel"}} {self._backchannel}')
            for level, n in sorted(self._safety.items()):
                lines.append(f'tg_call_safety_escalation_total{{level="{_esc(level)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._attempts = 0
            self._accepted = 0
            self._declined = 0
            self._by_decline.clear()
            self._connected = 0
            self._active = 0
            self._peak_active = 0
            self._dur_total = 0.0
            self._dur_count = 0
            self._dur_max = 0.0
            self._last_dur = 0.0
            self._by_end_reason.clear()
            self._filler = 0
            self._backchannel = 0
            self._safety.clear()
            self._compensated = 0
            self._last_call_ts = 0.0
            self._last_end_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[CallStats] = None
_LOCK = threading.Lock()


def get_call_stats() -> CallStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = CallStats()
    return _SINGLETON


__all__ = ["CallStats", "get_call_stats"]
