"""CSRF 准入/拒绝「按日落库」时序持久化（SQLite）。

背景与定位（P2，2026-07-31）
----------------------------
``csrf_stats`` 是**进程内**累计——本机重启频繁，重启即归零。而 P2 的收口决策
（把「同源 Origin/Referer 回落」降级为纯观测，只留 token/Bearer 正路）需要的是
**跨两周的通行侧证据**：还有多少合法写请求靠 Origin/Referer 放行？归零多少天了？
进程计数器回答不了，这里按 (日, 键) 增量 upsert 落地。

**刻意只落两类**（写放大控制）：
- ``reject:<kind>``   —— 全量拒绝（本就稀有，出现即异常）；
- ``admit:origin`` / ``admit:referer`` —— 收口决策的直接信号（正常应趋零）。
``csrf_pair``/``bearer`` 放行是绝对主流（每次写请求一条），**不落库**——趋势价值
低且每请求一次 SQLite 写不值得；它们的进程内累计在 csrf_stats.admitted_by 可见。

设计对齐 frontend_error_trend：纯增量 upsert、默认关（未 configure → no-op）、
模块级单例、键白名单收敛基数有界；绝不落 IP/UA/URL 原文。
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
CREATE TABLE IF NOT EXISTS csrf_trend_daily (
    day  TEXT NOT NULL,
    key  TEXT NOT NULL,
    n    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);
"""

_REJECT_KINDS = ("cookie_no_header", "pair_mismatch", "origin_mismatch",
                 "referer_mismatch", "bare")
# 只持久化这两张通行证的放行（收口决策信号）；csrf_pair/bearer 刻意不落库
_TRENDED_ADMITS = ("origin", "referer")


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与其他 trend store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class CsrfTrendStore:
    """CSRF 准入/拒绝按 (日, 键) 聚合（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def add(self, key: str, *, n: int = 1, now: Optional[float] = None) -> None:
        """把一次事件计入当日该键。键须为白名单形态（调用方已保证）。绝不抛。"""
        cnt = max(0, int(n))
        if cnt == 0:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO csrf_trend_daily (day, key, n) VALUES (?, ?, ?) "
                    "ON CONFLICT(day, key) DO UPDATE SET n = n + excluded.n",
                    (day, str(key), cnt),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[csrf_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 14, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）：[{day, rejects, admits, by_key:{...}}]。缺数据补零。"""
        n = max(1, min(int(days or 14), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        buckets: Dict[str, Dict[str, int]] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, key, n FROM csrf_trend_daily WHERE day >= ? "
                    "ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    buckets.setdefault(str(r["day"]), {})[str(r["key"])] = int(r["n"])
        except Exception:
            logger.debug("[csrf_trend] daily 读取失败（已忽略）", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for day in day_keys:
            by_key = buckets.get(day, {})
            out.append({
                "day": day,
                "rejects": sum(v for k, v in by_key.items() if k.startswith("reject:")),
                "admits": sum(v for k, v in by_key.items() if k.startswith("admit:")),
                "by_key": dict(sorted(by_key.items())),
            })
        return out

    def prune(self, *, retention_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        keep = retention_days if retention_days is not None else _RETENTION_DAYS
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(keep)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM csrf_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[csrf_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 frontend_error_trend 同构）────────────────────
_STORE: Optional[CsrfTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_csrf_trend(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[CsrfTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 旁路写入恒 no-op。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = CsrfTrendStore(db_path)
            except Exception:
                logger.warning("[csrf_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_csrf_trend_store() -> Optional[CsrfTrendStore]:
    return _STORE


def record_csrf_reject_trend(kind: str) -> None:
    """中间件拒绝旁路：未启用 → 零开销返回。kind 白名单外折叠 bare。"""
    if not _ENABLED or _STORE is None:
        return
    k = kind if kind in _REJECT_KINDS else "bare"
    _STORE.add(f"reject:{k}")


def record_csrf_admit_trend(ticket: str) -> None:
    """中间件放行旁路：**只落 origin/referer**（收口决策信号），其余 no-op。"""
    if not _ENABLED or _STORE is None:
        return
    if ticket in _TRENDED_ADMITS:
        _STORE.add(f"admit:{ticket}")


def reset_csrf_trend() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "CsrfTrendStore",
    "configure_csrf_trend",
    "get_csrf_trend_store",
    "record_csrf_reject_trend",
    "record_csrf_admit_trend",
    "reset_csrf_trend",
]
