"""前端错误/意图落空「按日落库」时序持久化（SQLite）。

背景与定位
----------
``frontend_error_stats`` 是**进程内**累计——本机重启频繁，重启即归零，ops 看板只能看
「当下有多少」，看不到「scoped_fail / dead_intent / conv_not_found 这些意图落空是在
收敛还是回潮」（几轮账号视角/深链修复的效果验收要靠这条时间线）。本模块把 beacon
上报按 (日, 类型) 增量 upsert 落地，供看板画近 N 天 sparkline。

设计（对齐 identity_trend_store / translation_trend_store）：
- **纯增量 upsert**：``INSERT ... ON CONFLICT DO UPDATE``，无周期快照线程，写在 beacon
  路由旁路（telemetry 本就低频）。
- **默认关**：未 ``configure_frontend_error_trend(enabled=True, ...)`` → record 恒 no-op。
- **模块级单例**：路由旁路调用一行。
- 只存 (日期, 消毒后类型, 计数)——类型经 ``frontend_error_stats._san_type`` 白名单
  收敛（词表外折叠 "Error"），基数天然有界；绝不落页面/函数名/原文。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.web.frontend_error_stats import _san_type

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS fe_trend_daily (
    day    TEXT NOT NULL,
    etype  TEXT NOT NULL,
    n      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, etype)
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与其他 trend store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class FrontendErrorTrendStore:
    """前端错误按 (日, 类型) 聚合（线程安全 SQLite）。"""

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

    def add(self, etype: str, *, n: int = 1, now: Optional[float] = None) -> None:
        """把一次上报计入当日该类型。类型二次消毒（白名单外折叠 Error）。绝不抛。"""
        cnt = max(0, int(n))
        if cnt == 0:
            return
        t = _san_type(etype)
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO fe_trend_daily (day, etype, n) VALUES (?, ?, ?) "
                    "ON CONFLICT(day, etype) DO UPDATE SET n = n + excluded.n",
                    (day, t, cnt),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[fe_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）：[{day, total, by_type:{...}}]。缺数据补零不断点。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        buckets: Dict[str, Dict[str, int]] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, etype, n FROM fe_trend_daily WHERE day >= ? "
                    "ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    buckets.setdefault(str(r["day"]), {})[str(r["etype"])] = int(r["n"])
        except Exception:
            logger.debug("[fe_trend] daily 读取失败（已忽略）", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for day in day_keys:
            by_type = buckets.get(day, {})
            out.append({
                "day": day,
                "total": sum(by_type.values()),
                "by_type": dict(sorted(by_type.items())),
            })
        return out

    def prune(self, *, retention_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数。"""
        keep = retention_days if retention_days is not None else _RETENTION_DAYS
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(keep)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM fe_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[fe_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 identity_trend_store 同构）────────────────────
_STORE: Optional[FrontendErrorTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_frontend_error_trend(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[FrontendErrorTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = FrontendErrorTrendStore(db_path)
            except Exception:
                logger.warning("[fe_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_frontend_error_trend_store() -> Optional[FrontendErrorTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_frontend_error_trend(etype: str) -> None:
    """beacon 路由旁路写入：未启用 / 无 store → 立即返回（零开销）。绝不抛。"""
    if not _ENABLED or _STORE is None:
        return
    _STORE.add(etype)


def reset_frontend_error_trend() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "FrontendErrorTrendStore",
    "configure_frontend_error_trend",
    "get_frontend_error_trend_store",
    "record_frontend_error_trend",
    "reset_frontend_error_trend",
]
