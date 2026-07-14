# -*- coding: utf-8 -*-
"""Agent Mesh — Agent 间消息总线 (Phase 5)。

双通道设计:
  * **SQLite 持久化** (默认): agent_messages 表, 断电不丢, 可审计, 支持多消费者
  * **HTTP 实时**: POST /agent-mesh/messages 立即入库 + 可选触发订阅者 webhook

消息类型:
  * notification — 单向异步, 告诉对方"我做了什么" (默认)
  * query        — request-response, 同步调用; 走 correlation_id 配对
  * reply        — 对 query 的响应
  * command      — 指令 (如"接管这个 lead"), 需幂等 key
  * ack          — 对 command 的确认

消费模式:
  * Pull: ``poll_messages(to_agent, limit)`` 轮询未读消息
  * Sync Query: ``query_sync(to, type, payload, timeout=30)`` 发 query 并阻塞
    等 reply (基于 correlation_id 轮询 + timeout)

为什么不用 Redis/RabbitMQ
------------------------
项目目标是**零外部依赖**可跑, SQLite 在 2-10 QPS 级别完全够用 (两个 Claude
之间通信频率不会超过)。未来真有多机集群需求, 再抽象接口换底层。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)

# 消息类型常量
MSG_NOTIFICATION = "notification"
MSG_QUERY = "query"
MSG_REPLY = "reply"
MSG_COMMAND = "command"
MSG_ACK = "ack"

VALID_MSG_TYPES = frozenset({
    MSG_NOTIFICATION, MSG_QUERY, MSG_REPLY, MSG_COMMAND, MSG_ACK,
})

# 状态常量
STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_FAILED = "failed"


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def send_message(*,
                  from_agent: str,
                  to_agent: str,
                  message_type: str = MSG_NOTIFICATION,
                  canonical_id: str = "",
                  payload: Optional[Dict[str, Any]] = None,
                  correlation_id: str = "") -> str:
    """入库一条消息, 返回 correlation_id (新建或延续)。

    Args:
        from_agent / to_agent: agent 标识, 如 "agent_a" / "agent_b" / "human:ops_01"
        message_type: 见常量
        canonical_id: 可选, 关联 lead
        payload: 任意 dict, JSON 序列化入 payload_json
        correlation_id: query/reply 配对时必填; 新 query 则自动生成

    Returns:
        correlation_id (query/reply 用)
    """
    if not from_agent or not to_agent or not message_type:
        raise ValueError("from_agent / to_agent / message_type 必填")
    if message_type not in VALID_MSG_TYPES:
        logger.warning("[mesh] 未知消息类型 %s, 仍写入", message_type)

    cid = correlation_id or str(uuid.uuid4())
    payload_str = "{}"
    if payload:
        try:
            payload_str = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            payload_str = "{}"
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages"
                " (from_agent, to_agent, canonical_id, message_type,"
                "  correlation_id, payload_json, status)"
                " VALUES (?,?,?,?,?,?,?)",
                (from_agent, to_agent, canonical_id or "",
                 message_type, cid, payload_str, STATUS_PENDING),
            )
    except Exception as e:
        logger.warning("[mesh] send_message 失败: %s", e)
        return ""
    return cid


def poll_messages(to_agent: str, *,
                    message_type: str = "",
                    status: str = STATUS_PENDING,
                    limit: int = 50) -> List[Dict[str, Any]]:
    """拉取给指定 agent 的消息列表 (按时间升序, 最老的先处理)。

    消费者应该处理完每条后调 mark_delivered / mark_acknowledged。
    """
    if not to_agent:
        return []
    sql = ("SELECT id, from_agent, to_agent, canonical_id, message_type,"
           " correlation_id, payload_json, status, created_at, delivered_at,"
           " acknowledged_at, error FROM agent_messages"
           " WHERE to_agent=?")
    params: list = [to_agent]
    if message_type:
        sql += " AND message_type=?"
        params.append(message_type)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out
    except Exception as e:
        logger.debug("[mesh] poll 失败: %s", e)
        return []


def mark_delivered(message_id: int) -> bool:
    """消费者读到消息后打 delivered 标记 (不等于已处理)。"""
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE agent_messages SET status=?, delivered_at=datetime('now')"
                " WHERE id=? AND status=?",
                (STATUS_DELIVERED, int(message_id), STATUS_PENDING))
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def mark_acknowledged(message_id: int, error: str = "") -> bool:
    """消费者处理完打 acknowledged/failed 终态。"""
    status = STATUS_FAILED if error else STATUS_ACKNOWLEDGED
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE agent_messages SET status=?, acknowledged_at=datetime('now'),"
                " error=? WHERE id=?",
                (status, error, int(message_id)))
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def find_by_correlation(correlation_id: str,
                          message_type: str = "") -> Optional[Dict[str, Any]]:
    """按 correlation_id 找消息 (query_sync 用它找 reply)。"""
    if not correlation_id:
        return None
    sql = ("SELECT id, from_agent, to_agent, canonical_id, message_type,"
           " correlation_id, payload_json, status, created_at"
           " FROM agent_messages WHERE correlation_id=?")
    params: list = [correlation_id]
    if message_type:
        sql += " AND message_type=?"
        params.append(message_type)
    sql += " ORDER BY id DESC LIMIT 1"
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
        except Exception:
            d["payload"] = {}
        return d
    except Exception:
        return None


def query_sync(*, from_agent: str,
                 to_agent: str,
                 payload: Dict[str, Any],
                 canonical_id: str = "",
                 timeout_sec: float = 30.0,
                 poll_interval: float = 1.0) -> Optional[Dict[str, Any]]:
    """发送 query 并阻塞等待 reply (基于 correlation_id 轮询)。

    对方 agent 必须在 timeout 内 send_message(MSG_REPLY, correlation_id=...)。

    Returns:
        reply 的 payload dict; 超时/无响应返回 None
    """
    cid = send_message(from_agent=from_agent, to_agent=to_agent,
                        message_type=MSG_QUERY, canonical_id=canonical_id,
                        payload=payload)
    if not cid:
        return None
    deadline = time.monotonic() + float(timeout_sec)
    while time.monotonic() < deadline:
        reply = find_by_correlation(cid, message_type=MSG_REPLY)
        if reply:
            return reply.get("payload") or {}
        time.sleep(max(0.1, float(poll_interval)))
    logger.info("[mesh] query_sync 超时 correlation=%s to=%s", cid, to_agent)
    return None


def reply_to(query_message: Dict[str, Any],
              from_agent: str,
              payload: Dict[str, Any]) -> str:
    """便捷: 根据收到的 query 消息构造 reply, 自动复用 correlation_id 和 canonical_id。

    query_message 是 poll_messages 返回的 dict。
    """
    return send_message(
        from_agent=from_agent,
        to_agent=query_message.get("from_agent") or "",
        message_type=MSG_REPLY,
        canonical_id=query_message.get("canonical_id") or "",
        payload=payload,
        correlation_id=query_message.get("correlation_id") or "",
    )


def cleanup_old_messages(older_than_days: int = 30) -> int:
    """删除 acknowledged/failed 的旧消息 (归档/节省空间)。pending/delivered 不动。"""
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=older_than_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM agent_messages WHERE status IN (?, ?) AND created_at < ?",
                (STATUS_ACKNOWLEDGED, STATUS_FAILED, cutoff))
            return cur.rowcount or 0
    except Exception as e:
        logger.debug("[mesh] cleanup 失败: %s", e)
        return 0
