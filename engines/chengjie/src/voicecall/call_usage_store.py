"""按账号的通话用量持久化（喂账号级预算闸，跨进程重启存活）。

与 ``protocol_autoreply_limits.SendCountStore`` 同源同风格：把每通已接听通话（时间戳 + 时长）
落 SQLite，滚动 24h 窗口计数 → `evaluate_call_budget` 的 ``calls_today``/``minutes_today``
跨重启仍反映真实近 24h 量（否则重启即归零 = 日预算形同虚设，真号安全洞）。

**为何滚动 24h 而非自然日**：自然日午夜清零会被「23:59 打一轮 + 00:01 再打一轮」绕过；
滚动窗口对风控更诚实（与仓内 flood_waits_24h/errors_24h 同口径）。

线程安全（check_same_thread=False + 自带锁）；摊还清理 >2 天陈旧行（表恒小）。绝不抛给调用方——
任何 IO 异常由上层捕获降级（预算闸缺数据时保守放行按 0 计），**永不阻断通话**。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Tuple

logger = logging.getLogger(__name__)

_DAY = 86400.0

_DDL = """
CREATE TABLE IF NOT EXISTS call_usage (
    account_key  TEXT NOT NULL,
    ts           REAL NOT NULL,
    duration_sec REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_call_usage_key_ts
    ON call_usage(account_key, ts);
"""


class CallUsageStore:
    """按账号（``platform:account_id``）的通话用量：滚动 24h 次数 + 分钟数。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._writes = 0
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_DDL)
            self._conn.commit()
            self._prune_locked(time.time() - 2 * _DAY)

    def record_call(self, account_key: str, duration_sec: float,
                    *, now: float | None = None) -> None:
        """记一通已接听通话（挂断时调用，duration_sec=通话时长）。"""
        ak = str(account_key or "").strip()
        if not ak:
            return
        ts = float(now if now is not None else time.time())
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO call_usage (account_key, ts, duration_sec) VALUES (?,?,?)",
                    (ak, ts, max(0.0, float(duration_sec or 0.0))),
                )
                self._conn.commit()
                self._writes += 1
                if self._writes % 50 == 0:      # 摊还清理
                    self._prune_locked(ts - 2 * _DAY)
        except Exception:
            logger.debug("[call-usage] record 失败（忽略）", exc_info=True)

    def usage_since(self, account_key: str, since_ts: float) -> Tuple[int, float]:
        """返回 (通话数, 总分钟)。异常安全退化 (0, 0.0)（预算闸据此保守放行）。"""
        ak = str(account_key or "").strip()
        if not ak:
            return (0, 0.0)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(duration_sec),0) "
                    "FROM call_usage WHERE account_key=? AND ts>=?",
                    (ak, float(since_ts)),
                ).fetchone()
            calls = int((row[0] if row else 0) or 0)
            minutes = float((row[1] if row else 0.0) or 0.0) / 60.0
            return (calls, round(minutes, 2))
        except Exception:
            logger.debug("[call-usage] usage_since 失败（忽略）", exc_info=True)
            return (0, 0.0)

    def usage_today(self, account_key: str, *, now: float | None = None) -> Tuple[int, float]:
        """滚动近 24h 的 (通话数, 总分钟)。"""
        n = float(now if now is not None else time.time())
        return self.usage_since(account_key, n - _DAY)

    def _prune_locked(self, before_ts: float) -> None:
        try:
            self._conn.execute("DELETE FROM call_usage WHERE ts<?", (float(before_ts),))
            self._conn.commit()
        except Exception:
            logger.debug("[call-usage] prune 失败（忽略）", exc_info=True)


__all__ = ["CallUsageStore"]
