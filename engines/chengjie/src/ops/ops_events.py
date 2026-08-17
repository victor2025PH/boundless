"""账号运维事件审计（实施31 追加 · 2026-07-20）。

反封号告警是「瞬时」的：出事推一条 TG，且同 (kind, account) 30 分钟防抖——老板没盯着
TG 就漏了，事后也查不到「这号这周被风控几次」。本 store 把每次运维事件（暂停/封禁/熔断/
限速触顶…）**全量**落库（与 TG 防抖解耦：审计全记，TG 才防抖），供回溯与「号健康史」统计。

与 ``protocol_autoreply_limits.SendCountStore`` 同风格：独立 SQLite（默认 ``config/
ops_events.db``，cwd=实例数据根）、线程安全、幂等建表、周期清理陈旧行、任何 IO 异常静默
降级（审计绝不阻断发送/告警主流程）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DAY = 86400.0
_RETAIN_DAYS = 90  # 审计保留 90 天（够看季度健康史；表恒小）

_DDL = """
CREATE TABLE IF NOT EXISTS ops_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'telegram',
    account_id  TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    alerted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ops_events_acct_ts
    ON ops_events(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_ops_events_kind_ts
    ON ops_events(kind, ts);
"""


class OpsEventStore:
    """账号运维事件审计（线程安全 SQLite）。"""

    def __init__(self, db_path: Any) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._writes = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_DDL)
            self._conn.commit()
            self._prune_locked(time.time() - _RETAIN_DAYS * _DAY)

    def record(self, kind: str, *, account_id: str = "", platform: str = "telegram",
               reason: str = "", detail: str = "", alerted: bool = False,
               ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO ops_events (ts, platform, account_id, kind, reason, detail, alerted)
                   VALUES (?,?,?,?,?,?,?)""",
                (float(ts), str(platform or "telegram"), str(account_id or ""),
                 str(kind or ""), str(reason or ""), str(detail or ""), 1 if alerted else 0),
            )
            self._conn.commit()
            self._writes += 1
            if self._writes % 50 == 0:
                self._prune_locked(time.time() - _RETAIN_DAYS * _DAY)

    def recent(self, *, account_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        q = "SELECT ts, platform, account_id, kind, reason, detail, alerted FROM ops_events"
        args: List[Any] = []
        if account_id:
            q += " WHERE account_id=?"
            args.append(str(account_id))
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def count_since(self, *, account_id: str = "", kind: str = "",
                    since_ts: float = 0.0) -> int:
        q = "SELECT COUNT(*) FROM ops_events WHERE ts>=?"
        args: List[Any] = [float(since_ts)]
        if account_id:
            q += " AND account_id=?"
            args.append(str(account_id))
        if kind:
            q += " AND kind=?"
            args.append(str(kind))
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return int((row[0] if row else 0) or 0)

    def summary(self, *, account_id: str = "", days: int = 7) -> Dict[str, Any]:
        """近 N 天各类事件计数（「号健康史」概览：paused/banned/… 各几次）。"""
        since = time.time() - float(days) * _DAY
        q = "SELECT kind, COUNT(*) AS n FROM ops_events WHERE ts>=?"
        args: List[Any] = [since]
        if account_id:
            q += " AND account_id=?"
            args.append(str(account_id))
        q += " GROUP BY kind"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        by_kind = {str(r["kind"]): int(r["n"]) for r in rows}
        return {"account_id": account_id, "days": days,
                "total": sum(by_kind.values()), "by_kind": by_kind}

    def _prune_locked(self, before_ts: float) -> None:
        try:
            self._conn.execute("DELETE FROM ops_events WHERE ts<?", (float(before_ts),))
            self._conn.commit()
        except Exception:
            logger.debug("[ops_events] prune 失败（忽略）", exc_info=True)


_store: Optional[OpsEventStore] = None
_store_lock = threading.Lock()


def get_ops_event_store(db_path: str = "config/ops_events.db") -> Optional[OpsEventStore]:
    """进程内单例（首次可指定路径）。建库失败 → None（审计降级，绝不阻断主流程）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                try:
                    _store = OpsEventStore(db_path)
                except Exception:
                    logger.warning("[ops_events] 建库失败，审计降级（不落库）", exc_info=True)
                    _store = None
    return _store


def reset_ops_event_store() -> None:
    """测试辅助：清空单例。"""
    global _store
    with _store_lock:
        _store = None


__all__ = ["OpsEventStore", "get_ops_event_store", "reset_ops_event_store"]
