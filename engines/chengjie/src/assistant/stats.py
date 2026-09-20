# -*- coding: utf-8 -*-
"""assistant 进程级观测计数（风格对齐 frontend_error_stats / avatar_voice_stats）。

进程口径（重启清零）：问答量/自答率/限频拦截/报障量/延迟分布。
持久口径（重启存活）走 qa_log.stats()——两者在 metrics 快照里并列给出，
ops 卡优先读持久口径，进程口径用于「本进程自启动以来」的健康观察。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class AssistantStats:
    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "queries", "answered", "miss", "errors", "rate_limited",
        # 拒答分型（2026-08-27）：no_hit=检索零命中（补语料）；
        # no_basis=检索命中但 LLM 自认答不了（NO_BASIS 哨兵）。哨兵依赖模型
        # 行为，不单独计数就无从判断它到底在不在工作。持久口径见 qa_log.stats。
        "miss_no_hit", "miss_no_basis",
        "reports", "reports_dup", "feedback_up", "feedback_down",
        "_lat_sum_ms", "_lat_n", "_lat_max_ms",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.queries = 0
        self.answered = 0
        self.miss = 0
        self.miss_no_hit = 0
        self.miss_no_basis = 0
        self.errors = 0
        self.rate_limited = 0
        self.reports = 0
        self.reports_dup = 0
        self.feedback_up = 0
        self.feedback_down = 0
        self._lat_sum_ms = 0
        self._lat_n = 0
        self._lat_max_ms = 0

    def record_query(self, *, answered: bool, latency_ms: int = 0,
                     error: bool = False, refusal: str = "") -> None:
        """``refusal`` 仅在 ``answered=False`` 时有意义：
        ``no_hit``（检索零命中）/ ``no_basis``（哨兵）。旧调用方不传＝只进
        总数 miss，分型计数保持 0（向后兼容，绝不因缺参数报错）。"""
        with self._lock:
            self._last_ts = time.time()
            self.queries += 1
            if error:
                self.errors += 1
            elif answered:
                self.answered += 1
            else:
                self.miss += 1
                if refusal == "no_hit":
                    self.miss_no_hit += 1
                elif refusal == "no_basis":
                    self.miss_no_basis += 1
            if latency_ms > 0:
                self._lat_sum_ms += int(latency_ms)
                self._lat_n += 1
                self._lat_max_ms = max(self._lat_max_ms, int(latency_ms))

    def record_rate_limited(self) -> None:
        with self._lock:
            self._last_ts = time.time()
            self.rate_limited += 1

    def record_report(self, *, dup: bool = False) -> None:
        with self._lock:
            self._last_ts = time.time()
            self.reports += 1
            if dup:
                self.reports_dup += 1

    def record_feedback(self, verdict: str) -> None:
        with self._lock:
            self._last_ts = time.time()
            if verdict == "up":
                self.feedback_up += 1
            elif verdict == "down":
                self.feedback_down += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            avg = int(self._lat_sum_ms / self._lat_n) if self._lat_n else 0
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "queries": self.queries,
                "answered": self.answered,
                "miss": self.miss,
                "miss_no_hit": self.miss_no_hit,
                "miss_no_basis": self.miss_no_basis,
                "errors": self.errors,
                "rate_limited": self.rate_limited,
                "reports": self.reports,
                "reports_dup": self.reports_dup,
                "feedback_up": self.feedback_up,
                "feedback_down": self.feedback_down,
                "avg_latency_ms": avg,
                "max_latency_ms": self._lat_max_ms,
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP assistant_queries_total Assistant ball Q&A queries",
                "# TYPE assistant_queries_total counter",
                f"assistant_queries_total {self.queries}",
                "# HELP assistant_answered_total Assistant queries answered with KB sources",
                "# TYPE assistant_answered_total counter",
                f"assistant_answered_total {self.answered}",
                "# HELP assistant_miss_total Assistant queries with no KB hit",
                "# TYPE assistant_miss_total counter",
                f"assistant_miss_total {self.miss}",
                "# HELP assistant_miss_no_hit_total Refusals: retrieval found nothing",
                "# TYPE assistant_miss_no_hit_total counter",
                f"assistant_miss_no_hit_total {self.miss_no_hit}",
                "# HELP assistant_miss_no_basis_total Refusals: LLM declared NO_BASIS",
                "# TYPE assistant_miss_no_basis_total counter",
                f"assistant_miss_no_basis_total {self.miss_no_basis}",
                "# HELP assistant_reports_total Assistant bug reports submitted",
                "# TYPE assistant_reports_total counter",
                f"assistant_reports_total {self.reports}",
                "# HELP assistant_rate_limited_total Assistant queries rejected by rate limit",
                "# TYPE assistant_rate_limited_total counter",
                f"assistant_rate_limited_total {self.rate_limited}",
            ]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.queries = 0
            self.answered = 0
            self.miss = 0
            self.miss_no_hit = 0
            self.miss_no_basis = 0
            self.errors = 0
            self.rate_limited = 0
            self.reports = 0
            self.reports_dup = 0
            self.feedback_up = 0
            self.feedback_down = 0
            self._lat_sum_ms = 0
            self._lat_n = 0
            self._lat_max_ms = 0
            self._last_ts = 0.0


_SINGLETON: Optional[AssistantStats] = None
_LOCK = threading.Lock()


def get_assistant_stats() -> AssistantStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = AssistantStats()
    return _SINGLETON


__all__ = ["AssistantStats", "get_assistant_stats"]
