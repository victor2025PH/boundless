# -*- coding: utf-8 -*-
"""案例中心「按日趋势」落库（SQLite，仿 translation_trend_store / tts_cost_store）。

背景：``case_stats`` 是进程内计数，重启即归零（本机重启频繁，ops 卡上已如实标注
「自启动」）；周报要回答「这周立案是多了还是少了、都是什么来源」，必须有跨重启
的日粒度数据。本模块把 open/close 事件按日 upsert 落地，供 ops 卡画 sparkline
与周批读数。

设计取舍：
- **刻意不加 config 开关**（与 translation_trend_store 的默认关不同）：案例是
  低频事件（日常几条/天），一次 upsert 的 IO 可忽略；它是既有 cases 子系统的
  观测补件而非新行为子系统，加开关只会让多数部署的趋势图恒空。任何异常
  （磁盘/权限/并发）都被调用方与本模块双层吞掉，绝不影响立案主流程。
- **落点**：``AITR_DATA_DIR`` 环境（tests 的 conftest 已指向 tmp → 测试绝不写
  仓库 config/）→ 否则 CWD ``config/``（服务进程 CWD=实例数据根，正确落点；
  引擎根 CLI 不会触发案例事件，无误写面）。
- 只存日期与计数（by_source / closed_by_* 为 JSON），绝不落用户内容。
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS case_trend_daily (
    day       TEXT NOT NULL PRIMARY KEY,
    opened    INTEGER NOT NULL DEFAULT 0,
    closed    INTEGER NOT NULL DEFAULT 0,
    by_source TEXT NOT NULL DEFAULT '{}',
    closed_by_source TEXT NOT NULL DEFAULT '{}',
    closed_by_resolution TEXT NOT NULL DEFAULT '{}'
);
"""

_EXTRA_COLS = (
    ("closed_by_source", "TEXT NOT NULL DEFAULT '{}'"),
    ("closed_by_resolution", "TEXT NOT NULL DEFAULT '{}'"),
)


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与 xlate_trend 同口径，跨时区部署一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def _parse_dist(raw: Any) -> Dict[str, int]:
    try:
        dist = json.loads(raw or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}
    out: Dict[str, int] = {}
    if isinstance(dist, dict):
        for k, v in dist.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def _bump_dist(raw: Any, key: str) -> str:
    dist = _parse_dist(raw)
    k = str(key or "unknown")
    dist[k] = int(dist.get(k, 0)) + 1
    return json.dumps(dist, ensure_ascii=False)


def default_db_path() -> Path:
    base = os.environ.get("AITR_DATA_DIR", "").strip()
    root = Path(base) if base else Path(".")
    return root / "config" / "cases_trend.db"


class CaseTrendStore:
    """案例按日聚合（线程安全 SQLite；所有写口吞异常）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._migrate_columns()
            self._conn.commit()

    def _migrate_columns(self) -> None:
        """旧库幂等补列（P3：结案来源/resolution 分桶跨重启可读）。"""
        cols = {
            str(r[1]) for r in self._conn.execute(
                "PRAGMA table_info(case_trend_daily)").fetchall()
        }
        for name, decl in _EXTRA_COLS:
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE case_trend_daily ADD COLUMN {name} {decl}")

    def add_opened(self, source: str, now: Optional[float] = None) -> None:
        day = _day_str(now)
        src = str(source or "unknown")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT by_source FROM case_trend_daily WHERE day = ?", (day,)
                ).fetchone()
                by_src = _bump_dist(row["by_source"] if row else "{}", src)
                self._conn.execute(
                    "INSERT INTO case_trend_daily (day, opened, closed, by_source) "
                    "VALUES (?, 1, 0, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  opened = opened + 1, by_source = excluded.by_source",
                    (day, by_src),
                )
                self._conn.commit()
        except Exception:
            logger.debug("case trend add_opened 失败（忽略）", exc_info=True)

    def add_closed(
        self,
        now: Optional[float] = None,
        source: str = "",
        resolution_bucket: str = "",
    ) -> None:
        day = _day_str(now)
        src = str(source or "unknown")
        bucket = str(resolution_bucket or "other")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT closed_by_source, closed_by_resolution "
                    "FROM case_trend_daily WHERE day = ?", (day,)
                ).fetchone()
                by_src = _bump_dist(
                    row["closed_by_source"] if row else "{}", src)
                by_res = _bump_dist(
                    row["closed_by_resolution"] if row else "{}", bucket)
                self._conn.execute(
                    "INSERT INTO case_trend_daily "
                    "(day, opened, closed, by_source, closed_by_source, "
                    " closed_by_resolution) "
                    "VALUES (?, 0, 1, '{}', ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  closed = closed + 1, "
                    "  closed_by_source = excluded.closed_by_source, "
                    "  closed_by_resolution = excluded.closed_by_resolution",
                    (day, by_src, by_res),
                )
                self._conn.commit()
        except Exception:
            logger.debug("case trend add_closed 失败（忽略）", exc_info=True)

    def recent(self, days: int = 14) -> List[Dict[str, Any]]:
        """近 N 天（升序，缺日不补零——前端按需补）。失败返回空列表。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT day, opened, closed, by_source, "
                    "closed_by_source, closed_by_resolution "
                    "FROM case_trend_daily ORDER BY day DESC LIMIT ?",
                    (max(1, int(days)),),
                ).fetchall()
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for r in reversed(rows):
            out.append({
                "day": r["day"],
                "opened": int(r["opened"]),
                "closed": int(r["closed"]),
                "by_source": _parse_dist(r["by_source"]),
                "closed_by_source": _parse_dist(r["closed_by_source"]),
                "closed_by_resolution": _parse_dist(r["closed_by_resolution"]),
            })
        return out

    def window_totals(self, lo: float, hi: float) -> Dict[str, Any]:
        """UTC 日键落在 ``[lo, hi)`` 的 opened/closed/分桶合计（周报用）。"""
        opened = closed = 0
        by_source: Dict[str, int] = {}
        closed_by_source: Dict[str, int] = {}
        closed_by_resolution: Dict[str, int] = {}
        # 多取几天防边界；再按 day epoch 过滤
        span_days = max(1, int((float(hi) - float(lo)) / 86400.0) + 3)
        for row in self.recent(span_days):
            try:
                day_ts = calendar.timegm(
                    time.strptime(row["day"], "%Y-%m-%d"))
            except Exception:
                continue
            # 日整段 [day_ts, day_ts+86400) 与查询窗 [lo, hi) 有交集才计入——
            # 不能只判「日起点落在窗内」（事件常在日末，lo=事件前几秒会把当天整段漏掉）。
            day_end = day_ts + 86400.0
            if day_end <= float(lo) or day_ts >= float(hi):
                continue
            opened += int(row.get("opened") or 0)
            closed += int(row.get("closed") or 0)
            for k, n in (row.get("by_source") or {}).items():
                by_source[k] = by_source.get(k, 0) + int(n)
            for k, n in (row.get("closed_by_source") or {}).items():
                closed_by_source[k] = closed_by_source.get(k, 0) + int(n)
            for k, n in (row.get("closed_by_resolution") or {}).items():
                closed_by_resolution[k] = (
                    closed_by_resolution.get(k, 0) + int(n))
        return {
            "opened": opened,
            "closed": closed,
            "by_source": by_source,
            "closed_by_source": closed_by_source,
            "closed_by_resolution": closed_by_resolution,
        }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_store: Optional[CaseTrendStore] = None
_store_lock = threading.Lock()
_store_failed = False


def get_case_trend_store() -> Optional[CaseTrendStore]:
    """模块级单例；初始化失败记一次后恒返 None（绝不反复重试拖慢立案）。"""
    global _store, _store_failed
    if _store is not None or _store_failed:
        return _store
    with _store_lock:
        if _store is None and not _store_failed:
            try:
                _store = CaseTrendStore(default_db_path())
            except Exception:
                logger.debug("case trend store 初始化失败（趋势不可用）", exc_info=True)
                _store_failed = True
    return _store


def peek_case_trend_store() -> Optional[CaseTrendStore]:
    """只读探测既有单例（周报用：绝不新建 :memory:/空库把「零案例」误报成事实）。"""
    return _store
