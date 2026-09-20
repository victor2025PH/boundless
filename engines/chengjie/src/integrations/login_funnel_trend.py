"""账号接入漏斗「按日落库」时序持久化（SQLite）。

背景与定位
----------
``login_funnel_stats`` 是**进程内**累计——本机重启频繁，ops 看板只能看「当下发起/成功/
失败」，看不到「成功率是在收敛还是回潮」「checkpoint 是不是这周突然增多」。本模块把
漏斗关键段（started / authorized / failed）与失败原因码按日增量 upsert 落地，供看板
画近 N 天 sparkline。

设计取舍（相对「按 platform:mode 分桶」的原设想再次优化）
----------------------------------------------------
- **全站聚合日序列**，不按 (platform, mode) 分桶落库。分桶已在进程内 dump 表里实时
  可见；趋势问的是「接入整体在变好还是变坏」——拆桶会把曲线打成噪音，还把
  ``_MAX_KEYS`` 那套折叠再做一遍。
- **原因码单独表**：一眼读「checkpoint / two_factor / session_timeout」周分布，
  正是「代码坏了 vs 账号被风控」的分水岭。
- 对齐 frontend_error_trend：纯增量 upsert、默认关、模块级单例、旁路写入零阻断。
- 只存计数，绝不记 PIN / account_id / 二维码 / 任何用户可识别信息。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.login_funnel_stats import _REASON_CODES, _san_reason

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS login_funnel_trend_daily (
    day         TEXT NOT NULL PRIMARY KEY,
    started     INTEGER NOT NULL DEFAULT 0,
    authorized  INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_funnel_reason_daily (
    day     TEXT NOT NULL,
    reason  TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, reason)
);
"""

_STAGE_COLS = frozenset({"started", "authorized", "failed"})


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与其他 trend store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class LoginFunnelTrendStore:
    """账号接入漏斗按日聚合（线程安全 SQLite）。"""

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
        self,
        stage: str,
        *,
        reason_code: str = "",
        now: Optional[float] = None,
    ) -> None:
        """把一次漏斗事件计入当日。只认 started/authorized/failed；绝不抛。"""
        st = str(stage or "").strip().lower()
        if st not in _STAGE_COLS:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO login_funnel_trend_daily (day, started, authorized, failed) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    f"  {st} = {st} + 1",
                    (
                        day,
                        1 if st == "started" else 0,
                        1 if st == "authorized" else 0,
                        1 if st == "failed" else 0,
                    ),
                )
                if st == "failed":
                    reason = _san_reason(reason_code)
                    # 再钉一次枚举：_san_reason 已折叠未知码，这里只防空。
                    if reason not in _REASON_CODES:
                        reason = "login_failed"
                    self._conn.execute(
                        "INSERT INTO login_funnel_reason_daily (day, reason, n) "
                        "VALUES (?, ?, 1) "
                        "ON CONFLICT(day, reason) DO UPDATE SET n = n + 1",
                        (day, reason),
                    )
                self._conn.commit()
        except Exception:
            logger.debug("[login_funnel_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）：[{day, started, authorized, failed, by_reason}]。

        缺数据补零不断点——sparkline 需要等长序列。
        """
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        totals: Dict[str, Dict[str, int]] = {}
        reasons: Dict[str, Dict[str, int]] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, started, authorized, failed "
                    "FROM login_funnel_trend_daily WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    totals[str(r["day"])] = {
                        "started": int(r["started"] or 0),
                        "authorized": int(r["authorized"] or 0),
                        "failed": int(r["failed"] or 0),
                    }
                for r in self._conn.execute(
                    "SELECT day, reason, n FROM login_funnel_reason_daily "
                    "WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    reasons.setdefault(str(r["day"]), {})[str(r["reason"])] = int(r["n"] or 0)
        except Exception:
            logger.debug("[login_funnel_trend] daily 读取失败（已忽略）", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for day in day_keys:
            t = totals.get(day) or {"started": 0, "authorized": 0, "failed": 0}
            out.append({
                "day": day,
                "started": t["started"],
                "authorized": t["authorized"],
                "failed": t["failed"],
                "by_reason": dict(sorted(reasons.get(day, {}).items())),
            })
        return out

    def prune(self, *, retention_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数（两表合计）。"""
        keep = retention_days if retention_days is not None else _RETENTION_DAYS
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(keep)) * 86400)
        try:
            with self._lock:
                c1 = self._conn.execute(
                    "DELETE FROM login_funnel_trend_daily WHERE day < ?", (cut,))
                c2 = self._conn.execute(
                    "DELETE FROM login_funnel_reason_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c1.rowcount or 0) + int(c2.rowcount or 0)
        except Exception:
            logger.debug("[login_funnel_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门 ──────────────────────────────────────────────────
_STORE: Optional[LoginFunnelTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_login_funnel_trend(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[LoginFunnelTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = LoginFunnelTrendStore(db_path)
            except Exception:
                logger.warning("[login_funnel_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_login_funnel_trend_store() -> Optional[LoginFunnelTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_login_funnel_trend(
    stage: str, *, reason_code: str = "",
) -> None:
    """``record_login_stage`` 旁路写入：未启用 / 无 store → 立即返回。绝不抛。"""
    if not _ENABLED or _STORE is None:
        return
    _STORE.add(stage, reason_code=reason_code)


def reset_login_funnel_trend() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "LoginFunnelTrendStore",
    "configure_login_funnel_trend",
    "get_login_funnel_trend_store",
    "record_login_funnel_trend",
    "reset_login_funnel_trend",
]
