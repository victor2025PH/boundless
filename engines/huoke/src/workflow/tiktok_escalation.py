# -*- coding: utf-8 -*-
"""
TikTok Cross-Platform Escalation — bridges TikTok events to the acquisition pipeline.

TikTok automation emits events with `username` (not `lead_id`). This module:
1. Subscribes to tiktok.* events
2. Resolves username → lead_id via LeadsStore
3. Re-emits enriched events with lead_id for the acquisition pipeline
4. Handles direct cross-platform escalation (TikTok → Telegram/WhatsApp)

Auto-registers on import if EventBus is available.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional

from src.openclaw_env import local_api_base

log = logging.getLogger(__name__)

_registered = False
_lock = threading.Lock()


def _event_auto_tasks_allowed() -> bool:
    """task_execution_policy.disable_event_driven_auto_tasks 为 true 时不自动建任务。"""
    try:
        from src.host.task_policy import policy_blocks_event_driven_auto_tasks
        return not policy_blocks_event_driven_auto_tasks()
    except Exception:
        return True


def register_tiktok_escalation():
    """Set up TikTok event handlers on the global EventBus. Idempotent."""
    global _registered
    if _registered:
        return
    with _lock:
        if _registered:
            return
        try:
            from .event_bus import get_event_bus
            bus = get_event_bus()

            bus.on("tiktok.follow_received", _on_follow_received)
            bus.on("tiktok.user_followed", _on_user_followed)
            bus.on("tiktok.dm_sent", _on_dm_sent)
            bus.on("tiktok.lead_discovered", _on_lead_discovered)
            bus.on("tiktok.message_classified", _on_message_classified)
            bus.on("tiktok.escalate_to_human", _on_escalate_to_human)

            _registered = True
            log.info("[TikTok Escalation] Registered 6 event handlers")
        except Exception as e:
            log.warning("[TikTok Escalation] Registration failed: %s", e)


def _resolve_lead_id(username: str) -> Optional[int]:
    """Resolve a TikTok username to a lead_id."""
    try:
        from ..leads.store import get_leads_store
        return get_leads_store().find_by_platform_username("tiktok", username)
    except Exception:
        return None


def _on_follow_received(event):
    """Handle follow-back: enrich with lead_id, update score, check for cross-platform escalation."""
    username = event.data.get("username", "")
    if not username:
        return

    lead_id = _resolve_lead_id(username)
    if not lead_id:
        return

    try:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        store.update_score(lead_id)

        lead = store.get_lead(lead_id)
        if not lead:
            return

        from .event_bus import get_event_bus
        bus = get_event_bus()
        bus.emit_simple("tiktok.follow_received.enriched",
                        source="tiktok_escalation",
                        lead_id=lead_id, username=username,
                        score=lead.get("score", 0),
                        status=lead.get("status", ""))

        score = lead.get("score", 0)
        if score >= 25.0 and _event_auto_tasks_allowed():
            _schedule_cross_platform_outreach(lead_id, username, lead)

        # ★ P3-1: 回关后快速响应 — 趁热打铁，1.5小时内发第一条DM
        # 条件: 有回关 + 评分 >= 10 + 近48h内未发过DM + 有执行设备
        device_id = event.data.get("device_id", "")
        if device_id and score >= 10.0 and _event_auto_tasks_allowed():
            try:
                outbound_dms = store.interaction_count(
                    lead_id, direction="outbound", days=2
                )
                if outbound_dms == 0:
                    _schedule_followback_rapid_dm(lead_id, username, device_id)
            except Exception as _rdm_err:
                log.debug("[TikTok Escalation] 快速响应DM检查失败: %s", _rdm_err)

    except Exception as e:
        log.debug("[TikTok Escalation] follow_received handler error: %s", e)


def _on_user_followed(event):
    """After following a user, ensure they exist in CRM and emit enriched event."""
    username = event.data.get("username", "")
    lead_id = _resolve_lead_id(username)
    if lead_id:
        try:
            from .event_bus import get_event_bus
            get_event_bus().emit_simple("tiktok.user_followed.enriched",
                                       source="tiktok_escalation",
                                       lead_id=lead_id, username=username)
        except Exception:
            pass


def _on_dm_sent(event):
    """After DM sent, update lead status and check if ready for conversion."""
    username = event.data.get("username", "")
    device_id = event.data.get("device_id", "")
    lead_id = _resolve_lead_id(username)
    if not lead_id:
        return

    try:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        store.update_score(lead_id)

        lead = store.get_lead(lead_id)
        if not lead:
            return

        interactions = store.get_interactions(lead_id, platform="tiktok", limit=20)
        inbound_count = sum(1 for ix in interactions if ix["direction"] == "inbound")
        outbound_dms = [ix for ix in interactions
                        if ix.get("action") == "send_dm" and ix.get("direction") == "outbound"]

        if inbound_count >= 2 and lead.get("score", 0) >= 20.0:
            store.update_lead(lead_id, status="qualified")
            from .event_bus import get_event_bus
            get_event_bus().emit_simple("lead.qualified",
                                       source="tiktok_escalation",
                                       lead_id=lead_id, platform="tiktok",
                                       username=username)
            log.info("[TikTok Escalation] Lead #%d qualified for conversion: %s",
                     lead_id, username)
            # 记录 A/B 转化事件（让 template_optimizer 知道哪个变体带来了转化）
            try:
                for ix in outbound_dms:
                    _meta = ix.get("metadata") or {}
                    _vid = _meta.get("ab_variant", "")
                    if _vid:
                        from src.host.ab_testing import get_ab_store
                        get_ab_store().record("dm_template_style", _vid,
                                              "converted",
                                              metadata={"lead_id": lead_id})
                        break
            except Exception:
                pass

        # ★ P1 Drip Campaign: 首次发 DM 后，调度后续跟进序列
        if len(outbound_dms) == 1 and device_id and _event_auto_tasks_allowed():
            _schedule_drip_followups(lead_id, username, device_id)

    except Exception as e:
        log.debug("[TikTok Escalation] dm_sent handler error: %s", e)


def _on_lead_discovered(event):
    """When a lead is discovered via search, emit enriched event."""
    lead_id = event.data.get("lead_id")
    if lead_id:
        try:
            from .event_bus import get_event_bus
            get_event_bus().emit_simple("tiktok.lead_discovered.enriched",
                                       source="tiktok_escalation",
                                       lead_id=lead_id,
                                       query=event.data.get("query", ""))
        except Exception:
            pass


def _on_message_classified(event):
    """Track intent classification results in CRM, and trigger urgent inbox check for high-intent users."""
    username = event.data.get("username", "")
    intent = event.data.get("intent", "")
    confidence = event.data.get("confidence", 0)
    device_id = event.data.get("device_id", "")
    lead_id = _resolve_lead_id(username)
    if not lead_id:
        return

    try:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        store.add_interaction(
            lead_id, "tiktok", "message_classified",
            direction="inbound",
            metadata={"intent": intent, "confidence": confidence,
                      "next_action": event.data.get("next_action", "")},
        )

        if intent in ("interested", "meeting"):
            store.update_score(lead_id, delta=5)
            store.update_lead(lead_id, status="responded")
            # ── 高意向：立即触发紧急回复任务 ──
            if device_id and _event_auto_tasks_allowed():
                _schedule_urgent_inbox_check(device_id, username, intent, confidence)
            # ★ Drip: 用户已回复，取消后续 drip 跟进任务（不再打扰）
            _cancel_drip_if_replied(lead_id, username)
            # ★ P2-3: 高意向用户 Telegram 告警（实时通知运营人员）
            try:
                from src.host.alert_notifier import get_alert_notifier
                get_alert_notifier().notify_event(
                    event_type="high_intent_reply",
                    title=f"高意向用户回复: @{username}",
                    body=(
                        f"用户 @{username} 表达了 <b>{intent}</b> 意向\n"
                        f"置信度: {confidence:.0%}\n"
                        f"设备: {device_id[:8] if device_id else 'N/A'}\n"
                        f"已自动触发优先处理"
                    ),
                    level="warning",
                )
            except Exception:
                pass
        elif intent == "negative":
            store.update_lead(lead_id, status="rejected")
            # ★ Drip: 用户明确拒绝，也取消 drip 任务
            _cancel_drip_if_replied(lead_id, username)

    except Exception as e:
        log.debug("[TikTok Escalation] message_classified handler error: %s", e)


def _schedule_urgent_inbox_check(device_id: str, username: str,
                                  intent: str, confidence: float):
    """
    为高意向用户触发优先级100的紧急私信检查任务。

    使用 HTTP API（本机 /tasks）提交，与 job_scheduler 保持一致，
    避免跨层直接导入 executor/worker_pool（保持 workflow 层与 host 层解耦）。
    priority=100 字段由 WorkerPool 识别，会取消同设备上正在运行的低优先级任务。
    """
    if not _event_auto_tasks_allowed():
        return
    import json as _json
    import urllib.request as _ur

    payload = _json.dumps({
        "type": "tiktok_check_inbox",
        "device_id": device_id,
        "params": {
            "auto_reply": True,
            "use_ai": True,
            "max_messages": 20,
            "urgent_user": username,
            "triggered_by": f"high_intent:{intent}:{confidence:.2f}",
        },
        "priority": 100,
    }).encode()

    try:
        req = _ur.Request(
            f"{local_api_base()}/tasks",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = _ur.urlopen(req, timeout=5)
        resp_data = _json.loads(resp.read().decode())
        task_id = resp_data.get("task_id", "?")
        log.info(
            "[TikTok Escalation] 高意向用户 %s (intent=%s, conf=%.2f) → "
            "紧急任务 %s 已提交到设备 %s",
            username, intent, confidence, task_id[:8], device_id[:8],
        )
    except Exception as e:
        log.warning("[TikTok Escalation] 紧急任务提交失败: %s", e)


def _on_escalate_to_human(event):
    """Mark leads requiring manual intervention."""
    username = event.data.get("username", "")
    intent = event.data.get("intent", "")
    lead_id = _resolve_lead_id(username)

    log.warning("[人工介入] TikTok 用户 %s 需人工处理 (intent=%s, msg=%s)",
                username, intent, event.data.get("message", "")[:100])

    if lead_id:
        try:
            from ..leads.store import get_leads_store
            store = get_leads_store()
            store.update_lead(lead_id, status="needs_manual",
                              tags=["human_escalation"])
            store.add_interaction(
                lead_id, "tiktok", "human_escalation",
                direction="inbound",
                content=event.data.get("message", "")[:500],
                metadata={"intent": intent},
            )
        except Exception as e:
            log.debug("[TikTok Escalation] escalate_to_human handler error: %s", e)


def _schedule_followback_rapid_dm(lead_id: int, username: str, device_id: str):
    """
    ★ P3-1: 回关快速响应 — 在对方回关后 1.5 小时内发送首条 DM。

    背景: 用户回关的黄金互动窗口约 2 小时，超过 6 小时回复率下降 70%+。
    当前调度系统最坏情况下需等待 24h，严重浪费暖流量。

    实现: 提交 priority=75 的 tiktok_send_dm 任务，delay_seconds=5400 (1.5h)。
    priority=75 高于普通调度(50)但低于紧急回复(100)，保证不抢占关键任务。
    """
    if not _event_auto_tasks_allowed():
        return
    import json as _json
    import urllib.request as _ur

    # 从 ab_winner.json 获取最优消息风格
    try:
        from src.host.device_registry import data_file

        _winner_path = data_file("ab_winner.json")
        _ab_style = None
        if _winner_path.exists():
            with open(_winner_path, encoding="utf-8") as _f:
                _ab_style = _json.load(_f).get("dm_template_style")
    except Exception:
        _ab_style = None

    # 简洁开场白（不引流，只建立连接）
    _templates = {
        "warm_greeting": "Hey! Thanks for following back 😊 Great to connect!",
        "question_opener": "Hey! Quick question — what kind of content do you enjoy most? 😊",
        "compliment_first": "Hey! Love your vibe, glad we're connected! 🙌",
        "direct_referral": "Hey! Great to connect — I share more content on Telegram too 👋",
    }
    message = _templates.get(_ab_style or "warm_greeting",
                             "Hey! Thanks for following back 😊 Great to connect!")

    try:
        payload = _json.dumps({
            "type": "tiktok_send_dm",
            "device_id": device_id,
            "params": {
                "username": username,
                "message": message,
                "lead_id": lead_id,
                "source": "followback_rapid",
                "ab_variant": _ab_style,
            },
            "priority": 75,
            "delay_seconds": 5400,  # 1.5 小时后发送（不能立即发，太突兀）
        }).encode()

        req = _ur.Request(
            f"{local_api_base()}/tasks",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = _ur.urlopen(req, timeout=5)
        task_id = _json.loads(resp.read().decode()).get("task_id", "?")
        log.info("[TikTok Escalation] 回关快速响应DM已调度: @%s lead#%d → task %s (1.5h后)",
                 username, lead_id, task_id[:8])
    except Exception as e:
        log.debug("[TikTok Escalation] 快速响应DM提交失败: %s", e)


def _schedule_cross_platform_outreach(lead_id: int, username: str, lead: dict):
    """Schedule outreach on Telegram/WhatsApp for a high-value TikTok lead."""
    if not _event_auto_tasks_allowed():
        return
    try:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        profiles = store.get_platform_profiles(lead_id)

        platforms_available = {p["platform"] for p in profiles}

        if "telegram" in platforms_available:
            _create_conversion_task(lead_id, "telegram", lead)
        elif "whatsapp" in platforms_available:
            _create_conversion_task(lead_id, "whatsapp", lead)
        else:
            log.info("[TikTok Escalation] Lead #%d (%s) has no cross-platform profiles yet",
                     lead_id, username)

    except Exception as e:
        log.debug("[TikTok Escalation] cross-platform outreach error: %s", e)


def _create_conversion_task(lead_id: int, platform: str, lead: dict):
    """Create a task for cross-platform outreach."""
    try:
        from ..host.task_store import create_task
        from ..host.task_origin import with_origin

        name = lead.get("name", "").split()[0] if lead.get("name") else "there"

        task_type = f"{platform}_send_message"
        params = with_origin(
            {
                "lead_id": lead_id,
                "username": lead.get("name", ""),
                "message": f"Hi {name}! We connected on TikTok — great to reach you here too!",
                "source": "tiktok_escalation",
            },
            "tiktok_escalation",
        )

        task_id = create_task(task_type=task_type, params=params)
        log.info("[TikTok Escalation] Created %s task %s for lead #%d",
                 platform, task_id[:8], lead_id)

        from .event_bus import get_event_bus
        get_event_bus().emit_simple("lead.cross_platform_scheduled",
                                   source="tiktok_escalation",
                                   lead_id=lead_id, platform=platform,
                                   task_id=task_id)

    except Exception as e:
        log.warning("[TikTok Escalation] Failed to create %s task: %s", platform, e)


# ─────────────────────────────────────────────────────────────
# ★ P1 Drip Campaign — 多阶段触达序列
# ─────────────────────────────────────────────────────────────

# 触达序列定义
_DRIP_SEQUENCE = [
    # (延迟小时数, 上下文类型, 描述)
    (24,  "followup_day1",  "Day1 跟进：提供价值内容/互动问题（不引流）"),
    (72,  "referral",       "Day3 引流：发送 Telegram/WhatsApp 链接"),
    (168, "last_chance",    "Day7 最终：温和最后一次机会，否则归档"),
]


def _infer_best_send_offset(lead_id: int, base_delay_hours: float) -> float:
    """
    ★ P2-2: 根据用户历史活跃时间推算最优发送时刻，调整延迟偏移量（秒）。

    策略:
    - 从 interactions 表取该 lead 的近7天入站消息时间
    - 统计最常出现的UTC小时
    - 计算在 base_delay 附近，距最优小时最近的整点偏移
    - 偏移范围限制在 ±90 分钟内，避免偏差过大
    - 不足3个样本时返回0（数据不足，使用默认延迟）
    """
    try:
        from src.leads.store import get_leads_store
        from datetime import datetime, timezone, timedelta
        store = get_leads_store()

        interactions = store.get_interactions(lead_id, limit=20)
        inbound = [
            i for i in interactions
            if i.get("direction") == "inbound"
        ]
        if len(inbound) < 3:
            return 0.0  # 样本不足

        # 统计各小时出现次数
        hour_counts: dict = {}
        for ix in inbound:
            ts_str = ix.get("created_at", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                h = ts.hour
                hour_counts[h] = hour_counts.get(h, 0) + 1
            except Exception:
                continue

        if not hour_counts:
            return 0.0

        # 最常活跃的小时
        best_hour = max(hour_counts, key=hour_counts.get)

        # 计算 base_delay 对应的目标时刻 → 找最近一个 best_hour 的整点
        now = datetime.now(timezone.utc)
        target_base = now + timedelta(hours=base_delay_hours)
        # 往 best_hour 对齐：最近的一次
        candidate = target_base.replace(minute=30, second=0, microsecond=0)
        # 找最接近的 best_hour（当天/次日）
        for delta_days in range(3):
            d = target_base.replace(hour=best_hour, minute=30,
                                    second=0, microsecond=0)
            d += timedelta(days=delta_days)
            diff = (d - target_base).total_seconds()
            if -5400 <= diff <= 5400:  # 在 ±90 分钟内
                candidate = d
                break

        offset_sec = (candidate - target_base).total_seconds()
        # 限制在 ±90 分钟
        offset_sec = max(-5400, min(5400, offset_sec))
        return offset_sec

    except Exception:
        return 0.0


def _schedule_drip_followups(lead_id: int, username: str, device_id: str):
    """
    ★ Drip Campaign: 首次 DM 发出后，在 Day1/Day3/Day7 调度后续跟进任务。

    设计原则:
    - Day1: 不引流，只互动（问问题/分享内容），提高开口概率
    - Day3: 发引流消息（已建立初步信任）
    - Day7: 最终机会，温和告别（不激进）
    - ★ P2-2: 延迟自动对齐用户历史活跃时段（±90分钟）
    - 如果用户在任何阶段回复，后续任务自动取消（_cancel_drip_on_reply 处理）
    """
    if not _event_auto_tasks_allowed():
        return
    import json as _json
    import urllib.request as _ur

    for delay_hours, context, desc in _DRIP_SEQUENCE:
        try:
            payload = _json.dumps({
                "type": "tiktok_drip_followup",
                "device_id": device_id,
                "params": {
                    "username": username,
                    "lead_id": lead_id,
                    "context": context,
                    "drip_stage": desc,
                    "cancel_if_replied": True,  # 如已回复则跳过
                },
                "priority": 40,  # 低优先级，不影响主流程
                "delay_seconds": int(
                    delay_hours * 3600
                    + _infer_best_send_offset(lead_id, delay_hours)
                ),
            }).encode()

            req = _ur.Request(
                f"{local_api_base()}/tasks",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = _ur.urlopen(req, timeout=5)
            resp_data = _json.loads(resp.read().decode())
            task_id = resp_data.get("task_id", "?")
            log.info(
                "[Drip] 已调度 Day%dH 跟进任务 %s → 用户 %s (lead#%d)",
                delay_hours, task_id[:8], username, lead_id,
            )
        except Exception as e:
            log.debug("[Drip] 调度 %s 跟进任务失败: %s", context, e)


def _cancel_drip_if_replied(lead_id: int, username: str):
    """
    当用户主动回复时，取消后续 drip 任务（避免过度打扰）。
    由 _on_message_classified 中 interested/meeting 时调用。
    """
    try:
        import json as _json
        import urllib.request as _ur

        # 查询该 lead 的待执行 drip 任务
        req = _ur.Request(
            f"{local_api_base()}/tasks?task_type=tiktok_drip_followup&status=pending",
            method="GET",
        )
        resp = _ur.urlopen(req, timeout=5)
        tasks = _json.loads(resp.read().decode())

        cancelled = 0
        for task in tasks:
            params = task.get("params") or {}
            if params.get("username") == username or params.get("lead_id") == lead_id:
                cancel_req = _ur.Request(
                    f"{local_api_base()}/tasks/{task['task_id']}/cancel",
                    method="POST",
                )
                try:
                    _ur.urlopen(cancel_req, timeout=3)
                    cancelled += 1
                except Exception:
                    pass

        if cancelled:
            log.info("[Drip] 用户 %s 已回复，取消了 %d 个待发跟进任务", username, cancelled)

    except Exception as e:
        log.debug("[Drip] 取消 drip 任务失败: %s", e)
