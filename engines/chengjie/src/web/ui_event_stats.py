"""前端 UI 交互事件埋点（进程级单例）。

背景：前端加了不少「引导性」交互（空态引导按钮、群区显示模式切换等），但此前
后台对「用户有没有点、点了多少」零感知——引导做没做对全靠猜。

本模块把这些交互变成**可观测计数**：前端 beacon 到 `POST /api/telemetry/ui-event`，
此处按 (page, action) 累计，经 dump()→`/api/workspace/metrics.ui_events`、
dump_prom()→Prometheus（`ui_events_*`），回答「空态引导按钮点击率、群区模式
切换频次」这类「引导有效性」问题。

风格对齐 src/web/frontend_error_stats.py：无新增依赖，线程安全，进程级单例。
**只收消毒后的 page 路径 + action 标识符**，绝不收文本内容/URL 查询串/自由文本
（无 PII）。distinct key 有上限（防脏数据/刷量把内存撑爆），超限归入
`__other__` 并计 overflow。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

_MAX_KEYS = 100  # by_action / by_page 各自最多保留的 distinct key 数
# action 是小写标识符（如 empty_state_cta / group_mode_toggle），不合法归 unknown
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
# page 路径只保留合法 URL path 字符（丢查询串/hash/异常内容），截断防超长
_PATH_SAFE = re.compile(r"[^A-Za-z0-9/_\-.:]")


def _san_page(page: str) -> str:
    p = str(page or "").split("?", 1)[0].split("#", 1)[0].strip()
    p = _PATH_SAFE.sub("", p)
    if not p:
        return "unknown"
    return p[:80]


def _san_action(action: str) -> str:
    a = str(action or "").strip()
    return a if _ACTION_RE.match(a) else "unknown"


class UiEventStats:
    """前端 UI 交互事件计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "total", "overflow", "_by_action", "_by_page",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.total = 0
        self.overflow = 0                     # distinct key 超限被归入 __other__ 的次数
        self._by_action: Dict[str, int] = {}
        self._by_page: Dict[str, int] = {}

    @staticmethod
    def _bump(d: Dict[str, int], key: str) -> bool:
        """key 已存在或未超限 → +1 返回 False；超限 → 归 __other__ 返回 True（overflow）。"""
        if key in d or len(d) < _MAX_KEYS:
            d[key] = d.get(key, 0) + 1
            return False
        d["__other__"] = d.get("__other__", 0) + 1
        return True

    def record(self, *, page: str = "", action: str = "") -> None:
        p, a = _san_page(page), _san_action(action)
        with self._lock:
            self.total += 1
            self._last_ts = time.time()
            of = self._bump(self._by_page, p)
            of = self._bump(self._by_action, a) or of
            if of:
                self.overflow += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": self.total,
                "overflow": self.overflow,
                "by_action": dict(sorted(self._by_action.items(), key=lambda kv: (-kv[1], kv[0]))),
                "by_page": dict(sorted(self._by_page.items(), key=lambda kv: (-kv[1], kv[0]))),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP ui_events_total Frontend UI interaction events (denominator)",
                "# TYPE ui_events_total counter",
                f"ui_events_total {self.total}",
                "# HELP ui_events_by_action_total Frontend UI interaction events by action",
                "# TYPE ui_events_by_action_total counter",
            ]
            for a, n in sorted(self._by_action.items()):
                lines.append(f'ui_events_by_action_total{{action="{_esc(a)}"}} {int(n)}')
            lines += [
                "# HELP ui_events_by_page_total Frontend UI interaction events by page path",
                "# TYPE ui_events_by_page_total counter",
            ]
            for p, n in sorted(self._by_page.items()):
                lines.append(f'ui_events_by_page_total{{page="{_esc(p)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.overflow = 0
            self._by_action.clear()
            self._by_page.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[UiEventStats] = None
_LOCK = threading.Lock()


def get_ui_event_stats() -> UiEventStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = UiEventStats()
    return _SINGLETON


__all__ = ["UiEventStats", "get_ui_event_stats"]
