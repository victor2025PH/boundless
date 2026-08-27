# -*- coding: utf-8 -*-
"""专属歌订单（实施58 P2）：客户点名「写/唱一首关于我们的歌」→ 订单 → 离线
填词+渲染+质检 → 人审 → 送达。

设计不变量：
- **运行时零 GPU 零 LLM**：聊天链只建订单行（幂等去重）；填词/渲染/质检全在
  独立进程 worker（scripts/song_custom_worker.py），审核送达在 /singing 页。
- 订单自带素材（peer_name + 客户原话 facts_json）——worker 不回读引擎库，
  歌词只能取材「对方亲口说过的话」（memory_grounding 哲学的订单版）。
- 状态机单向：pending → rendering → review → delivered / failed / rejected；
  claim 原子（WHERE status='pending'），审批原子（WHERE status='review'），
  双 worker / 双窗口并发天然安全（draft resolve 409 同款语义）。
- 每会话同时只允许一张活单（防「说三遍写歌=三张单」）；日订单帽保 GPU 预算。

库：<数据根>/config/song_orders.db（persona_media.db 同模式，WAL）。
门禁：tests/test_song_orders.py。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.companion.song_stock import data_root

logger = logging.getLogger("ai_chat_assistant.song_orders")

ORDERS_SUBPATH = os.path.join("config", "song_orders.db")

ACTIVE_STATUSES = ("pending", "rendering", "review")
FINAL_STATUSES = ("delivered", "failed", "rejected")
ALL_STATUSES = ACTIVE_STATUSES + FINAL_STATUSES

# 陈旧活单自动过期（天）：worker 长期没跑/渲染永败时不永久堵住该会话再下单
STALE_ACTIVE_DAYS = 3.0


# ── 意图检测（保守窄口径，与 detect_song_request 分工：那是「点播现货」，
# 这是「定制新歌」——后者优先级更高，两者都命中按定制处理） ────────────────────
_NEG_RE = re.compile(r"别(?:给我)?写歌|不用写歌|别唱|不要唱|唱什么唱")
_REQ_RES = [
    re.compile(r"(?:给|帮|为)我?(?:写|作|编|创作)一?首歌"),
    re.compile(r"(?:写|作|编)一?首(?:关于|属于|我们|专属)"),
    re.compile(r"唱一?首(?:关于|属于)(?:我们|我|咱)"),
    re.compile(r"专属(?:的)?歌|我们的歌.{0,6}(?:写|唱|做|作)"),
    re.compile(r"把(?:我|我们|咱们?).{0,8}(?:写|唱)进歌"),
    re.compile(r"\bwrite\s+(?:me\s+)?a\s+song\b", re.I),
    re.compile(r"\bsong\s+about\s+(?:us|me)\b", re.I),
]


def detect_custom_song_request(text: str) -> bool:
    """客户本条是否在求**定制**歌（纯函数，宁漏勿滥）。"""
    t = str(text or "").strip()
    if not t or len(t) > 400:
        return False
    if _NEG_RE.search(t):
        return False
    return any(r.search(t) for r in _REQ_RES)


# ── 订单库 ───────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    chat_key TEXT NOT NULL DEFAULT '',
    conv_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    voice_key TEXT NOT NULL DEFAULT '',
    peer_name TEXT NOT NULL DEFAULT '',
    request_text TEXT NOT NULL DEFAULT '',
    facts_json TEXT NOT NULL DEFAULT '[]',
    lyrics TEXT NOT NULL DEFAULT '',
    take_json TEXT NOT NULL DEFAULT '{}',
    audio_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    fail_reason TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL DEFAULT 0,
    delivered_ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id);
CREATE INDEX IF NOT EXISTS idx_orders_conv ON orders(conv_id, status);
"""


class SongOrderStore:
    """SQLite 订单库（WAL；进程内锁 + 条件 UPDATE 双保险）。"""

    def __init__(self, path: Optional[Path] = None, *,
                 root: Optional[Path] = None):
        self.path = Path(path) if path else (data_root(root) / ORDERS_SUBPATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return c

    # ── 建单 ────────────────────────────────────────────────────────────
    def create_order(self, *, platform: str, account_id: str, chat_key: str,
                     persona_id: str, voice_key: str = "",
                     peer_name: str = "", request_text: str = "",
                     facts: Optional[List[str]] = None,
                     daily_cap: int = 10,
                     now: Optional[float] = None) -> Optional[int]:
        """建订单。同会话已有活单 / 触日帽 → None（幂等去重，绝不重复下单）。"""
        ts = float(now if now is not None else time.time())
        cid = f"{platform}:{account_id}:{chat_key}"
        day0 = ts - (ts % 86400)
        with self._lock, self._conn() as c:
            self._expire_stale(c, ts)
            row = c.execute(
                "SELECT id FROM orders WHERE conv_id=? AND status IN (?,?,?)",
                (cid, *ACTIVE_STATUSES)).fetchone()
            if row is not None:
                return None
            if daily_cap > 0:
                n = c.execute(
                    "SELECT COUNT(*) FROM orders WHERE created_ts>=?",
                    (day0,)).fetchone()[0]
                if int(n) >= int(daily_cap):
                    return None
            cur = c.execute(
                "INSERT INTO orders (platform, account_id, chat_key, conv_id,"
                " persona_id, voice_key, peer_name, request_text, facts_json,"
                " status, created_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (platform, account_id, chat_key, cid, persona_id, voice_key,
                 peer_name[:80], request_text[:400],
                 json.dumps(list(facts or [])[:6], ensure_ascii=False),
                 "pending", ts, ts))
            return int(cur.lastrowid)

    def _expire_stale(self, c: sqlite3.Connection, now: float) -> None:
        c.execute(
            "UPDATE orders SET status='failed', fail_reason='stale_expired',"
            " updated_ts=? WHERE status IN (?,?,?) AND created_ts < ?",
            (now, *ACTIVE_STATUSES, now - STALE_ACTIVE_DAYS * 86400))

    # ── worker 侧 ───────────────────────────────────────────────────────
    def claim_next_pending(self, now: Optional[float] = None
                           ) -> Optional[Dict[str, Any]]:
        """原子认领最老 pending → rendering；无单回 None。"""
        ts = float(now if now is not None else time.time())
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE status='pending' "
                            "ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            hit = c.execute(
                "UPDATE orders SET status='rendering', updated_ts=? "
                "WHERE id=? AND status='pending'", (ts, row["id"])).rowcount
            if not hit:
                return None
            return dict(row)

    def set_review(self, oid: int, *, lyrics: str, take: Dict[str, Any],
                   audio_path: str, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        with self._lock, self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='review', lyrics=?, take_json=?,"
                " audio_path=?, updated_ts=? WHERE id=? AND status='rendering'",
                (lyrics, json.dumps(take, ensure_ascii=False),
                 audio_path, ts, oid)).rowcount > 0

    def set_failed(self, oid: int, reason: str,
                   now: Optional[float] = None,
                   take: Optional[Dict[str, Any]] = None) -> bool:
        """失败落码。``reason`` 用**稳定短码**（lyrics_rejected/render_failed/
        voice_missing:*）——前端按码译人话；技术细节进 ``take``（take_json 留档）。"""
        ts = float(now if now is not None else time.time())
        with self._lock, self._conn() as c:
            if take is not None:
                return c.execute(
                    "UPDATE orders SET status='failed', fail_reason=?,"
                    " take_json=?, updated_ts=? WHERE id=? AND status IN (?,?,?)",
                    (str(reason)[:200], json.dumps(take, ensure_ascii=False),
                     ts, oid, *ACTIVE_STATUSES)).rowcount > 0
            return c.execute(
                "UPDATE orders SET status='failed', fail_reason=?,"
                " updated_ts=? WHERE id=? AND status IN (?,?,?)",
                (str(reason)[:200], ts, oid, *ACTIVE_STATUSES)).rowcount > 0

    def retry(self, oid: int, now: Optional[float] = None) -> bool:
        """failed → pending（原子；worker 下轮自动重拾）。清失败码防陈旧展示。"""
        ts = float(now if now is not None else time.time())
        with self._lock, self._conn() as c:
            return c.execute(
                "UPDATE orders SET status='pending', fail_reason='',"
                " created_ts=?, updated_ts=? WHERE id=? AND status='failed'",
                (ts, ts, oid)).rowcount > 0

    def delete_order(self, oid: int) -> Optional[str]:
        """删终态单（delivered/failed/rejected）。回音频路径供调用方清文件；
        活单拒删（防把制作中的单从 worker 脚下抽走）。"""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT status, audio_path FROM orders WHERE id=?",
                            (oid,)).fetchone()
            if row is None or str(row["status"]) not in FINAL_STATUSES:
                return None
            c.execute("DELETE FROM orders WHERE id=?", (oid,))
            return str(row["audio_path"] or "")

    # ── 审核侧 ──────────────────────────────────────────────────────────
    def resolve_review(self, oid: int, *, to_status: str,
                       fail_reason: str = "",
                       now: Optional[float] = None) -> bool:
        """review → delivered/rejected（原子；已被别的窗口处置回 False）。"""
        if to_status not in ("delivered", "rejected"):
            return False
        ts = float(now if now is not None else time.time())
        with self._lock, self._conn() as c:
            return c.execute(
                "UPDATE orders SET status=?, fail_reason=?, updated_ts=?,"
                " delivered_ts=? WHERE id=? AND status='review'",
                (to_status, str(fail_reason)[:200], ts,
                 ts if to_status == "delivered" else 0, oid)).rowcount > 0

    # ── 查询 ────────────────────────────────────────────────────────────
    def has_active(self, conv_id: str) -> bool:
        """该会话是否已有活单（intake 用：活单在场→仍可承诺「在录了」）。"""
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM orders WHERE conv_id=? AND status IN (?,?,?) "
                "LIMIT 1", (str(conv_id), *ACTIVE_STATUSES)).fetchone() is not None

    def get(self, oid: int) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?",
                            (oid,)).fetchone()
            return dict(row) if row else None

    def list_orders(self, *, status: str = "", limit: int = 50
                    ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM orders"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args + (int(limit),))]

    def counts(self) -> Dict[str, int]:
        with self._conn() as c:
            return {r["status"]: int(r["n"]) for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}


_STORE: Optional[SongOrderStore] = None
_STORE_LOCK = threading.Lock()


def get_order_store(root: Optional[Path] = None) -> SongOrderStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SongOrderStore(root=root)
        return _STORE


def resolve_custom_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """companion.singing.custom 合并视图（默认关；review 默认开=人审后发）。"""
    try:
        raw = (((cfg or {}).get("companion") or {}).get("singing")
               or {}).get("custom") or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    try:
        cap = int(raw.get("daily_orders_cap", 10))
    except Exception:
        cap = 10
    return {
        "enabled": bool(raw.get("enabled", False)),
        "review": bool(raw.get("review", True)),
        "daily_orders_cap": cap,
    }


__all__ = [
    "SongOrderStore", "get_order_store", "detect_custom_song_request",
    "resolve_custom_cfg", "ACTIVE_STATUSES", "FINAL_STATUSES",
]
