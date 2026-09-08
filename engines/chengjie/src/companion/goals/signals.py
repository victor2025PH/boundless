"""目标结算/规划的「信号采集」——惰性读现有事实源，绝不新建状态。

设计：**settle-on-read（读时结算）**。不订阅事件总线、不加轮询任务——目标被读到
（右栏卡打开 / 注入 / 主动桥）那一刻，从既有单一事实源现取信号：

- intimacy / funnel_stage：``companion_context`` 进程级 provider（contacts 子系统）
- entitlement：``resolve_entitlement(chat_key)``（monetization；contact_key == 端用户 id）
- 最近入站时刻：inbox store（``protocol_bridge.get_inbox_store`` 进程级 getter）

任何一路缺失 → 对应字段维持「未知」哨兵值，消费方（ledger/planner）按保守行为
降级；本模块**绝不抛**。纯函数消费方只吃 ``GoalSignals``，可零 IO 单测。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("src.companion.goals.signals")


@dataclass
class GoalSignals:
    """结算/规划输入的统一信号快照（哨兵：intimacy=-1 未知；ts=0 未知）。"""

    now: float = 0.0
    intimacy: float = -1.0
    funnel_stage: str = ""
    entitlement: Optional[Dict[str, Any]] = None
    last_ts: float = 0.0            # 会话最后一条消息（任意方向）
    last_inbound_ts: float = 0.0    # 对方最后开口时刻
    negative_emotion: bool = False
    emotion_intensity: float = -1.0
    extras: Dict[str, Any] = field(default_factory=dict)


def entitlement_unlocked(ent: Optional[Dict[str, Any]], item_id: str) -> bool:
    """端用户是否已解锁某付费项（unlocked/grants 任一命中）。形状对齐
    ``companion_context.resolve_entitlement`` 的 ``{tier, grants, unlocked}``。"""
    if not isinstance(ent, dict) or not item_id:
        return False
    item = str(item_id)
    for key in ("unlocked", "grants"):
        val = ent.get(key)
        try:
            if isinstance(val, dict) and item in val:
                return True
            if isinstance(val, (list, tuple, set)) and item in val:
                return True
        except Exception:
            continue
    return False


def entitlement_tier(ent: Optional[Dict[str, Any]]) -> str:
    if not isinstance(ent, dict):
        return ""
    return str(ent.get("tier") or "").strip().lower()


def _last_inbound_ts(inbox_store: Any, conversation_id: str) -> float:
    """对方最后开口时刻：扫最近消息找 direction=='in' 的最大 ts。best-effort。"""
    if inbox_store is None or not conversation_id:
        return 0.0
    try:
        msgs = inbox_store.list_recent_messages(conversation_id, limit=30) or []
    except Exception:
        return 0.0
    best = 0.0
    for m in msgs:
        try:
            if str(m.get("direction") or "") != "in":
                continue
            ts = float(m.get("ts") or 0)
            if ts > best:
                best = ts
        except Exception:
            continue
    return best


def inbound_count_since(inbox_store: Any, conversation_id: str,
                        since_ts: float) -> int:
    """``since_ts`` 之后对方开口的条数（O-3 B 客户活跃判据：30min ≥5 条）。best-effort。"""
    if inbox_store is None or not conversation_id:
        return 0
    try:
        msgs = inbox_store.list_recent_messages(conversation_id, limit=40) or []
    except Exception:
        return 0
    n = 0
    for m in msgs:
        try:
            if str(m.get("direction") or "") != "in":
                continue
            if float(m.get("ts") or 0) >= float(since_ts):
                n += 1
        except Exception:
            continue
    return n


def collect_signals(
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    conversation_id: str = "",
    inbox_store: Any = None,
    negative_emotion: bool = False,
    emotion_intensity: float = -1.0,
    now: Optional[float] = None,
) -> GoalSignals:
    """从现有事实源惰性采集信号快照。任何一路失败按未知降级，绝不抛。

    ``inbox_store`` 缺省时经 ``protocol_bridge.get_inbox_store()`` 进程级 getter
    兜底取（未注册 → None → 入站时刻未知）。
    """
    n = float(now if now is not None else time.time())
    sig = GoalSignals(now=n, negative_emotion=bool(negative_emotion),
                      emotion_intensity=float(emotion_intensity))
    try:
        from src.utils.companion_context import (
            resolve_entitlement,
            resolve_funnel_stage,
            resolve_intimacy_score,
        )
        if chat_key:
            v = resolve_intimacy_score(
                account_id or "default", chat_key, channel=platform or "telegram")
            if v is not None:
                sig.intimacy = float(v)
            st = resolve_funnel_stage(
                account_id or "default", chat_key, channel=platform or "telegram")
            if st:
                sig.funnel_stage = str(st).strip().lower()
            ent = resolve_entitlement(str(chat_key))
            if isinstance(ent, dict):
                sig.entitlement = ent
    except Exception:
        logger.debug("collect_signals: relationship providers failed", exc_info=True)

    store = inbox_store
    if store is None:
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            store = get_inbox_store()
        except Exception:
            store = None
    if store is not None and conversation_id:
        try:
            conv = store.get_conversation(conversation_id) or {}
            sig.last_ts = float(conv.get("last_ts") or 0)
        except Exception:
            pass
        sig.last_inbound_ts = _last_inbound_ts(store, conversation_id)
        # O-3 B：近 30 分钟入站条数（planner 客户活跃自适应）
        sig.extras["inbound_30m"] = inbound_count_since(
            store, conversation_id, n - 1800.0)
    return sig


__all__ = [
    "GoalSignals",
    "collect_signals",
    "entitlement_tier",
    "entitlement_unlocked",
    "inbound_count_since",
]
