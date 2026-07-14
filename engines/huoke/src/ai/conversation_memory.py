# -*- coding: utf-8 -*-
"""
ConversationMemory — SQLite-backed conversation history with windowed context.

Replaces the in-memory deque-based ConversationHistory with persistent storage
and a configurable context window (default 15 messages + summary).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from src.host.device_registry import data_file

log = logging.getLogger(__name__)

_DB_PATH = data_file("conversations.db")
_CONTEXT_WINDOW = 15
_SUMMARY_THRESHOLD = 30


class ConversationMemory:
    """Per-lead persistent conversation storage with cross-platform linking."""

    _instance: Optional["ConversationMemory"] = None
    _init_lock = threading.Lock()

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @classmethod
    def get_instance(cls) -> "ConversationMemory":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path), timeout=10)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_conv_lead
                ON conversations(lead_id);
            CREATE INDEX IF NOT EXISTS idx_conv_lead_plat
                ON conversations(lead_id, platform);
            CREATE INDEX IF NOT EXISTS idx_conv_ts
                ON conversations(timestamp);

            CREATE TABLE IF NOT EXISTS conversation_summaries (
                lead_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                message_count INTEGER DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_aliases (
                alias TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_alias_lead
                ON lead_aliases(lead_id);
        """)
        conn.commit()

    def add_message(self, lead_id: str, role: str, content: str,
                    platform: str = "", metadata: Optional[dict] = None):
        """Record a message in conversation history."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO conversations (lead_id, platform, role, content, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (lead_id, platform, role, content, time.time(),
             json.dumps(metadata or {})),
        )
        conn.commit()

    def get_context(self, lead_id: str, limit: int = _CONTEXT_WINDOW,
                    platform: str = "") -> List[Dict[str, str]]:
        """Return recent messages as LLM-ready context list.

        If a summary exists, prepend it as a system message.
        """
        conn = self._get_conn()
        if platform:
            rows = conn.execute(
                "SELECT role, content FROM conversations "
                "WHERE lead_id=? AND platform=? ORDER BY timestamp DESC LIMIT ?",
                (lead_id, platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content FROM conversations "
                "WHERE lead_id=? ORDER BY timestamp DESC LIMIT ?",
                (lead_id, limit),
            ).fetchall()

        messages = [{"role": r["role"], "content": r["content"]}
                    for r in reversed(rows)]

        summary_row = conn.execute(
            "SELECT summary FROM conversation_summaries WHERE lead_id=?",
            (lead_id,),
        ).fetchone()
        if summary_row and summary_row["summary"]:
            messages.insert(0, {
                "role": "system",
                "content": f"Previous conversation summary: {summary_row['summary']}",
            })
        return messages

    def get_message_count(self, lead_id: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversations WHERE lead_id=?",
            (lead_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def link_lead(self, alias: str, lead_id: str):
        """Link a platform-specific identifier to a canonical lead_id."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO lead_aliases (alias, lead_id) VALUES (?, ?)",
            (alias, lead_id),
        )
        conn.commit()

    def resolve_lead(self, alias: str) -> str:
        """Resolve an alias to a canonical lead_id, or return alias as-is."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT lead_id FROM lead_aliases WHERE alias=?", (alias,),
        ).fetchone()
        return row["lead_id"] if row else alias

    def set_summary(self, lead_id: str, summary: str, message_count: int = 0):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO conversation_summaries "
            "(lead_id, summary, message_count, updated_at) VALUES (?, ?, ?, ?)",
            (lead_id, summary, message_count, time.time()),
        )
        conn.commit()

    def should_summarize(self, lead_id: str) -> bool:
        """Check if conversation is long enough to benefit from summarization."""
        conn = self._get_conn()
        total = self.get_message_count(lead_id)
        row = conn.execute(
            "SELECT message_count FROM conversation_summaries WHERE lead_id=?",
            (lead_id,),
        ).fetchone()
        last_summarized = row["message_count"] if row else 0
        return (total - last_summarized) >= _SUMMARY_THRESHOLD

    def get_all_messages(self, lead_id: str,
                         platform: str = "") -> List[Dict]:
        """Retrieve full conversation history for a lead."""
        conn = self._get_conn()
        if platform:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE lead_id=? AND platform=? "
                "ORDER BY timestamp", (lead_id, platform),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE lead_id=? ORDER BY timestamp",
                (lead_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_lead(self, lead_id: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM conversations WHERE lead_id=?", (lead_id,))
        conn.execute("DELETE FROM conversation_summaries WHERE lead_id=?",
                     (lead_id,))
        conn.execute("DELETE FROM lead_aliases WHERE lead_id=?", (lead_id,))
        conn.commit()

    def list_leads(self, limit: int = 100) -> List[Dict]:
        """List leads with recent activity."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT lead_id, platform, COUNT(*) as msg_count, "
            "MAX(timestamp) as last_active "
            "FROM conversations GROUP BY lead_id "
            "ORDER BY last_active DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
