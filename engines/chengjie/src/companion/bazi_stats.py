"""命理技能观测（进程级单例，风格对齐 avatar_voice_stats）——「开闸后值不值」用数据说话。

计数面 = 漏斗四段：话题触达（topic）→ 画像采集（ask/captured）→ 内容供给
（chart/daily/kline）→ 变现（deep allowed/upsell）。全部 in-process 计数器 +
RLock，方法绝不抛；经 `/api/workspace/metrics.bazi` + Prometheus `bazi_*` 导出，
ops-overview「🔮 命理技能」卡消费。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class BaziStats:
    __slots__ = (
        "_lock", "_since", "topic_turns", "chart_injections", "same_turn_charts",
        "ask_directives", "birth_captured", "gender_completed",
        "daily_chat", "daily_ritual", "deep_allowed", "deep_upsell",
        "kline_sent", "kline_failed",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._since = time.time()
        self.topic_turns = 0
        self.chart_injections = 0
        self.same_turn_charts = 0
        self.ask_directives = 0
        self.birth_captured = 0
        self.gender_completed = 0
        self.daily_chat = 0
        self.daily_ritual = 0
        self.deep_allowed = 0
        self.deep_upsell = 0
        self.kline_sent = 0
        self.kline_failed = 0

    # ── 记录（绝不抛） ──────────────────────────────────────────────────────
    def record_topic_turn(self) -> None:
        with self._lock:
            self.topic_turns += 1

    def record_chart_injection(self, *, same_turn: bool = False) -> None:
        with self._lock:
            self.chart_injections += 1
            if same_turn:
                self.same_turn_charts += 1

    def record_ask_directive(self) -> None:
        with self._lock:
            self.ask_directives += 1

    def record_birth_captured(self) -> None:
        with self._lock:
            self.birth_captured += 1

    def record_gender_completed(self) -> None:
        with self._lock:
            self.gender_completed += 1

    def record_daily_card(self, source: str = "chat") -> None:
        with self._lock:
            if source == "ritual":
                self.daily_ritual += 1
            else:
                self.daily_chat += 1

    def record_deep_reading(self, *, allowed: bool) -> None:
        with self._lock:
            if allowed:
                self.deep_allowed += 1
            else:
                self.deep_upsell += 1

    def record_kline(self, *, ok: bool) -> None:
        with self._lock:
            if ok:
                self.kline_sent += 1
            else:
                self.kline_failed += 1

    # ── 导出 ────────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        with self._lock:
            total_daily = self.daily_chat + self.daily_ritual
            out: Dict[str, Any] = {
                "since": self._since,
                "topic_turns": self.topic_turns,
                "chart_injections": self.chart_injections,
                "same_turn_charts": self.same_turn_charts,
                "ask_directives": self.ask_directives,
                "birth_captured": self.birth_captured,
                "gender_completed": self.gender_completed,
                "daily_cards": {"chat": self.daily_chat, "ritual": self.daily_ritual,
                                "total": total_daily},
                "deep_reading": {"allowed": self.deep_allowed,
                                 "upsell": self.deep_upsell},
                "kline": {"sent": self.kline_sent, "failed": self.kline_failed},
            }
            # 采集转化率：引导发出后拿到生辰的比例（同轮直报不吃引导，单列不混）
            out["capture_rate"] = (
                round(self.birth_captured / self.ask_directives, 3)
                if self.ask_directives > 0 else None)
            out["active"] = bool(
                self.topic_turns or self.ask_directives or total_daily
                or self.deep_allowed or self.deep_upsell or self.kline_sent
                or self.kline_failed or self.birth_captured)
            return out

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP bazi_topic_turns_total Bazi topic turns (chart/ask/daily injected)",
                "# TYPE bazi_topic_turns_total counter",
                f"bazi_topic_turns_total {self.topic_turns}",
                "# HELP bazi_chart_injections_total Bazi chart blocks injected",
                "# TYPE bazi_chart_injections_total counter",
                f"bazi_chart_injections_total {self.chart_injections}",
                f"bazi_same_turn_charts_total {self.same_turn_charts}",
                "# HELP bazi_birth_ask_total Birth-info ask directives injected",
                "# TYPE bazi_birth_ask_total counter",
                f"bazi_birth_ask_total {self.ask_directives}",
                "# HELP bazi_birth_captured_total Birth-info facts captured",
                "# TYPE bazi_birth_captured_total counter",
                f"bazi_birth_captured_total {self.birth_captured}",
                f"bazi_gender_completed_total {self.gender_completed}",
                "# HELP bazi_daily_cards_total Daily fortune cards surfaced",
                "# TYPE bazi_daily_cards_total counter",
                f'bazi_daily_cards_total{{source="chat"}} {self.daily_chat}',
                f'bazi_daily_cards_total{{source="ritual"}} {self.daily_ritual}',
                "# HELP bazi_deep_reading_total Deep-reading gate outcomes",
                "# TYPE bazi_deep_reading_total counter",
                f'bazi_deep_reading_total{{outcome="allowed"}} {self.deep_allowed}',
                f'bazi_deep_reading_total{{outcome="upsell"}} {self.deep_upsell}',
                "# HELP bazi_kline_total Life K-line cards",
                "# TYPE bazi_kline_total counter",
                f'bazi_kline_total{{outcome="sent"}} {self.kline_sent}',
                f'bazi_kline_total{{outcome="failed"}} {self.kline_failed}',
            ]
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._since = time.time()
            self.topic_turns = 0
            self.chart_injections = 0
            self.same_turn_charts = 0
            self.ask_directives = 0
            self.birth_captured = 0
            self.gender_completed = 0
            self.daily_chat = 0
            self.daily_ritual = 0
            self.deep_allowed = 0
            self.deep_upsell = 0
            self.kline_sent = 0
            self.kline_failed = 0


_SINGLETON: Optional[BaziStats] = None
_LOCK = threading.Lock()


def get_bazi_stats() -> BaziStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = BaziStats()
    return _SINGLETON


__all__ = ["BaziStats", "get_bazi_stats"]
