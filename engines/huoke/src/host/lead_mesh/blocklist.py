# -*- coding: utf-8 -*-
"""Phase 8h: Lead Blocklist — 跨 device/agent 的 peer 骚扰保护 (2026-04-24).

运营在 Dashboard 点 "加入 blocklist" → A 端 add_friend / send_greeting
入口主动 skip, 避免反复骚扰.

原则:
  * canonical_id 作主键 (跨 device 身份唯一)
  * 不干扰 B 端 (B 有 peer_cooldown_handoff 自成体系, 用途不同 —
    B 是跨渠道骚扰保护, A blocklist 是运营手工"不再联系")
  * 硬删 (DELETE) — 可追溯靠 journey 事件足够, 不加 deleted_at 软删复杂度
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from src.host.database import _connect


def add_to_blocklist(canonical_id: str,
                       reason: str = "",
                       note: str = "",
                       created_by: str = "") -> bool:
    """加入 blocklist. canonical_id 唯一 (已存在时更新 reason/note).

    Returns True 新加, False 仅更新 (已存在).
    """
    if not canonical_id:
        return False
    with _connect() as conn:
        # 已存在就 UPDATE (允许运营改 reason/note)
        cur = conn.execute(
            "SELECT 1 FROM lead_blocklist WHERE canonical_id=? LIMIT 1",
            (canonical_id,))
        exists = cur.fetchone() is not None
        if exists:
            conn.execute(
                "UPDATE lead_blocklist SET reason=?, note=?, created_by=?"
                " WHERE canonical_id=?",
                (reason or "", note or "", created_by or "", canonical_id))
        else:
            conn.execute(
                "INSERT INTO lead_blocklist (canonical_id, reason, note, created_by)"
                " VALUES (?, ?, ?, ?)",
                (canonical_id, reason or "", note or "", created_by or ""))
        conn.commit()
    return not exists


def remove_from_blocklist(canonical_id: str) -> bool:
    """移除. 返回 True 真的删了 (原来在), False 原来就不在."""
    if not canonical_id:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM lead_blocklist WHERE canonical_id=?",
            (canonical_id,))
        conn.commit()
        return cur.rowcount > 0


def is_blocklisted(canonical_id: str) -> bool:
    """是否在 blocklist. A 端前置检查用, 高频调用, 只 SELECT 主键扫描."""
    if not canonical_id:
        return False
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM lead_blocklist WHERE canonical_id=? LIMIT 1",
                (canonical_id,)).fetchone()
        return row is not None
    except Exception:
        return False  # DB 问题时保守放行


def get_blocklist_entry(canonical_id: str) -> Optional[Dict[str, Any]]:
    """取单条记录 (包含 reason/note/created_at), 为 journey 事件附 meta 用."""
    if not canonical_id:
        return None
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute(
            "SELECT canonical_id, reason, note, created_at, created_by"
            " FROM lead_blocklist WHERE canonical_id=?",
            (canonical_id,)).fetchone()
    return dict(row) if row else None


def list_blocklist(limit: int = 50) -> List[Dict[str, Any]]:
    """最近加入的在前 (created_at DESC)."""
    limit = max(1, min(200, int(limit)))
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(
            "SELECT canonical_id, reason, note, created_at, created_by"
            " FROM lead_blocklist"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_blocklist() -> int:
    try:
        with _connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM lead_blocklist").fetchone()
        return int(n[0]) if n else 0
    except Exception:
        return 0
