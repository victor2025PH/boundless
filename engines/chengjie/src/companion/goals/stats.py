"""营销目标观测（进程级单例，风格对齐 bazi_stats / avatar_voice_stats）。

漏斗五段：建目标 → 每日拍规划（含 hold 分桶）→ 注入生成链（draft/reply/proactive）
→ 主动桥真发 → 生命周期终态（done/failed/expired/cancelled）。
经 ``/api/workspace/metrics.goals`` + Prometheus ``goals_*`` 导出，
ops-overview「🎯 营销目标」卡消费。方法绝不抛。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class GoalStats:
    __slots__ = (
        "_lock", "_since", "created", "done", "failed", "expired", "cancelled",
        "paused", "beats_planned", "hold_emotion", "hold_silent",
        "injected_draft", "injected_reply", "injected_proactive",
        "beats_sent_proactive", "settle_runs", "milestones_advanced",
        "feedback_adopt", "feedback_reject",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._since = time.time()
        self.created = 0
        self.done = 0
        self.failed = 0
        self.expired = 0
        self.cancelled = 0
        self.paused = 0
        self.beats_planned = 0
        self.hold_emotion = 0
        self.hold_silent = 0
        self.injected_draft = 0
        self.injected_reply = 0
        self.injected_proactive = 0
        self.beats_sent_proactive = 0
        self.settle_runs = 0
        self.milestones_advanced = 0
        self.feedback_adopt = 0
        self.feedback_reject = 0

    # ── 记录（绝不抛）────────────────────────────────────────────────────────
    def record_created(self) -> None:
        with self._lock:
            self.created += 1

    def record_terminal(self, status: str) -> None:
        with self._lock:
            s = str(status or "")
            if s == "done":
                self.done += 1
            elif s == "failed":
                self.failed += 1
            elif s == "expired":
                self.expired += 1
            elif s == "cancelled":
                self.cancelled += 1

    def record_paused(self) -> None:
        with self._lock:
            self.paused += 1

    def record_beat_planned(self) -> None:
        with self._lock:
            self.beats_planned += 1

    def record_hold(self, reason: str) -> None:
        with self._lock:
            if str(reason) == "emotion":
                self.hold_emotion += 1
            else:
                self.hold_silent += 1

    def record_injected(self, chain: str) -> None:
        with self._lock:
            c = str(chain or "")
            if c == "draft":
                self.injected_draft += 1
            elif c == "proactive":
                self.injected_proactive += 1
            else:
                self.injected_reply += 1

    def record_beat_sent_proactive(self) -> None:
        with self._lock:
            self.beats_sent_proactive += 1

    def record_settle(self, *, milestones_advanced: int = 0) -> None:
        with self._lock:
            self.settle_runs += 1
            if milestones_advanced > 0:
                self.milestones_advanced += int(milestones_advanced)

    def record_feedback(self, verdict: str) -> None:
        with self._lock:
            if str(verdict) == "adopt":
                self.feedback_adopt += 1
            elif str(verdict) == "reject":
                self.feedback_reject += 1

    # ── 导出 ────────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        with self._lock:
            injected = (self.injected_draft + self.injected_reply
                        + self.injected_proactive)
            out: Dict[str, Any] = {
                "since": self._since,
                "created": self.created,
                "terminal": {"done": self.done, "failed": self.failed,
                             "expired": self.expired,
                             "cancelled": self.cancelled},
                "paused": self.paused,
                "beats": {"planned": self.beats_planned,
                          "hold_emotion": self.hold_emotion,
                          "hold_silent": self.hold_silent,
                          "sent_proactive": self.beats_sent_proactive},
                "injected": {"draft": self.injected_draft,
                             "reply": self.injected_reply,
                             "proactive": self.injected_proactive,
                             "total": injected},
                "feedback": {"adopt": self.feedback_adopt,
                             "reject": self.feedback_reject},
                "settle_runs": self.settle_runs,
                "milestones_advanced": self.milestones_advanced,
            }
            out["active"] = bool(
                self.created or injected or self.beats_planned
                or self.done or self.failed or self.expired)
            return out

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP goals_created_total Marketing goals created",
                "# TYPE goals_created_total counter",
                f"goals_created_total {self.created}",
                "# HELP goals_terminal_total Goal terminal outcomes",
                "# TYPE goals_terminal_total counter",
                f'goals_terminal_total{{status="done"}} {self.done}',
                f'goals_terminal_total{{status="failed"}} {self.failed}',
                f'goals_terminal_total{{status="expired"}} {self.expired}',
                f'goals_terminal_total{{status="cancelled"}} {self.cancelled}',
                "# HELP goals_beats_total Daily beats planned/held/sent",
                "# TYPE goals_beats_total counter",
                f'goals_beats_total{{kind="planned"}} {self.beats_planned}',
                f'goals_beats_total{{kind="hold_emotion"}} {self.hold_emotion}',
                f'goals_beats_total{{kind="hold_silent"}} {self.hold_silent}',
                f'goals_beats_total{{kind="sent_proactive"}} {self.beats_sent_proactive}',
                "# HELP goals_injected_total Goal blocks injected into generation",
                "# TYPE goals_injected_total counter",
                f'goals_injected_total{{chain="draft"}} {self.injected_draft}',
                f'goals_injected_total{{chain="reply"}} {self.injected_reply}',
                f'goals_injected_total{{chain="proactive"}} {self.injected_proactive}',
                "# HELP goals_milestones_advanced_total Milestone advances",
                "# TYPE goals_milestones_advanced_total counter",
                f"goals_milestones_advanced_total {self.milestones_advanced}",
                "# HELP goals_beat_feedback_total Agent feedback on daily beats",
                "# TYPE goals_beat_feedback_total counter",
                f'goals_beat_feedback_total{{verdict="adopt"}} {self.feedback_adopt}',
                f'goals_beat_feedback_total{{verdict="reject"}} {self.feedback_reject}',
            ]
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._since = time.time()
            self.created = 0
            self.done = 0
            self.failed = 0
            self.expired = 0
            self.cancelled = 0
            self.paused = 0
            self.beats_planned = 0
            self.hold_emotion = 0
            self.hold_silent = 0
            self.injected_draft = 0
            self.injected_reply = 0
            self.injected_proactive = 0
            self.beats_sent_proactive = 0
            self.settle_runs = 0
            self.milestones_advanced = 0
            self.feedback_adopt = 0
            self.feedback_reject = 0


_SINGLETON: Optional[GoalStats] = None
_LOCK = threading.Lock()


def get_goal_stats() -> GoalStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = GoalStats()
    return _SINGLETON


__all__ = ["GoalStats", "get_goal_stats"]
