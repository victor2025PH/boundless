"""R9 危机事件落库/审计。

R4→R6→R8 形成了"输入预防 → 输出兜底 → 真人接管"的安全链，但全程只有 webhook + 日志，
**留不下结构化记录**。一个会触及真实心理危机的陪聊产品，必须能事后复盘、合规审计、
追踪每起危机是否被人工处理。本模块在 SQLite 落一张轻量 ``crisis_event`` 表：

- 记录：时间 / 用户 / 会话 / 等级 / 类别 / 连击 / 是否触发升级 / 是否触发安全兜底 / 短摘要；
- 查询：最近事件、未处理事件、按用户筛；
- 处置：标记"已人工处理"+ 处理人 + 备注。

隐私：默认关（由 ``companion.wellbeing.crisis_audit`` 开），且只存**短摘要**（≤120 字）非全文。
纯存储、平台无关、可单测。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("CrisisEventStore")

# ── 落库即广播（#185 D7，2026-09-05）────────────────────────────────────────
# R8 升级此前只走 escalation_needed webhook；桌面包没配 webhook ⇒ severe 命中谁也
# 不知道。留痕行是「有人该看一眼」这个事实的唯一结构化落点，所以在 record() 成功
# 后把整行推给进程内监听器（工作台徽标/置顶/案例桥接由 wellbeing_escalation_bridge
# 订阅）。**不改 skill_manager**：它照常只调 record()。
# 监听器异常一律吞掉——审计与桥接都是旁路，绝不反噬主回复。
CrisisEventListener = Callable[[Dict[str, Any]], None]
_LISTENERS: List[CrisisEventListener] = []
_LISTENERS_LOCK = threading.Lock()


def add_crisis_event_listener(fn: CrisisEventListener) -> None:
    """注册「危机事件已落库」监听器（幂等：同一可调用对象只登记一次）。"""
    with _LISTENERS_LOCK:
        if fn not in _LISTENERS:
            _LISTENERS.append(fn)


def remove_crisis_event_listener(fn: CrisisEventListener) -> bool:
    with _LISTENERS_LOCK:
        try:
            _LISTENERS.remove(fn)
            return True
        except ValueError:
            return False


def _notify_listeners(event: Dict[str, Any]) -> None:
    with _LISTENERS_LOCK:
        fns = list(_LISTENERS)
    for fn in fns:
        try:
            fn(dict(event))
        except Exception:  # noqa: BLE001
            logger.debug("crisis_event listener failed: %r", fn, exc_info=True)


class CrisisEventStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS crisis_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        chat_id TEXT NOT NULL DEFAULT '',
        level TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        streak INTEGER NOT NULL DEFAULT 1,
        escalated INTEGER NOT NULL DEFAULT 0,
        safety_override INTEGER NOT NULL DEFAULT 0,
        excerpt TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        handled INTEGER NOT NULL DEFAULT 0,
        handled_by TEXT NOT NULL DEFAULT '',
        handled_at REAL,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_crisis_created ON crisis_event(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_crisis_user ON crisis_event(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_crisis_handled ON crisis_event(handled, created_at DESC);
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def record(
        self,
        *,
        user_id: str,
        level: str,
        chat_id: str = "",
        category: str = "",
        streak: int = 1,
        escalated: bool = False,
        safety_override: bool = False,
        excerpt: str = "",
    ) -> Optional[int]:
        """落一条危机事件；返回行 id，失败返回 None（绝不抛，避免影响主回复）。

        落库成功后同步广播给 ``add_crisis_event_listener`` 登记的监听器（携 ``id``）。
        """
        now = time.time()
        row: Dict[str, Any] = {
            "user_id": str(user_id), "chat_id": str(chat_id), "level": str(level),
            "category": str(category)[:32], "streak": int(streak),
            "escalated": bool(escalated), "safety_override": bool(safety_override),
            "excerpt": str(excerpt or "")[:120], "created_at": now,
        }
        try:
            cur = self._conn.execute(
                "INSERT INTO crisis_event (user_id, chat_id, level, category, streak,"
                " escalated, safety_override, excerpt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["user_id"], row["chat_id"], row["level"], row["category"],
                    row["streak"], 1 if escalated else 0, 1 if safety_override else 0,
                    row["excerpt"], now,
                ),
            )
            self._conn.commit()
            rid = int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("crisis_event record failed: %s", e)
            return None
        if rid is not None:
            row["id"] = rid
            _notify_listeners(row)
        return rid

    def list_recent(
        self,
        limit: int = 50,
        *,
        only_unhandled: bool = False,
        user_prefix: str = "",
        match_key: str = "",
        since_id: int = 0,
        only_escalated: bool = False,
    ) -> List[Dict[str, Any]]:
        """最近危机事件。

        ``user_prefix``：仅按 ``user_id`` 前缀筛（后台审计页用）。
        ``match_key``（R9e）：按 ``user_id`` 前缀**或** ``chat_id`` 精确匹配——一个 key
        同时覆盖 1:1 私聊（key=对端 user_id）与群聊（key=群 chat_id），供坐席侧栏用。
        ``since_id`` / ``only_escalated``（#185 桥接补扫）：只取 id 大于水位且触发过
        升级的行——看门狗兜底「监听器没接上时漏掉的升级事件」。
        """
        lim = max(1, min(int(limit or 50), 500))
        where = []
        params: List[Any] = []
        if only_unhandled:
            where.append("handled = 0")
        if int(since_id or 0) > 0:
            where.append("id > ?")
            params.append(int(since_id))
        if only_escalated:
            where.append("escalated = 1")
        if user_prefix:
            where.append("user_id LIKE ?")
            params.append(f"{user_prefix}%")
        if match_key:
            where.append("(user_id LIKE ? OR chat_id = ?)")
            params.append(f"{match_key}%")
            params.append(str(match_key))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(lim)
        try:
            rows = self._conn.execute(
                "SELECT id, user_id, chat_id, level, category, streak, escalated,"
                " safety_override, excerpt, created_at, handled, handled_by, handled_at, note"
                f" FROM crisis_event{clause} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("crisis_event list failed: %s", e)
            return []
        cols = [
            "id", "user_id", "chat_id", "level", "category", "streak", "escalated",
            "safety_override", "excerpt", "created_at", "handled", "handled_by",
            "handled_at", "note",
        ]
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(zip(cols, r))
            d["escalated"] = bool(d["escalated"])
            d["safety_override"] = bool(d["safety_override"])
            d["handled"] = bool(d["handled"])
            out.append(d)
        return out

    def mark_handled(
        self, event_id: int, *, handled_by: str = "", note: str = "",
    ) -> bool:
        try:
            cur = self._conn.execute(
                "UPDATE crisis_event SET handled = 1, handled_by = ?, handled_at = ?,"
                " note = ? WHERE id = ?",
                (str(handled_by)[:64], time.time(), str(note)[:500], int(event_id)),
            )
            self._conn.commit()
            return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("crisis_event mark_handled failed: %s", e)
            return False

    def max_id(self) -> int:
        """当前最大行 id（0=空表）；看门狗补扫水位初始化用。"""
        try:
            row = self._conn.execute("SELECT MAX(id) FROM crisis_event").fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    def count_since(self, since_ts: float) -> int:
        """``since_ts`` 之后的事件数（审计页「近 N 天」空态口径）。"""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM crisis_event WHERE created_at >= ?",
                (float(since_ts),),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def count(self, *, only_unhandled: bool = False) -> int:
        try:
            q = "SELECT COUNT(*) FROM crisis_event"
            if only_unhandled:
                q += " WHERE handled = 0"
            row = self._conn.execute(q).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


__all__ = [
    "CrisisEventStore",
    "add_crisis_event_listener",
    "remove_crisis_event_listener",
]
