# -*- coding: utf-8 -*-
"""案例中心观测计数（进程级单例，风格对齐 bazi_stats / outbound_translation_stats）。

记「自本次启动以来」的立案/结案/升级/告警发射计数与结案时长——回答运营两个问题：
「AI 这段时间替我盯出了多少事」「事被处理得多快」。live 状态（当前未结案数）
不在这里——那由 ``/api/cases/active`` 按 ContextStore 实况给出，两者口径互补。

全部调用点 best-effort（异常吞掉），绝不影响立案/结案主流程。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict


def _safe_label(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(s or ""))


class CaseStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.opened_total = 0
        self.opened_by_source: Dict[str, int] = {}
        self.upgraded_total = 0
        self.repeat_signals = 0
        self.closed_total = 0
        self.close_hours_sum = 0.0
        self.close_hours_n = 0
        # P3：结案分桶（进程口径；回答「媒体质疑结得快不快 / 误报多不多」）
        self.closed_by_source: Dict[str, int] = {}
        self.closed_by_resolution: Dict[str, int] = {}
        self.close_hours_by_source: Dict[str, float] = {}
        self.close_n_by_source: Dict[str, int] = {}
        # P5：误报静默拦下的开案数（回答「勾的静默有没有真在帮忙挡噪音」）
        self.suppressed_total = 0
        self.suppressed_by_source: Dict[str, int] = {}
        self.alerts_emitted = 0

    # ── 记录 ────────────────────────────────────────────────────────────────
    def record_opened(self, source: str) -> None:
        with self._lock:
            self.opened_total += 1
            key = str(source or "unknown")
            self.opened_by_source[key] = self.opened_by_source.get(key, 0) + 1

    def record_upgraded(self, source: str) -> None:
        with self._lock:
            self.upgraded_total += 1

    def record_repeat(self) -> None:
        with self._lock:
            self.repeat_signals += 1

    def record_closed(
        self,
        open_hours: float = -1.0,
        source: str = "",
        resolution_bucket: str = "",
    ) -> None:
        with self._lock:
            self.closed_total += 1
            if open_hours >= 0:
                self.close_hours_sum += float(open_hours)
                self.close_hours_n += 1
            src = str(source or "unknown")
            self.closed_by_source[src] = self.closed_by_source.get(src, 0) + 1
            if open_hours >= 0:
                self.close_hours_by_source[src] = (
                    self.close_hours_by_source.get(src, 0.0) + float(open_hours))
                self.close_n_by_source[src] = self.close_n_by_source.get(src, 0) + 1
            bucket = str(resolution_bucket or "other")
            self.closed_by_resolution[bucket] = (
                self.closed_by_resolution.get(bucket, 0) + 1)

    def record_suppressed(self, source: str) -> None:
        """误报静默期内被拦下的同类开案（P5：运营勾了「别再自动立案」才有）。"""
        with self._lock:
            self.suppressed_total += 1
            key = str(source or "unknown")
            self.suppressed_by_source[key] = self.suppressed_by_source.get(key, 0) + 1

    def record_alert(self) -> None:
        with self._lock:
            self.alerts_emitted += 1

    # ── 导出 ────────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        with self._lock:
            avg = (self.close_hours_sum / self.close_hours_n) if self.close_hours_n else None
            avg_by_src: Dict[str, float] = {}
            for src, n in self.close_n_by_source.items():
                if n > 0:
                    avg_by_src[src] = round(
                        self.close_hours_by_source.get(src, 0.0) / n, 2)
            return {
                "since": self.started_at,
                "opened": self.opened_total,
                "opened_by_source": dict(self.opened_by_source),
                "upgraded": self.upgraded_total,
                "repeat_signals": self.repeat_signals,
                "closed": self.closed_total,
                "avg_close_hours": round(avg, 2) if avg is not None else None,
                "closed_by_source": dict(self.closed_by_source),
                "closed_by_resolution": dict(self.closed_by_resolution),
                "avg_close_hours_by_source": avg_by_src,
                "suppressed": self.suppressed_total,
                "suppressed_by_source": dict(self.suppressed_by_source),
                "alerts_emitted": self.alerts_emitted,
            }

    def dump_prom(self) -> str:
        d = self.dump()
        lines = [
            "# HELP ws_cases_opened_total Cases opened since boot",
            "# TYPE ws_cases_opened_total counter",
            f"ws_cases_opened_total {d['opened']}",
            "# HELP ws_cases_closed_total Cases closed since boot",
            "# TYPE ws_cases_closed_total counter",
            f"ws_cases_closed_total {d['closed']}",
            "# HELP ws_cases_upgraded_total Case severity upgrades since boot",
            "# TYPE ws_cases_upgraded_total counter",
            f"ws_cases_upgraded_total {d['upgraded']}",
            "# HELP ws_cases_alerts_total Case alerts emitted since boot",
            "# TYPE ws_cases_alerts_total counter",
            f"ws_cases_alerts_total {d['alerts_emitted']}",
            "# HELP ws_cases_suppressed_total Case opens suppressed by false-alarm mute",
            "# TYPE ws_cases_suppressed_total counter",
            f"ws_cases_suppressed_total {d['suppressed']}",
        ]
        for src, n in sorted((d.get("opened_by_source") or {}).items()):
            lines.append(
                f'ws_cases_opened_by_source_total{{source="{_safe_label(src)}"}} {n}')
        for src, n in sorted((d.get("closed_by_source") or {}).items()):
            lines.append(
                f'ws_cases_closed_by_source_total{{source="{_safe_label(src)}"}} {n}')
        for bucket, n in sorted((d.get("closed_by_resolution") or {}).items()):
            lines.append(
                f'ws_cases_closed_by_resolution_total{{bucket="{_safe_label(bucket)}"}} {n}')
        return "\n".join(lines) + "\n"


_stats: CaseStats | None = None
_stats_lock = threading.Lock()


def get_case_stats() -> CaseStats:
    global _stats
    if _stats is None:
        with _stats_lock:
            if _stats is None:
                _stats = CaseStats()
    return _stats
