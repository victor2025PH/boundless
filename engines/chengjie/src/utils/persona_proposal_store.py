# -*- coding: utf-8 -*-
"""人设补丁提案持久化。对齐 persona_quiz_store：线程安全 SQLite，模块级软失败。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "config/persona_proposals.db"
DEFAULT_KEEP = 50
_STATUSES = frozenset({"pending", "accepted", "rejected", "applied", "stale"})

_DDL = """
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id      TEXT NOT NULL,
    ts              REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    kind            TEXT NOT NULL,
    field           TEXT NOT NULL,
    action          TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    current_json    TEXT NOT NULL DEFAULT 'null',
    proposed_json   TEXT NOT NULL DEFAULT 'null',
    rationale       TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0,
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    base_rev        TEXT NOT NULL DEFAULT '',
    needs_operator  INTEGER NOT NULL DEFAULT 1,
    reviewed_by     TEXT NOT NULL DEFAULT '',
    reviewed_ts     REAL
);
CREATE INDEX IF NOT EXISTS idx_proposals_persona_status
    ON proposals (persona_id, status, ts DESC);
"""


def _json_dump(val: Any) -> str:
    try:
        return json.dumps(val, ensure_ascii=False)
    except Exception:
        return "null"


def _json_load(raw: Any, default: Any) -> Any:
    try:
        return json.loads(str(raw if raw is not None else ""))
    except Exception:
        return default


class PersonaProposalStore:
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
            self._conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "persona_id": str(row["persona_id"] or ""),
            "ts": float(row["ts"] or 0.0),
            "status": str(row["status"] or ""),
            "kind": str(row["kind"] or ""),
            "field": str(row["field"] or ""),
            "action": str(row["action"] or ""),
            "dedupe_key": str(row["dedupe_key"] or ""),
            "current": _json_load(row["current_json"], None),
            "proposed": _json_load(row["proposed_json"], None),
            "rationale": str(row["rationale"] or ""),
            "confidence": float(row["confidence"] or 0),
            "evidence": _json_load(row["evidence_json"], []),
            "base_rev": str(row["base_rev"] or ""),
            "needs_operator": bool(int(row["needs_operator"] or 0)),
            "reviewed_by": str(row["reviewed_by"] or ""),
            "reviewed_ts": float(row["reviewed_ts"] or 0) if row["reviewed_ts"] is not None else None,
        }

    def insert_pending(self, row: dict) -> Optional[Dict[str, Any]]:
        """同一人设 pending + dedupe_key 已存在则返回已有行，不重复插。"""
        pid = str((row or {}).get("persona_id") or "").strip()
        key = str((row or {}).get("dedupe_key") or "").strip()
        if not pid or not key:
            return None
        now = time.time()
        with self._lock:
            exist = self._conn.execute(
                "SELECT * FROM proposals WHERE persona_id = ? AND status = 'pending'"
                " AND dedupe_key = ? ORDER BY id DESC LIMIT 1",
                (pid, key),
            ).fetchone()
            if exist:
                return self._row_to_dict(exist)
            cur = self._conn.execute(
                "INSERT INTO proposals ("
                " persona_id, ts, status, kind, field, action, dedupe_key,"
                " current_json, proposed_json, rationale, confidence, evidence_json,"
                " base_rev, needs_operator)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid, now, "pending",
                    str(row.get("kind") or ""),
                    str(row.get("field") or ""),
                    str(row.get("action") or ""),
                    key,
                    _json_dump(row.get("current")),
                    _json_dump(row.get("proposed")),
                    str(row.get("rationale") or ""),
                    float(row.get("confidence") or 0),
                    _json_dump(list(row.get("evidence") or [])),
                    str(row.get("base_rev") or ""),
                    1 if row.get("needs_operator") else 0,
                ),
            )
            self._conn.commit()
            rid = int(cur.lastrowid) if cur.lastrowid else 0
        if rid:
            try:
                self.prune(pid, keep=DEFAULT_KEEP)
            except Exception:
                logger.debug("[persona_proposal_store] prune 失败", exc_info=True)
            return self.get(pid, rid)
        return None

    def list(self, persona_id: str, status: str = "pending",
             limit: int = 50) -> List[Dict[str, Any]]:
        pid = str(persona_id or "").strip()
        if not pid:
            return []
        try:
            lim = max(1, min(200, int(limit)))
        except Exception:
            lim = 50
        st = str(status or "pending").strip()
        if st and st not in _STATUSES and st != "all":
            st = "pending"
        with self._lock:
            if st == "all":
                rows = self._conn.execute(
                    "SELECT * FROM proposals WHERE persona_id = ?"
                    " ORDER BY ts DESC LIMIT ?",
                    (pid, lim),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM proposals WHERE persona_id = ? AND status = ?"
                    " ORDER BY ts DESC LIMIT ?",
                    (pid, st, lim),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, persona_id: str, proposal_id: int) -> Optional[Dict[str, Any]]:
        pid = str(persona_id or "").strip()
        try:
            rid = int(proposal_id)
        except Exception:
            return None
        if not pid or rid <= 0:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proposals WHERE persona_id = ? AND id = ?",
                (pid, rid),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def mark_stale_outdated(self, persona_id: str, current_rev: str) -> int:
        """pending 且 base_rev 对不上当前档案 → stale。返回条数。"""
        pid = str(persona_id or "").strip()
        if not pid:
            return 0
        rev = str(current_rev or "")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE proposals SET status = 'stale' WHERE persona_id = ?"
                " AND status = 'pending' AND base_rev != ?",
                (pid, rev),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def set_status(self, persona_id: str, proposal_id: int, status: str,
                   *, reviewed_by: str = "",
                   proposed: Any = None, update_proposed: bool = False) -> Optional[Dict[str, Any]]:
        pid = str(persona_id or "").strip()
        st = str(status or "").strip()
        if st not in _STATUSES:
            return None
        rec = self.get(pid, proposal_id)
        if rec is None:
            return None
        now = time.time()
        with self._lock:
            if update_proposed:
                self._conn.execute(
                    "UPDATE proposals SET status = ?, reviewed_by = ?, reviewed_ts = ?,"
                    " proposed_json = ? WHERE persona_id = ? AND id = ?",
                    (st, str(reviewed_by or ""), now, _json_dump(proposed),
                     pid, int(proposal_id)),
                )
            else:
                self._conn.execute(
                    "UPDATE proposals SET status = ?, reviewed_by = ?, reviewed_ts = ?"
                    " WHERE persona_id = ? AND id = ?",
                    (st, str(reviewed_by or ""), now, pid, int(proposal_id)),
                )
            self._conn.commit()
        return self.get(pid, proposal_id)

    def pending_count(self, persona_id: str) -> int:
        pid = str(persona_id or "").strip()
        if not pid:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM proposals WHERE persona_id = ? AND status = 'pending'",
                (pid,),
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def prune(self, persona_id: str, keep: int = DEFAULT_KEEP) -> int:
        pid = str(persona_id or "").strip()
        if not pid:
            return 0
        try:
            keep_n = max(1, int(keep))
        except Exception:
            keep_n = DEFAULT_KEEP
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM proposals WHERE persona_id = ? AND id NOT IN ("
                "  SELECT id FROM ("
                "    SELECT id FROM proposals WHERE persona_id = ?"
                "    ORDER BY ts DESC, id DESC LIMIT ?"
                "  )"
                ")",
                (pid, pid, keep_n),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)


_STORE: Optional[PersonaProposalStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure(db_path: Any = DEFAULT_DB_PATH) -> Optional[PersonaProposalStore]:
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _DB_PATH = str(db_path)
        try:
            _STORE = PersonaProposalStore(_DB_PATH)
        except Exception:
            logger.warning("[persona_proposal_store] 建库失败", exc_info=True)
            _STORE = None
        return _STORE


def get() -> Optional[PersonaProposalStore]:
    global _STORE
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                try:
                    _STORE = PersonaProposalStore(_DB_PATH)
                except Exception:
                    logger.warning("[persona_proposal_store] 懒建库失败", exc_info=True)
                    _STORE = None
    return _STORE


def reset() -> None:
    global _STORE
    with _CFG_LOCK:
        _STORE = None
