# -*- coding: utf-8 -*-
"""LockManager — 跨 agent 软锁 (Phase 5)。

SQLite 表级软锁, TTL 过期机制。与 fb_concurrency (进程内 threading.Lock)
不同: 本模块解决**多进程/多机器**的跨 agent 并发, 单进程内走 fb_concurrency
更快。

典型场景:
    # agent_b 想对 lead_X 做引流, 先锁住 2 分钟
    with acquire_lock(lead_id, "referring", by="agent_b", ttl=120) as ok:
        if not ok:
            return  # 别的 agent 已经在对它操作
        # ... 做引流 ...

自动清理: 超时的锁不会主动删, 下次 acquire 时发现 expires_at < now 直接覆盖。
"""
from __future__ import annotations

import datetime as _dt
import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 180


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _expires_iso(ttl_sec: int) -> str:
    return (_dt.datetime.utcnow() + _dt.timedelta(seconds=int(ttl_sec))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def acquire_lock_raw(canonical_id: str, action: str, *,
                      by: str,
                      ttl_sec: int = DEFAULT_TTL_SEC) -> bool:
    """原子获取锁。返回 True = 拿到; False = 别人持有且未过期。

    用 BEGIN IMMEDIATE + INSERT OR REPLACE 实现: 若已存在且未过期 → 失败;
    过期 → 覆盖。
    """
    if not canonical_id or not action or not by:
        return False
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 检查现有锁
            row = conn.execute(
                "SELECT locked_by, expires_at FROM lead_locks"
                " WHERE canonical_id=? AND action=?",
                (canonical_id, action)).fetchone()
            now = _now_iso()
            if row:
                owner, exp = row[0], row[1]
                if exp > now and owner != by:
                    conn.execute("ROLLBACK")
                    return False
                # 过期 或 自己持有 → 续约
            conn.execute(
                "INSERT OR REPLACE INTO lead_locks"
                " (canonical_id, action, locked_by, acquired_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                (canonical_id, action, by, now, _expires_iso(ttl_sec)))
            conn.execute("COMMIT")
        return True
    except Exception as e:
        logger.debug("[lock] acquire 失败 %s/%s: %s", canonical_id[:12], action, e)
        return False


def release_lock(canonical_id: str, action: str, *, by: str = "") -> bool:
    """释放锁 (必须是持有者, 非持有者释放失败)。by 为空则强制释放 (管理员用)。"""
    if not canonical_id or not action:
        return False
    try:
        with _connect() as conn:
            if by:
                cur = conn.execute(
                    "DELETE FROM lead_locks WHERE canonical_id=? AND action=?"
                    " AND locked_by=?",
                    (canonical_id, action, by))
            else:
                cur = conn.execute(
                    "DELETE FROM lead_locks WHERE canonical_id=? AND action=?",
                    (canonical_id, action))
            return (cur.rowcount or 0) > 0
    except Exception as e:
        logger.debug("[lock] release 失败: %s", e)
        return False


def is_locked(canonical_id: str, action: str) -> Optional[dict]:
    """查询当前锁状态; 未锁或已过期返回 None。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT locked_by, acquired_at, expires_at FROM lead_locks"
                " WHERE canonical_id=? AND action=?",
                (canonical_id, action)).fetchone()
        if not row:
            return None
        if row[2] <= _now_iso():
            return None
        return {"locked_by": row[0], "acquired_at": row[1], "expires_at": row[2]}
    except Exception:
        return None


@contextmanager
def acquire_lock(canonical_id: str, action: str, *,
                  by: str,
                  ttl_sec: int = DEFAULT_TTL_SEC) -> Iterator[bool]:
    """上下文管理器版本。yield True=拿到锁, False=未拿到 (可继续决策)。

    即使 yield False 也进 context, 由调用方决定放弃或等待。
    自动释放: yield 结束后如果本 acquire 真的拿到了锁, 会 release。
    """
    got = acquire_lock_raw(canonical_id, action, by=by, ttl_sec=ttl_sec)
    try:
        yield got
    finally:
        if got:
            release_lock(canonical_id, action, by=by)
