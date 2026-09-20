# -*- coding: utf-8 -*-
"""自动化覆盖率「按日快照」时序持久化（P2 2026-08-09，SQLite）。

背景与定位
----------
覆盖率卡（automation_coverage）是**即时快照**——老板看到「现在 78% 有效全自动」，
但看不到「上周是 95%，一直在掉」这个真正需要行动的信号（掉＝坐席在批量接管 /
守卫在批量降档 / 新号预热堆积）。本模块按日落一行快照，供看板画近 N 天趋势。

与 ``translation_trend_store``（增量 upsert）的**语义差异**：覆盖率是状态不是流量，
按日 **REPLACE**（同日多次写，最后一次胜出＝当日最新态）；写侧由 watchdog 每 tick
节流（≥1h 才重写今天的行），零热路开销。

设计（对齐 risk_events / tts_cost_store 家族）：
- 默认路径落**可写数据区**（``data_paths.config_dir()``——服务进程＝实例数据根，
  测试 conftest 已把 AITR_DATA_DIR 指向 tmp → 天然隔离，绝不写仓库 config/）。
- **默认关**：``ops.automation_coverage_trend.enabled``（新子系统约定）；关＝零 IO。
- 模块级单例 + 依赖注入（db_path 可传 tmp）→ 不依赖真库即可单测。
- 只存日期与计数，绝不存会话内容。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS coverage_trend_daily (
    day              TEXT NOT NULL PRIMARY KEY,
    conversations    INTEGER NOT NULL DEFAULT 0,
    auto_ai          INTEGER NOT NULL DEFAULT 0,
    effective_auto   INTEGER NOT NULL DEFAULT 0,
    capped_auto      INTEGER NOT NULL DEFAULT 0,
    takeover_manual  INTEGER NOT NULL DEFAULT 0,
    manual           INTEGER NOT NULL DEFAULT 0,
    review           INTEGER NOT NULL DEFAULT 0,
    pending_drafts   INTEGER NOT NULL DEFAULT 0,
    stale_drafts     INTEGER NOT NULL DEFAULT 0
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """本地日期键 ``YYYY-MM-DD``（覆盖率是给本地运营看的班次口径，不用 UTC）。"""
    return time.strftime(
        "%Y-%m-%d", time.localtime(now if now is not None else time.time()))


class CoverageTrendStore:
    """覆盖率按日快照（线程安全 SQLite；REPLACE 语义）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        is_mem = str(db_path) == ":memory:"
        if not is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not is_mem:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def record_snapshot(
        self, coverage: Dict[str, Any], *, now: Optional[float] = None,
    ) -> None:
        """把一份 collect_automation_coverage 输出落为当日行（REPLACE）。绝不抛。"""
        try:
            t = (coverage or {}).get("totals") or {}
            d = (coverage or {}).get("drafts") or {}
            by_mode = t.get("by_mode") or {}
            by_age = d.get("by_age") or {}
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO coverage_trend_daily "
                    "(day, conversations, auto_ai, effective_auto, capped_auto,"
                    " takeover_manual, manual, review, pending_drafts, stale_drafts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _day_str(now),
                        int(t.get("conversations") or 0),
                        int(by_mode.get("auto_ai") or 0),
                        int(t.get("effective_auto") or 0),
                        int(t.get("capped_auto") or 0),
                        int(t.get("takeover_manual") or 0),
                        int(by_mode.get("manual") or 0),
                        int(by_mode.get("review") or 0),
                        int(d.get("pending") or 0),
                        int(by_age.get("stale") or 0),
                    ),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[coverage_trend] 快照落库失败（忽略）", exc_info=True)

    def recent(self, days: int = 14) -> List[Dict[str, Any]]:
        """近 N 天快照（旧→新，供 sparkline 直用）。"""
        n = max(1, min(90, int(days or 14)))
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM coverage_trend_daily "
                    "ORDER BY day DESC LIMIT ?", (n,),
                ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _default_db_path() -> Path:
    """默认落可写数据区（与 risk_events 同惯例：认 AITR_DATA_DIR，测试天然隔离）。"""
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / "automation_coverage_trend.db"
    except Exception:
        return Path("config") / "automation_coverage_trend.db"


_singleton: Optional[CoverageTrendStore] = None
_singleton_lock = threading.Lock()


def trend_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``ops.automation_coverage_trend``。默认关（新子系统约定）。"""
    raw = (((config or {}).get("ops") or {})
           .get("automation_coverage_trend") or {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        interval_min = float(raw.get("interval_min", 60) or 60)
    except Exception:
        interval_min = 60.0
    return {
        "enabled": bool(raw.get("enabled", False)),
        "interval_min": max(5.0, interval_min),
    }


def get_coverage_trend_store() -> CoverageTrendStore:
    """进程级单例（懒建；测试可绕过单例直接构造 tmp 实例）。"""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CoverageTrendStore(_default_db_path())
        return _singleton


__all__ = [
    "CoverageTrendStore",
    "get_coverage_trend_store",
    "trend_cfg",
]
