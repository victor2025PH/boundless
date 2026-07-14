# -*- coding: utf-8 -*-
"""Handoff — 引流交接状态机 (Phase 5)。

状态机::

    pending ──ack──► acknowledged ──complete──► completed
       │                │
       │                └──reject──► rejected
       │
       └─────expire───► expired (72h 未 ack 自动过期)

所有状态转移都会:
  1. 更新 lead_handoffs.state + state_updated_at
  2. append lead_journey 相应事件
  3. enqueue webhook (dispatch 异步发)

脱敏: conversation_snapshot 入库前调 _sanitize() 替换手机/邮箱/ID。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_COMPLETED = "completed"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"
STATE_DUPLICATE_BLOCKED = "duplicate_blocked"

ACTIVE_STATES = (STATE_PENDING, STATE_ACKNOWLEDGED)
TERMINAL_STATES = (STATE_COMPLETED, STATE_REJECTED, STATE_EXPIRED,
                   STATE_DUPLICATE_BLOCKED)

DEFAULT_EXPIRE_HOURS = 72


# ─── 脱敏 ────────────────────────────────────────────────────────────
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{7,}\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LINE_ID_RE = re.compile(r"@[A-Za-z][\w.\-]{2,19}")


def _sanitize(text: str) -> str:
    """对话原文脱敏, 避免交接单二次泄露。"""
    if not text:
        return ""
    s = text
    s = _EMAIL_RE.sub("[EMAIL]", s)
    s = _PHONE_RE.sub("[PHONE]", s)
    # LINE @id 保留类型标签, 不留原值
    s = _LINE_ID_RE.sub("[LINE_ID]", s)
    return s


def _sanitize_snapshot(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in turns or []:
        t2 = dict(t)
        if "text" in t2:
            t2["text"] = _sanitize(str(t2["text"]))
        if "message_text" in t2:
            t2["message_text"] = _sanitize(str(t2["message_text"]))
        out.append(t2)
    return out


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── CRUD ────────────────────────────────────────────────────────────

def create_handoff(*, canonical_id: str,
                    source_agent: str,
                    channel: str,
                    source_device: str = "",
                    target_agent: str = "",
                    receiver_account_key: str = "",
                    persona_key: str = "",
                    conversation_snapshot: Optional[List[Dict[str, Any]]] = None,
                    snippet_sent: str = "",
                    enqueue_webhook: bool = True,
                    auto_pick_receiver: bool = True,
                    honor_peer_cooldown: bool = False,
                    peer_cooldown_days: int = 30) -> str:
    """创建交接单, 返回 handoff_id (UUID)。

    自动:
      * snapshot 脱敏
      * **receiver_account_key 空且 auto_pick_receiver=True**: 调
        ``receivers.pick_receiver(channel, persona_key)`` 自动路由到空闲的
        接收方(含 backup 链路); 全部 at_cap 时 receiver_account_key 仍为空,
        运营可在 Dashboard 手动指派
      * append lead_journey: handoff_created (带 receiver 信息)
      * 如果 enqueue_webhook=True, 写 webhook_dispatches 待发

    **不做**:
      * 不做去重检查 (那是调用方的 gate 职责, 防止 race)
      * 不同步发 webhook (异步由 dispatcher 取走)

    Args:
        receiver_account_key: 显式指定接收方; 留空则走自动路由
        persona_key: 配合 auto_pick 过滤 receiver.persona_filter
        auto_pick_receiver: 关闭则不自动路由(仅测试场景用)
        honor_peer_cooldown: True=若 peer 最近 peer_cooldown_days 有任何活跃
            handoff(不管什么 channel) 就拒绝创建, 返回空串。用于 B 端换渠道
            引流时防骚扰。**默认 False** 保持向后兼容, 调用方应在引流决策
            层显式启用。
        peer_cooldown_days: 冷却窗口, 默认 30 天
    """
    if not canonical_id or not source_agent or not channel:
        raise ValueError("canonical_id / source_agent / channel 必填")

    # 跨渠道冷却检查 (默认关闭, 需调用方显式启用)
    if honor_peer_cooldown:
        existing = check_peer_cooldown_handoff(
            canonical_id, cooldown_days=peer_cooldown_days,
            honor_rejected=True)
        if existing:
            logger.info(
                "[handoff] peer_cooldown 阻塞: canonical=%s 已有 %s 渠道 %s "
                "handoff (state=%s, created=%s), 跳过 %s",
                canonical_id[:12], existing.get("channel"),
                existing.get("handoff_id", "")[:12], existing.get("state"),
                existing.get("created_at"), channel)
            # journey 记一笔 blocked 事件
            try:
                from .journey import append_journey
                append_journey(canonical_id, actor=source_agent,
                               action="handoff_blocked",
                               actor_device=source_device,
                               platform=channel,
                               data={"reason": "peer_cooldown",
                                     "existing_handoff_id": existing.get("handoff_id"),
                                     "existing_channel": existing.get("channel"),
                                     "existing_state": existing.get("state")})
            except Exception:
                pass
            return ""

    # 自动 pick receiver (如果 caller 没指定)
    auto_picked = False
    if not receiver_account_key and auto_pick_receiver:
        try:
            from .receivers import pick_receiver
            picked = pick_receiver(channel, persona_key=persona_key or None)
            if picked and picked.get("key"):
                receiver_account_key = picked["key"]
                auto_picked = True
                logger.info("[handoff] 自动路由 channel=%s persona=%s → %s "
                            "(current=%d/%d)",
                            channel, persona_key or "-",
                            receiver_account_key,
                            picked.get("current", 0), picked.get("cap", 0))
            else:
                logger.warning("[handoff] 自动路由失败 channel=%s persona=%s: "
                                "无可用 receiver (全部 at_cap 或未配置)",
                                channel, persona_key or "-")
        except Exception as e:
            logger.debug("[handoff] pick_receiver 异常(继续): %s", e)

    handoff_id = str(uuid.uuid4())
    snap = _sanitize_snapshot(conversation_snapshot or [])
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_handoffs"
                " (handoff_id, canonical_id, source_agent, source_device,"
                "  target_agent, channel, receiver_account_key,"
                "  conversation_snapshot_json, snippet_sent, state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (handoff_id, canonical_id, source_agent, source_device,
                 target_agent, channel, receiver_account_key,
                 json.dumps(snap, ensure_ascii=False, default=str),
                 snippet_sent, STATE_PENDING),
            )
    except Exception as e:
        logger.warning("[handoff] create 失败: %s", e)
        return ""

    # Journey
    try:
        from .journey import append_journey
        append_journey(canonical_id, actor=source_agent, action="handoff_created",
                       actor_device=source_device, platform=channel,
                       data={"handoff_id": handoff_id,
                             "receiver": receiver_account_key,
                             "auto_picked": auto_picked,
                             "snapshot_turns": len(snap)})
    except Exception:
        pass

    # Webhook
    if enqueue_webhook:
        try:
            from .webhook_dispatcher import enqueue_webhook as eq
            eq(event_type="handoff.created",
               payload={
                   "handoff_id": handoff_id,
                   "canonical_id": canonical_id,
                   "source_agent": source_agent,
                   "channel": channel,
                   "receiver_account_key": receiver_account_key,
                   "created_at": _now_iso(),
                   "snapshot_turns": len(snap),
                   "snippet_sent": snippet_sent,
               },
               related_canonical_id=canonical_id,
               related_handoff_id=handoff_id)
        except Exception as e:
            logger.debug("[handoff] enqueue_webhook 失败(不阻塞创建): %s", e)
    return handoff_id


def get_handoff(handoff_id: str) -> Optional[Dict[str, Any]]:
    if not handoff_id:
        return None
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM lead_handoffs WHERE handoff_id=?",
                (handoff_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["conversation_snapshot"] = json.loads(
                d.pop("conversation_snapshot_json") or "[]")
        except Exception:
            d["conversation_snapshot"] = []
        return d
    except Exception:
        return None


def list_handoffs(*,
                    state: str = "",
                    receiver_account_key: str = "",
                    canonical_id: str = "",
                    channel: str = "",
                    limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM lead_handoffs WHERE 1=1"
    params: list = []
    if state:
        sql += " AND state=?"
        params.append(state)
    if receiver_account_key:
        sql += " AND receiver_account_key=?"
        params.append(receiver_account_key)
    if canonical_id:
        sql += " AND canonical_id=?"
        params.append(canonical_id)
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["conversation_snapshot"] = json.loads(
                    d.pop("conversation_snapshot_json") or "[]")
            except Exception:
                d["conversation_snapshot"] = []
            out.append(d)
        return out
    except Exception as e:
        logger.debug("[handoff] list 失败: %s", e)
        return []


# ─── 状态转移 ────────────────────────────────────────────────────────

def _transition(handoff_id: str, from_states: tuple,
                 to_state: str, *,
                 by: str,
                 notes: str = "",
                 journey_action: str = "",
                 webhook_event: str = "") -> bool:
    """通用状态转移。from_states 定义允许的起始态。"""
    if not handoff_id or not to_state or not by:
        return False
    # 先取当前状态确认
    row = get_handoff(handoff_id)
    if not row:
        return False
    if row["state"] not in from_states:
        logger.info("[handoff] 拒绝转移 %s (当前=%s, 不在 %s)",
                    handoff_id[:12], row["state"], from_states)
        return False
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE lead_handoffs SET state=?, state_updated_at=datetime('now'),"
                " state_notes=? WHERE handoff_id=? AND state IN ({})".format(
                    ",".join(["?"] * len(from_states))),
                (to_state, notes or "", handoff_id, *from_states),
            )
            if (cur.rowcount or 0) == 0:
                return False  # 并发抢占
    except Exception as e:
        logger.warning("[handoff] transition 失败: %s", e)
        return False
    # journey
    try:
        from .journey import append_journey
        append_journey(row["canonical_id"], actor=by,
                       action=journey_action or f"handoff_{to_state}",
                       platform=row.get("channel") or "",
                       data={"handoff_id": handoff_id,
                             "from_state": row["state"],
                             "notes": notes})
    except Exception:
        pass
    # webhook
    if webhook_event:
        try:
            from .webhook_dispatcher import enqueue_webhook as eq
            eq(event_type=webhook_event,
               payload={
                   "handoff_id": handoff_id,
                   "canonical_id": row["canonical_id"],
                   "new_state": to_state,
                   "transitioned_by": by,
                   "notes": notes,
                   "at": _now_iso(),
               },
               related_canonical_id=row["canonical_id"],
               related_handoff_id=handoff_id)
        except Exception:
            pass
    return True


def acknowledge_handoff(handoff_id: str, *, by: str, notes: str = "") -> bool:
    """接收方标记"已看到"。pending → acknowledged。"""
    return _transition(handoff_id, (STATE_PENDING,), STATE_ACKNOWLEDGED,
                        by=by, notes=notes,
                        journey_action="handoff_acknowledged",
                        webhook_event="handoff.acknowledged")


def complete_handoff(handoff_id: str, *, by: str, notes: str = "") -> bool:
    """接收方标记"已接上对话"。pending|ack → completed。"""
    ok = _transition(handoff_id, (STATE_PENDING, STATE_ACKNOWLEDGED),
                      STATE_COMPLETED, by=by, notes=notes,
                      journey_action="handoff_completed",
                      webhook_event="handoff.completed")
    # K2: 推进生命周期 → converted
    if ok:
        try:
            h = get_handoff(handoff_id)
            if h and h.get("canonical_id"):
                from .canonical import advance_lifecycle
                advance_lifecycle(h["canonical_id"], "converted")
        except Exception:
            pass
    return ok


def reject_handoff(handoff_id: str, *, by: str, notes: str = "") -> bool:
    """接收方标记"对方拒接/放弃"。pending|ack → rejected。"""
    return _transition(handoff_id, (STATE_PENDING, STATE_ACKNOWLEDGED),
                        STATE_REJECTED, by=by, notes=notes,
                        journey_action="handoff_rejected",
                        webhook_event="handoff.rejected")


def expire_pending_handoffs(expire_hours: int = DEFAULT_EXPIRE_HOURS) -> int:
    """定时任务调用: 批量过期超过 N 小时未 ack 的 pending。

    返回: 被过期的条数。
    """
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=expire_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT handoff_id, canonical_id, channel FROM lead_handoffs"
                " WHERE state=? AND created_at < ?",
                (STATE_PENDING, cutoff)).fetchall()
        count = 0
        for r in rows:
            hid = r["handoff_id"]
            if _transition(hid, (STATE_PENDING,), STATE_EXPIRED,
                            by="system:expire_job",
                            notes=f"自动过期 (pending>{expire_hours}h)",
                            journey_action="handoff_expired",
                            webhook_event="handoff.expired"):
                count += 1
        return count
    except Exception as e:
        logger.warning("[handoff] expire_pending 失败: %s", e)
        return 0


# ─── 去重检查 (供 B 发引流前调用) ───────────────────────────────────

def check_duplicate_handoff(canonical_id: str,
                             channel: str,
                             since_days: int = 30) -> Optional[Dict[str, Any]]:
    """查询指定 lead 在指定渠道最近 N 天内是否已有活跃/已完成的 handoff。

    有 → 返回那条 handoff 的关键字段, 调用方应跳过再次引流。
    无 → None。

    **仅同渠道**: 比 check_peer_cooldown_handoff 宽松 - 允许跨渠道重试。
    """
    if not canonical_id or not channel:
        return None
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=since_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT handoff_id, state, created_at, source_agent FROM lead_handoffs"
                " WHERE canonical_id=? AND channel=? AND created_at >= ?"
                " AND state NOT IN (?, ?)"
                " ORDER BY created_at DESC LIMIT 1",
                (canonical_id, channel, cutoff, STATE_REJECTED, STATE_EXPIRED)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("[handoff] check_duplicate 失败: %s", e)
        return None


def check_peer_cooldown_handoff(canonical_id: str,
                                 cooldown_days: int = 30,
                                 honor_rejected: bool = True
                                 ) -> Optional[Dict[str, Any]]:
    """**跨渠道** 冷却检查 - 防止同一 peer 在短时间被多渠道骚扰。

    业务场景:
      * LINE 引流刚发出 → 对方未回 → B 机想改 WhatsApp 再试?
        → 此函数返回非空阻止 (pending 视为仍在等结果)
      * LINE 失败被 reject → 换 WhatsApp 是合理行为?
        → honor_rejected=True (默认) 时允许换渠道, 返回 None

    与 check_duplicate_handoff 的区别:
      * duplicate: 同 channel 内去重 (宽松)
      * peer_cooldown: 跨 channel 去重 (严格, 用于骚扰保护)

    Args:
        canonical_id: lead canonical_id
        cooldown_days: 冷却窗口天数, 默认 30
        honor_rejected: True=rejected/expired 状态不算阻塞(允许换渠道重试);
                        False=只要有过 handoff 记录都阻塞 (最严格)

    Returns:
        存在冷却期内的活跃/完成 handoff → 该行 dict;
        无 / 仅有 rejected 且 honor_rejected=True → None
    """
    if not canonical_id:
        return None
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(cooldown_days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    # 构造排除状态: honor_rejected 时 rejected/expired 不算; 否则都算
    if honor_rejected:
        exclude_states = (STATE_REJECTED, STATE_EXPIRED)
    else:
        exclude_states = ()
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            sql = ("SELECT handoff_id, channel, state, created_at, source_agent,"
                   " receiver_account_key FROM lead_handoffs"
                   " WHERE canonical_id=? AND created_at >= ?")
            params: list = [canonical_id, cutoff]
            if exclude_states:
                sql += " AND state NOT IN ({})".format(
                    ",".join(["?"] * len(exclude_states)))
                params.extend(exclude_states)
            sql += " ORDER BY created_at DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("[handoff] check_peer_cooldown 失败: %s", e)
        return None
