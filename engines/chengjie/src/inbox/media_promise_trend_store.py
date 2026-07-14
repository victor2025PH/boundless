"""Phase22c：出站媒体承诺「按日落库」时序持久化（SQLite）。

背景与定位
----------
``image_autosend`` 的承诺守卫计数（detected/fulfilled/retracted/offer_accept）是**进程内**
累计——重启即归零，ops 卡只能看「当下」瞬时值，看不到「兑现率这几天是在改善还是恶化」。
本模块把每次承诺事件按日增量 upsert 落地，供看板画近 N 天兑现率 sparkline，并为
``health_watchdog.media_promise_remind`` 的阈值校准（Phase22d）提供真实分布回放。

事件 → 列映射（兑现有 B 线同步 + A 线异步两条链，落空有撤回 + 异步失败两种，聚合口径
与看门狗告警一致）：
    detected                              → detected
    fulfilled / fulfilled_async           → fulfilled
    retracted / fulfill_failed            → retracted
    offer_accept                          → offer_accept
    fulfill_scheduled                     → （中间态，不计；其最终以 async 成/败入账）

设计（对齐 translation_trend_store）：
- **纯增量 upsert**（无快照线程）：写在 ``record_promise_event`` 单一 choke point，
  承诺事件本就低频（每次发图/语音承诺才触发），热路开销可忽略。
- **默认关**：未 ``configure_media_promise_trend_store(enabled=True)`` → record 恒 no-op。
- 只存元数据（日期/计数），绝不记录任何文本。
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
CREATE TABLE IF NOT EXISTS media_promise_daily (
    day          TEXT NOT NULL PRIMARY KEY,
    detected     INTEGER NOT NULL DEFAULT 0,
    fulfilled    INTEGER NOT NULL DEFAULT 0,
    retracted    INTEGER NOT NULL DEFAULT 0,
    offer_accept INTEGER NOT NULL DEFAULT 0
);
"""

# 事件名 → 列（聚合两条兑现链 + 两种落空）
_EVENT_COLUMN: Dict[str, str] = {
    "detected": "detected",
    "fulfilled": "fulfilled",
    "fulfilled_async": "fulfilled",
    "retracted": "retracted",
    "fulfill_failed": "retracted",
    "offer_accept": "offer_accept",
}


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（跨时区部署口径一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class MediaPromiseTrendStore:
    """出站媒体承诺按日聚合（线程安全 SQLite）。"""

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

    def add(
        self, *, detected: int = 0, fulfilled: int = 0, retracted: int = 0,
        offer_accept: int = 0, now: Optional[float] = None,
    ) -> None:
        """把一组增量计入当日聚合。绝不抛。"""
        d = max(0, int(detected))
        f = max(0, int(fulfilled))
        r = max(0, int(retracted))
        o = max(0, int(offer_accept))
        if d == 0 and f == 0 and r == 0 and o == 0:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO media_promise_daily "
                    "(day, detected, fulfilled, retracted, offer_accept) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  detected = detected + excluded.detected, "
                    "  fulfilled = fulfilled + excluded.fulfilled, "
                    "  retracted = retracted + excluded.retracted, "
                    "  offer_accept = offer_accept + excluded.offer_accept",
                    (day, d, f, r, o),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[promise_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）。缺数据补零；含兑现率 = 兑现/(兑现+落空)。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        rows: Dict[str, sqlite3.Row] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, detected, fulfilled, retracted, offer_accept "
                    "FROM media_promise_daily WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    rows[r["day"]] = r
        except Exception:
            logger.debug("[promise_trend] daily 读取失败（已忽略）", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for day in day_keys:
            row = rows.get(day)
            det = int(row["detected"]) if row else 0
            ful = int(row["fulfilled"]) if row else 0
            ret = int(row["retracted"]) if row else 0
            off = int(row["offer_accept"]) if row else 0
            outcome = ful + ret
            out.append({
                "day": day,
                "detected": det,
                "fulfilled": ful,
                "retracted": ret,
                "offer_accept": off,
                # 兑现率：兑现 /（兑现+落空）；无结果的一天记 None（前端断点，不画 0 误导）
                "fulfill_rate": round(ful / outcome, 4) if outcome else None,
            })
        return out

    def prune(self, *, retention_days: float = 90.0, now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数。"""
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(retention_days)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM media_promise_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[promise_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 translation_trend_store 同构）────────────────────
_STORE: Optional[MediaPromiseTrendStore] = None
_ENABLED = False
_CFG_LOCK = threading.Lock()


def configure_media_promise_trend_store(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[MediaPromiseTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → record 恒 no-op。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = MediaPromiseTrendStore(db_path)
            except Exception:
                logger.warning("[promise_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_media_promise_trend_store() -> Optional[MediaPromiseTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_media_promise_trend(name: str) -> None:
    """承诺事件旁路写入（挂在 ``record_promise_event`` 单一出口）：

    未启用 / 无 store → 立即返回（零开销）。绝不抛。未知事件名忽略。
    """
    if not _ENABLED or _STORE is None:
        return
    col = _EVENT_COLUMN.get(str(name or "").strip())
    if not col:
        return
    _STORE.add(**{col: 1})


def reset_media_promise_trend_store() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "MediaPromiseTrendStore",
    "configure_media_promise_trend_store",
    "get_media_promise_trend_store",
    "record_media_promise_trend",
    "reset_media_promise_trend_store",
]
