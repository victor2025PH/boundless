"""真机发送的电脑侧账本：拒收、首触、冷启动限额、client_msg_id。

对话正文在收件箱 messages。这张库只记「能不能发、发过没有」。
每小时、每天的条数只统计冷启动（对方还没回过）。已经有来往的回复不占这个额度。
配成 0 也不会变成不封顶，而是落回默认的 20/80。
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
    ts REAL NOT NULL,
    cold INTEGER NOT NULL DEFAULT 0,
    content_key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS device_pause (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    until_ts REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deal_pending (
    phone TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_confirm',
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
CREATE TABLE IF NOT EXISTS inbounds (
    inbound_key TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    replied INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL
);
"""


def resolve_caps(cfg: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """冷启动的每小时、每天上限。0、负数、缺省都落回 20/80，绝不表示不封顶。

    已经有对方来信的会话不使用这组数字。
    ``first_touch_hourly_cap`` / ``first_touch_daily_cap`` 优先于旧键名。
    """
    raw = cfg or {}
    hourly = _positive_int(
        raw.get("first_touch_hourly_cap", raw.get("hourly_cap")),
        DEFAULT_HOURLY_CAP,
    )
    daily = _positive_int(
        raw.get("first_touch_daily_cap", raw.get("daily_cap")),
        DEFAULT_DAILY_CAP,
    )
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
            for statement in (
                "ALTER TABLE sends ADD COLUMN cold INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE sends ADD COLUMN content_key TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE sends ADD COLUMN body TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    self._conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
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

    def count_sent_since(self, since_ts: float, *, cold_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM sends WHERE status='sent' AND ts>=?"
        if cold_only:
            sql += " AND cold=1"
        with self._lock:
            row = self._conn.execute(sql, (float(since_ts),)).fetchone()
        return int(row["n"] if row else 0)

    def pause_until(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT until_ts FROM device_pause WHERE id=1"
            ).fetchone()
        return float(row["until_ts"] if row else 0)

    def set_pause(self, until_ts: float, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO device_pause(id, until_ts, reason) VALUES(1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET until_ts=excluded.until_ts, "
                "reason=excluded.reason",
                (float(until_ts), str(reason or "")[:120]),
            )
            self._conn.commit()

    def pending_deal(self, phone: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deal_pending WHERE phone=?", (phone,),
            ).fetchone()
        return dict(row) if row else None

    def mark_pending_deal(self, phone: str, *, conversation_id: str, keyword: str,
                          now: Optional[float] = None) -> bool:
        """写入待确认。已经在待确认里则不再插一行。返回是否新写入。"""
        ts = float(now if now is not None else time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM deal_pending WHERE phone=?", (phone,),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "pending_confirm":
                return False
            self._conn.execute(
                "INSERT INTO deal_pending(phone, conversation_id, keyword, status, ts)"
                " VALUES(?,?,?,'pending_confirm',?) "
                "ON CONFLICT(phone) DO UPDATE SET keyword=excluded.keyword, "
                "status='pending_confirm', ts=excluded.ts, "
                "conversation_id=excluded.conversation_id",
                (phone, conversation_id, str(keyword or "")[:80], ts),
            )
            self._conn.commit()
        return True

    def confirm_pending_deal(self, phone: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE deal_pending SET status='confirmed' WHERE phone=?",
                (phone,),
            )
            self._conn.commit()

    def get_send(self, client_msg_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sends WHERE client_msg_id=?", (client_msg_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_content_sent(self, content_key: str, *, now: Optional[float] = None) -> int:
        """把这句话已经失败的记录改成 sent。已经是 sent 的不动。"""
        key = str(content_key or "").strip()
        if not key:
            return 0
        ts = float(now if now is not None else time.time())
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE sends SET status='sent', error='', ts=? "
                "WHERE content_key=? AND status!='sent'",
                (ts, key),
            )
            changed = int(cursor.rowcount or 0)
            if changed:
                self._conn.commit()
                return changed
            already = self._conn.execute(
                "SELECT 1 FROM sends WHERE content_key=? AND status='sent' LIMIT 1",
                (key,),
            ).fetchone()
            if already is not None:
                return 0
            self._conn.execute(
                "INSERT INTO sends(client_msg_id, phone, account_id, source, status, error, ts, cold, content_key)"
                " VALUES(?,?,?,?, 'sent', '', ?, 0, ?)",
                ("visible-" + key[:24], "", "", "acceptance", ts, key),
            )
            self._conn.commit()
            return 1

    def has_sent_content(self, content_key: str) -> bool:
        """同一句话已经发出去过。失败且没点发送的记录不算。"""
        key = str(content_key or "").strip()
        if not key:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sends WHERE content_key=? AND status='sent' LIMIT 1",
                (key,),
            ).fetchone()
        return row is not None

    def reserve(self, *, client_msg_id: str, phone: str, account_id: str,
                source: str, now: Optional[float] = None, cold: bool = False,
                content_key: str = "", body: str = "") -> Dict[str, Any]:
        """占坑。同一 id 已存在则原样返回，调用方不得再点发送。"""
        ts = float(now if now is not None else time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM sends WHERE client_msg_id=?", (client_msg_id,),
            ).fetchone()
            if existing is not None:
                return {"reserved": False, "duplicate": True, "row": dict(existing)}
            self._conn.execute(
                "INSERT INTO sends(client_msg_id, phone, account_id, source, status, error, ts, cold, content_key, body)"
                " VALUES(?,?,?,?, 'reserved', '', ?, ?, ?, ?)",
                (
                    client_msg_id, phone, account_id, source, ts,
                    1 if cold else 0, str(content_key or ""), str(body or "")[:500],
                ),
            )
            self._conn.commit()
        return {"reserved": True, "duplicate": False, "row": None}

    def note_inbound(self, phone: str, text: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        """记下一条入站。同一文本哈希只算一次，回复过就不再算未回复。"""
        import hashlib
        import re
        norm = re.sub(r"\s+", "", str(text or ""))
        key = hashlib.sha256(f"{phone}|{norm}".encode("utf-8")).hexdigest()[:40]
        ts = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT replied FROM inbounds WHERE inbound_key=?", (key,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO inbounds(inbound_key, phone, body, replied, ts) VALUES(?,?,?,0,?)",
                    (key, phone, str(text or "")[:500], ts),
                )
                self._conn.commit()
                return {"key": key, "fresh": True, "unreplied": True}
        return {"key": key, "fresh": False, "unreplied": int(row["replied"]) == 0}

    def tracks_inbound(self, phone: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM inbounds WHERE phone=? LIMIT 1", (phone,),
            ).fetchone()
        return row is not None

    def has_unreplied(self, phone: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM inbounds WHERE phone=? AND replied=0 LIMIT 1",
                (phone,),
            ).fetchone()
        return row is not None

    def mark_replied(self, phone: str, text: str) -> None:
        import hashlib
        import re
        norm = re.sub(r"\s+", "", str(text or ""))
        key = hashlib.sha256(f"{phone}|{norm}".encode("utf-8")).hexdigest()[:40]
        with self._lock:
            self._conn.execute(
                "UPDATE inbounds SET replied=1 WHERE inbound_key=? AND phone=?",
                (key, phone),
            )
            self._conn.commit()

    def list_sends(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sends ORDER BY ts ASC",
            ).fetchall()
        return [dict(row) for row in rows]

    def unsent_rows(self, phone: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sends WHERE phone=? AND status NOT IN ('sent', 'dry_run') "
                "ORDER BY ts DESC",
                (phone,),
            ).fetchall()
        return [dict(row) for row in rows]

    def release(self, client_msg_id: str) -> None:
        """放掉尚未真正发出的占坑，让冷却结束后同一条还能再试。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM sends WHERE client_msg_id=? AND status='reserved'",
                (client_msg_id,),
            )
            self._conn.commit()

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
