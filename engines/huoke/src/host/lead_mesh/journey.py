# -*- coding: utf-8 -*-
"""Lead Journey — append-only 事件流 (Phase 5)。

所有 agent/人对 lead 的动作都 append 一条事件, 从不 update。
Journey 是 Dossier 的真实来源 (single source of truth), 其他聚合字段
(current_owner, last_action_at 等) 都从 journey 推导。

Action 枚举 (新增不改 schema):
    # A 机 (greeting + friend 链路)
    extracted            — 从群/搜索里抽到这个 lead
    friend_requested     — 加好友请求发出
    friend_accepted      — 对方接受好友请求
    friend_rejected      — 对方拒绝
    greeting_sent        — 打招呼消息发出
    greeting_fallback    — 走了 Messenger fallback 路径
    # B 机 (inbox + reply 链路)
    inbox_received       — 收到对方新消息
    reply_sent           — AI 自动回复发出
    greeting_replied     — 对方回复了我们的 greeting (跨 bot 归因)
    # 引流 / 交接
    referral_sent        — 引流话术发出
    referral_blocked     — 被去重/配额 gate 拒绝
    handoff_created      — 交接单生成
    handoff_acknowledged — 接收方已看到
    handoff_completed    — 已接上对话
    handoff_rejected     — 对方拒接
    # 系统 / 运营
    lead_merged          — 被合并到另一个 canonical
    lead_marked_duplicate
    human_intervention   — 人工介入操作
    risk_detected        — 检测到风控
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)


def append_journey(canonical_id: str,
                    actor: str,
                    action: str,
                    *,
                    actor_device: str = "",
                    platform: str = "",
                    data: Optional[Dict[str, Any]] = None) -> int:
    """Append 一条 journey 事件。

    Args:
        canonical_id: 必须。指向 leads_canonical.canonical_id
        actor: 必须。格式: "agent_a" / "agent_b" / "human:<username>" /
               "lead_self" / "system"
        action: 必须。见模块 docstring 的枚举 (不在枚举里的字符串也允许
                写入, 但会 warn log)
        actor_device: 可选。device_id 或 agent_machine_id
        platform: 可选。facebook / line / whatsapp / ...
        data: 可选。扩展字段, JSON 序列化进 data_json

    Returns:
        rowid of inserted row; 失败返回 0
    """
    if not canonical_id or not actor or not action:
        return 0
    data_str = ""
    if data:
        try:
            data_str = json.dumps(data, ensure_ascii=False, default=str)
        except Exception as e:
            logger.debug("[journey] data 序列化失败: %s", e)
            data_str = "{}"
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO lead_journey"
                " (canonical_id, actor, actor_device, platform, action, data_json)"
                " VALUES (?,?,?,?,?,?)",
                (canonical_id, actor, actor_device, platform, action, data_str),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.warning("[journey] append 失败 lead=%s action=%s: %s",
                       canonical_id[:12], action, e)
        return 0


def get_journey(canonical_id: str, *,
                limit: int = 100,
                action_prefix: str = "",
                since_iso: str = "") -> List[Dict[str, Any]]:
    """按时间升序返回某 lead 的事件列表。

    Args:
        canonical_id: lead 主键
        limit: 最多返回 N 条 (默认 100)
        action_prefix: 只返回 action 以此字符串开头 (如 "handoff_" / "greeting_")
        since_iso: 只返回 at >= since_iso 的事件 (时间字符串, UTC ISO)
    """
    if not canonical_id:
        return []
    sql = ("SELECT id, canonical_id, actor, actor_device, platform,"
           " action, data_json, at FROM lead_journey"
           " WHERE canonical_id=?")
    params: list = [canonical_id]
    if action_prefix:
        sql += " AND action LIKE ?"
        params.append(action_prefix + "%")
    if since_iso:
        sql += " AND at >= ?"
        params.append(since_iso)
    sql += " ORDER BY at ASC, id ASC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.pop("data_json") or "{}")
            except Exception:
                d["data"] = {}
            out.append(d)
        return out
    except Exception as e:
        logger.debug("[journey] get_journey 失败: %s", e)
        return []


def count_actions(canonical_id: str,
                   action: str = "",
                   since_hours: int = 0) -> int:
    """统计某 lead 某类事件数量 (去重/配额判断用)。"""
    if not canonical_id:
        return 0
    sql = "SELECT COUNT(*) FROM lead_journey WHERE canonical_id=?"
    params: list = [canonical_id]
    if action:
        sql += " AND action=?"
        params.append(action)
    if since_hours > 0:
        import datetime as _dt
        cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=since_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        sql += " AND at >= ?"
        params.append(cutoff)
    try:
        with _connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def last_action(canonical_id: str,
                action: str = "") -> Optional[Dict[str, Any]]:
    """取某 lead 最近一条事件 (可限定类型)。用于判断 current_owner 等。"""
    if not canonical_id:
        return None
    sql = ("SELECT id, canonical_id, actor, actor_device, platform,"
           " action, data_json, at FROM lead_journey WHERE canonical_id=?")
    params: list = [canonical_id]
    if action:
        sql += " AND action=?"
        params.append(action)
    sql += " ORDER BY at DESC, id DESC LIMIT 1"
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["data"] = json.loads(d.pop("data_json") or "{}")
        except Exception:
            d["data"] = {}
        return d
    except Exception:
        return None
