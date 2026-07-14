# -*- coding: utf-8 -*-
"""Lead Mesh — 跨平台/跨 Agent 的 Lead Dossier + 通信协议 (2026-04-23 Phase 5)。

子模块:
  * canonical   — CanonicalResolver, 硬匹配 + 高置信度软匹配自动合并
  * journey     — JourneyStore, append-only 事件流
  * dossier     — LeadDossier 聚合视图 (read path)
  * lock_manager — 软锁 + TTL 过期 (防多 agent 并发)
  * handoff     — 引流交接状态机
  * agent_mesh  — Agent 间消息总线 (SQLite + HTTP 双通道)
  * webhook_dispatcher — 外部 webhook 推送 + 重试 + 死信

入口建议 (调用方):
    from src.host.lead_mesh import (
        resolve_identity, append_journey, get_dossier,
        acquire_lock, create_handoff, dispatch_agent_message
    )
"""
from __future__ import annotations

# 顶层便捷 API; 全部从子模块 re-export
from .canonical import (  # noqa
    resolve_identity,
    auto_merge_candidates,
    update_canonical_metadata,
    remove_canonical_tags,
    revive_referral,
    list_l2_verified_leads,
    count_l2_verified_leads,
)
from .journey import append_journey, get_journey, count_actions  # noqa
from .dossier import get_dossier, search_leads  # noqa
from .lock_manager import acquire_lock, release_lock, is_locked  # noqa
from .handoff import (  # noqa
    create_handoff, get_handoff, list_handoffs,
    acknowledge_handoff, complete_handoff, reject_handoff,
    expire_pending_handoffs,
    check_duplicate_handoff, check_peer_cooldown_handoff,
)
from .agent_mesh import (  # noqa
    send_message, poll_messages, mark_delivered, mark_acknowledged,
    query_sync,
)
from .webhook_dispatcher import (  # noqa
    enqueue_webhook, flush_pending_webhooks,
)
from .receivers import (  # noqa
    load_receivers, get_receiver, list_receivers,
    pick_receiver, receiver_load, all_loads as list_receiver_loads,
    count_today_handoffs, upsert_receiver, delete_receiver,
)
from .blocklist import (  # noqa
    add_to_blocklist, remove_from_blocklist, is_blocklisted,
    get_blocklist_entry, list_blocklist, count_blocklist,
)

__all__ = [
    "resolve_identity", "auto_merge_candidates",
    "append_journey", "get_journey", "count_actions",
    "get_dossier", "search_leads",
    "acquire_lock", "release_lock", "is_locked",
    "create_handoff", "get_handoff", "list_handoffs",
    "acknowledge_handoff", "complete_handoff", "reject_handoff",
    "expire_pending_handoffs",
    "send_message", "poll_messages", "mark_delivered", "mark_acknowledged",
    "query_sync",
    "enqueue_webhook", "flush_pending_webhooks",
]
