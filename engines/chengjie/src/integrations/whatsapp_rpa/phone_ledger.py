"""真机发送的电脑侧账本：拒收、首触、限额、client_msg_id。

对话正文在收件箱 messages。这张库只记「能不能发、发过没有」，
避免同一条消息点两次发送，也避免 daily_cap=0 被当成不封顶。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_HOURLY_CAP = 20
DEFAULT_DAILY_CAP = 80

_DDL = """
CREATE TABLE IF NOT EXISTS stops (
    phone TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    sample TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS first_touches (
    phone TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL DEFAULT '',
    client_msg_id TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sends (
    client_msg_id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_ts ON sends(ts);
CREATE TABLE IF NOT EXISTS touch_queue (
    phone TEXT PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT '',
    opener TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    ts REAL NOT NULL
);
"""


def resolve_caps(cfg: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """每小时、每天上限。0、负数、缺省都落回保守默认，绝不表示不封顶。"""
    raw = cfg or {}
    hourly = _positive_int(raw.get("hourly_cap"), DEFAULT_HOURLY_CAP)
    daily = _positive_int(raw.get("daily_cap"), DEFAULT_DAILY_CAP)
    return {"hourly_cap": hourly, "daily_cap": daily}


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return number


class PhoneLedger:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def stop(self, phone: str, *, reason: str = "", sample: str = "",
             now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO stops(phone, reason, sample, ts) VALUES(?,?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET reason=excluded.reason, "
                "sample=excluded.sample, ts=MIN(stops.ts, excluded.ts)",
                (phone, str(reason or "")[:120], str(sample or "")[:240], ts),
            )
            self._conn.commit()

    def is_stopped(self, phone: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM stops WHERE phone=?", (phone,),
            ).fetchone()
        return row is not None

    def has_first_touch(self, phone: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM first_touches WHERE phone=?", (phone,),
            ).fetchone()
        return row is not None

    def mark_first_touch(self, phone: str, *, conversation_id: str,
                         client_msg_id: str, now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO first_touches"
                "(phone, conversation_id, client_msg_id, ts) VALUES(?,?,?,?)",
                (phone, conversation_id, client_msg_id, ts),
            )
            self._conn.commit()

    def count_sent_since(self, since_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sends WHERE status='sent' AND ts>=?",
                (float(since_ts),),
            ).fetchone()
        return int(row["n"] if row else 0)

    def get_send(self, client_msg_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sends WHERE client_msg_id=?", (client_msg_id,),
            ).fetchone()
        return dict(row) if row else None

    def reserve(self, *, client_msg_id: str, phone: str, account_id: str,
                source: str, now: Optional[float] = None) -> Dict[str, Any]:
        """占坑。同一 id 已存在则原样返回，调用方不得再点发送。"""
        ts = float(now if now is not None else time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM sends WHERE client_msg_id=?", (client_msg_id,),
            ).fetchone()
            if existing is not None:
                return {"reserved": False, "duplicate": True, "row": dict(existing)}
            self._conn.execute(
                "INSERT INTO sends(client_msg_id, phone, account_id, source, status, error, ts)"
                " VALUES(?,?,?,?, 'reserved', '', ?)",
                (client_msg_id, phone, account_id, source, ts),
            )
            self._conn.commit()
        return {"reserved": True, "duplicate": False, "row": None}

    def finish(self, client_msg_id: str, status: str, *,
               error: str = "", now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE sends SET status=?, error=?, ts=? WHERE client_msg_id=?",
                (status, str(error or "")[:240], ts, client_msg_id),
            )
            self._conn.commit()

    def enqueue_touch(self, phone: str, *, account_id: str, opener: str,
                      now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO touch_queue"
                "(phone, account_id, opener, status, ts) VALUES(?,?,?,'pending',?)",
                (phone, account_id, opener, ts),
            )
            self._conn.commit()

    def pending_touches(self, limit: int = 20) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM touch_queue WHERE status='pending' "
                "ORDER BY ts ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def finish_touch(self, phone: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE touch_queue SET status=? WHERE phone=?",
                (status, phone),
            )
            self._conn.commit()
