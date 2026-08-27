"""统一收件箱——关系阶段 / Copilot 上下文构建（巨石拆分 slice 4）。

从 ``unified_inbox_routes.py`` 抽出的**关系阶段与 Copilot 联动上下文**构建族：
会话关系上下文、客户/会话级关系阶段（确认制 + 同步）、@mention 上下文、Copilot
联动上下文与润色、Copilot 曝光/采纳埋点。

依赖层级：仅依赖 services（_contacts_store/_inbox_store）、auth（_session_agent/
_agent_from_request）与各自的惰性跨模块 import，不反向依赖 routes，故无循环 import。
routes.py 等价重导出，对外引用路径保持不变。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Request

from src.ai.translation_service import detect_language, normalize_lang
from src.inbox.normalizer import conv_id
from src.web.routes.unified_inbox_auth import _agent_from_request, _session_agent
from src.web.routes.unified_inbox_helpers import FUNNEL_STAGE_LABELS, _PLATFORM_LABELS
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _conv_relationship_context(
    request: Request, conversation_id: str, store: Any,
) -> Dict[str, Any]:
    """P43/P45：拉取会话关系阶段上下文（轮次 + 亲密度 + contact_id）。"""
    cid = str(conversation_id or "").strip()
    ctx: Dict[str, Any] = {
        "conversation_id": cid,
        "message_count": 0,
        "exchange_count": 0,
        "intimacy_score": None,
        "contact_id": "",
        "last_msg_text": "",
        "last_msg_direction": "in",
    }
    if store is None or not cid:
        return ctx
    try:
        msg_count = store._conn.execute(
            "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?", (cid,),
        ).fetchone()["c"]
        ctx["message_count"] = int(msg_count or 0)
        ctx["exchange_count"] = max(0, ctx["message_count"] // 2)
        rows = store._conn.execute(
            """SELECT direction, text FROM messages
               WHERE conversation_id = ? ORDER BY ts DESC LIMIT 20""",
            (cid,),
        ).fetchall()
        for r in rows:
            if r["direction"] in ("in", "inbound"):
                ctx["last_msg_text"] = str(r["text"] or "")
                ctx["last_msg_direction"] = "in"
                break
            ctx["last_msg_direction"] = str(r["direction"] or "in")
        meta = store.get_conv_meta(cid) or {}
        ctx["contact_id"] = str(meta.get("contact_id") or "")
        if ctx["contact_id"]:
            cs = _contacts_store(request)
            if cs is not None:
                try:
                    journey = cs.get_journey_by_contact(ctx["contact_id"])
                    if journey is not None:
                        ctx["intimacy_score"] = float(journey.intimacy_score or 0)
                except Exception:
                    pass
    except Exception:
        logger.debug("_conv_relationship_context 失败", exc_info=True)
    return ctx


def _build_copilot_context(
    request: Request,
    conversation_id: str,
    store: Any,
    *,
    trigger: str = "",
    workflow_text: str = "",
    workflow_chain_name: str = "",
    workflow_step: int = 0,
    mention_body: str = "",
    mention_from: str = "",
) -> Dict[str, Any]:
    """P49：组装 Copilot 联动上下文（阶段 / 流失 / 工作链 / @mention）。"""
    rel = _build_relationship_stage_payload(request, conversation_id, store)
    mctx = _mention_context_for_conv(request, conversation_id, store)
    me = _session_agent(request)["agent_id"]

    mention_note = mention_body
    mention_agent = mention_from
    if store is not None and not mention_note:
        try:
            recent = store.get_recent_mention_note(conversation_id, me)
            if recent:
                mention_note = str(recent.get("body") or "")
                mention_agent = str(recent.get("agent_name") or recent.get("agent_id") or "")
        except Exception:
            pass

    if not trigger and mention_note:
        trigger = "mention"
    elif not trigger and mctx.get("churn_level") == "high":
        trigger = "churn"
    elif not trigger and rel.get("reunion"):
        trigger = "reunion"

    contact_id = str((rel.get("context") or {}).get("contact_id") or "")
    recent_downgrade = False
    if store is not None and contact_id:
        try:
            import time as _t
            week_ago = _t.time() - 7 * 86400
            for row in store.list_contact_stage_audits(contact_id, limit=5):
                if (
                    row.get("action") == "stage_downgrade"
                    and float(row.get("ts") or 0) > week_ago
                ):
                    recent_downgrade = True
                    break
        except Exception:
            pass

    return {
        "trigger": trigger,
        "stage": str(rel.get("display_stage") or rel.get("stage") or "initial"),
        "stage_label": rel.get("display_stage_label") or rel.get("stage_label") or "",
        "contact_stage": rel.get("contact_stage") or "",
        "contact_stage_label": rel.get("contact_stage_label") or "",
        "next_stage_label": rel.get("next_stage_label") or "",
        "pending_stage_label": rel.get("pending_stage_label") or "",
        "pending_advancement": bool(rel.get("pending_advancement")),
        "reunion": bool(rel.get("reunion")),
        "recent_downgrade": recent_downgrade,
        "churn_level": mctx.get("churn_level") or "",
        "claim_agent_id": mctx.get("claim_agent_id") or "",
        "overdue_chain": mctx.get("overdue_chain"),
        "workflow_text": workflow_text,
        "workflow_chain_name": workflow_chain_name,
        "workflow_step": int(workflow_step or 0),
        "mention_note": mention_note,
        "mention_from": mention_agent,
    }


async def _maybe_polish_copilot(
    request: Request,
    result: Dict[str, Any],
    *,
    conversation_id: str,
    partial_text: str,
    last_customer_msg: str,
    polish_requested: bool,
) -> Dict[str, Any]:
    """P52：可选 LLM 润色（失败回退规则建议）。"""
    from src.inbox.copilot_polisher import (
        get_polish_config,
        polish_suggestions,
        should_polish,
    )
    cm = getattr(request.app.state, "config_manager", None)
    cfg = get_polish_config(cm)
    if not should_polish(
        polish_requested=polish_requested,
        partial_text=partial_text,
        cfg=cfg,
    ):
        result["polished"] = False
        return result
    ai_client = getattr(request.app.state, "ai_client", None)
    ctx = result.get("context") or {}
    pr = await polish_suggestions(
        ai_client,
        result.get("suggestions") or [],
        context=ctx,
        last_customer_msg=last_customer_msg,
        cfg=cfg,
    )
    result["suggestions"] = pr.get("suggestions") or result.get("suggestions")
    result["polished"] = bool(pr.get("polished"))
    if pr.get("polish_error"):
        result["polish_error"] = pr["polish_error"]
    if pr.get("polish_count"):
        result["polish_count"] = pr["polish_count"]
    if result.get("polished"):
        store = _inbox_store(request)
        if store is not None:
            agent_id, _ = _agent_from_request(request)
            store.record_draft_audit(
                "", action="copilot_polish", agent_id=agent_id,
                reason=f"润色 {pr.get('polish_count', 0)} 条 Copilot 建议",
                conversation_id=conversation_id,
            )
    return result


def _record_copilot_impression_if_prefill(
    store: Any,
    conversation_id: str,
    agent_id: str,
    payload: Dict[str, Any],
    *,
    partial_text: str,
) -> None:
    """P54：仅预填（空 partial）记曝光，避免打字 debounce 刷屏。"""
    if store is None or (partial_text or "").strip():
        return
    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return
    ctx = payload.get("context") or {}
    top = suggestions[0] if suggestions else {}
    try:
        store.record_copilot_impression(
            conversation_id, agent_id,
            trigger=str(ctx.get("trigger") or payload.get("trigger") or "open"),
            stage=str(ctx.get("stage") or payload.get("stage") or "initial"),
            polished=bool(payload.get("polished")),
            suggestion_count=len(suggestions),
            top_source=str(top.get("source") or ""),
        )
    except Exception:
        pass


def _record_copilot_adopt_from_send(
    store: Any,
    conversation_id: str,
    agent_id: str,
    text: str,
    meta: Any,
) -> None:
    """P54：发送时记录 Copilot 采纳（best-effort）。"""
    if store is None or not isinstance(meta, dict):
        return
    from src.inbox.copilot_stats import classify_adoption
    suggested = str(meta.get("suggested_text") or meta.get("text") or "")
    match = str(meta.get("match") or "").strip() or classify_adoption(suggested, text)
    try:
        store.record_copilot_adopt(
            conversation_id, agent_id,
            match=match,
            source=str(meta.get("source") or ""),
            polished=bool(meta.get("polished")),
            trigger=str(meta.get("trigger") or ""),
            stage=str(meta.get("stage") or ""),
            suggested_preview=suggested,
            sent_preview=text,
        )
    except Exception:
        pass


def _mention_context_for_conv(
    request: Request, conversation_id: str, store: Any,
) -> Dict[str, Any]:
    """P48：组装 @mention 推荐所需会话上下文。"""
    rel = _build_relationship_stage_payload(request, conversation_id, store)
    ctx = rel.get("context") or {}
    stage = str(rel.get("display_stage") or rel.get("stage") or "initial")
    churn_level = ""
    claim_agent_id = ""
    overdue_chain = False
    if store is not None:
        try:
            meta = store.get_conv_meta(conversation_id) or {}
            churn_raw = str(meta.get("churn_risk") or "")
            if churn_raw:
                cd = json.loads(churn_raw)
                churn_level = str(cd.get("level") or "")
        except Exception:
            pass
        try:
            claim = store.get_conversation_claim(conversation_id)
            if claim:
                claim_agent_id = str(claim.get("agent_id") or "")
        except Exception:
            pass
        try:
            overdue_chain = store.has_overdue_chain_execution(conversation_id)
        except Exception:
            pass
    return {
        "stage": stage,
        "stage_label": rel.get("display_stage_label") or rel.get("stage_label") or "",
        "churn_level": churn_level,
        "claim_agent_id": claim_agent_id,
        "overdue_chain": overdue_chain,
        "contact_id": ctx.get("contact_id") or "",
    }


def _build_contact_relationship_payload(
    request: Request,
    contact_id: str,
    store: Any,
) -> Dict[str, Any]:
    """P50：客户级关系阶段（聚合信号 + 冲突检测）。"""
    from src.inbox.contact_rel_stage import (
        detect_stage_conflict,
        enrich_with_contact_stage,
    )
    from src.inbox.relationship_stage import compute_relationship_stage, enrich_with_manual_state

    cs = _contacts_store(request)
    intimacy_score = None
    message_count = 0
    conv_ids: List[str] = []
    if store is not None:
        try:
            rows = store._conn.execute(
                "SELECT conversation_id FROM conversations WHERE contact_id = ? LIMIT 30",
                (contact_id,),
            ).fetchall()
            conv_ids = [r["conversation_id"] for r in rows]
            if conv_ids:
                ph = ",".join("?" * len(conv_ids))
                message_count = store._conn.execute(
                    f"SELECT COUNT(*) as c FROM messages WHERE conversation_id IN ({ph})",
                    conv_ids,
                ).fetchone()["c"]
        except Exception:
            pass
    if cs is not None:
        try:
            journey = cs.get_journey_by_contact(contact_id)
            if journey is not None:
                intimacy_score = float(journey.intimacy_score or 0)
        except Exception:
            pass

    contact_rec = store.get_contact_rel_stage(contact_id) if store else None
    contact_stage = str((contact_rec or {}).get("confirmed_stage") or "")
    conv_stages = store.list_conv_rel_stages_for_contact(contact_id) if store else {}
    conflict = detect_stage_conflict(contact_stage, conv_stages)

    exchange_count = max(0, int(message_count or 0) // 2)
    computed = compute_relationship_stage(
        exchange_count=exchange_count, intimacy_score=intimacy_score,
        previous_stage=contact_stage,
    )
    confirmed = contact_stage or computed.get("stage") or "initial"
    reunion_ack = float((contact_rec or {}).get("reunion_ack_ts") or 0)

    result = enrich_with_manual_state(
        computed,
        confirmed_stage=confirmed,
        pending_stage="",
        reunion_ack_ts=reunion_ack,
    )
    return enrich_with_contact_stage(
        result,
        contact_stage=contact_stage,
        contact_updated_by=str((contact_rec or {}).get("updated_by") or ""),
        conflict=conflict,
    )


def _build_relationship_stage_payload(
    request: Request,
    conversation_id: str,
    store: Any,
    *,
    emit_pending_event: bool = False,
) -> Dict[str, Any]:
    """P43/P46/P50：构建关系阶段响应（确认制 + 客户级同步）。"""
    import time as _t
    from src.inbox.contact_rel_stage import (
        detect_stage_conflict,
        enrich_with_contact_stage,
    )
    from src.inbox.relationship_stage import (
        compute_relationship_stage,
        enrich_with_manual_state,
    )
    from src.utils.companion_relationship import STAGE_ORDER as _STAGES

    ctx = _conv_relationship_context(request, conversation_id, store)
    contact_id = str(ctx.get("contact_id") or "")
    meta = store.get_rel_stage_meta(conversation_id) if store else {
        "confirmed": "", "pending": "", "pending_ts": 0.0, "reunion_ack_ts": 0.0,
    }
    contact_rec = store.get_contact_rel_stage(contact_id) if store and contact_id else None
    contact_stage = str((contact_rec or {}).get("confirmed_stage") or "")
    confirmed = meta["confirmed"]
    computed = compute_relationship_stage(
        exchange_count=ctx["exchange_count"],
        intimacy_score=ctx["intimacy_score"],
        previous_stage=confirmed or contact_stage,
    )

    # P50：首次种子 — 优先客户级，否则写回客户级
    if store is not None and not confirmed:
        seed = contact_stage or computed.get("stage") or ""
        if seed:
            store.confirm_rel_stage(conversation_id, seed)
            confirmed = seed
            meta["confirmed"] = confirmed
            if contact_id and not contact_stage:
                store.set_contact_rel_stage(contact_id, seed)

    ci = _STAGES.index(computed["stage"]) if computed["stage"] in _STAGES else 0
    di = _STAGES.index(confirmed) if confirmed in _STAGES else 0
    pending_new = False
    if store is not None and ci > di:
        if meta["pending"] != computed["stage"]:
            store.set_rel_stage_pending(conversation_id, computed["stage"])
            pending_new = True
        pending_stage = computed["stage"]
    elif store is not None and meta["pending"]:
        store.clear_rel_stage_pending(conversation_id)
        pending_stage = ""
    else:
        pending_stage = meta["pending"] if ci > di else ""

    reunion_ack = float(meta["reunion_ack_ts"] or 0)
    if contact_rec and float(contact_rec.get("reunion_ack_ts") or 0) > reunion_ack:
        reunion_ack = float(contact_rec["reunion_ack_ts"])

    result = enrich_with_manual_state(
        computed,
        confirmed_stage=confirmed,
        pending_stage=pending_stage,
        reunion_ack_ts=reunion_ack,
    )

    conflict = None
    if store and contact_id:
        conv_stages = store.list_conv_rel_stages_for_contact(contact_id)
        conflict = detect_stage_conflict(contact_stage, conv_stages)
        result = enrich_with_contact_stage(
            result,
            contact_stage=contact_stage,
            contact_updated_by=str((contact_rec or {}).get("updated_by") or ""),
            conflict=conflict,
        )

    if emit_pending_event and pending_new:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("stage_advance_pending", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "confirmed_stage": confirmed,
                "confirmed_stage_label": result.get("confirmed_stage_label"),
                "pending_stage": result.get("pending_stage"),
                "pending_stage_label": result.get("pending_stage_label"),
                "ts": _t.time(),
            })
        except Exception:
            pass

    return {**result, "context": ctx}


def _memory_bullets(request: Request, key: str, query: str = "") -> List[str]:
    store = getattr(request.app.state, "episodic_memory_store", None)
    if store is None or not hasattr(store, "get_bullets_for_prompt"):
        return []
    try:
        raw = store.get_bullets_for_prompt(key, max_items=6, query_text=query) or ""
    except Exception:
        return []
    out: List[str] = []
    for line in str(raw).splitlines():
        item = line.strip().lstrip("-• ").strip()
        if item:
            out.append(item)
    return out[:6]


def _lookup_contacts_enrichment(
    request: Request,
    platform: str,
    account_id: str,
    chat_key: str,
) -> Optional[Dict[str, Any]]:
    """按渠道身份查 Contact/Journey，供工作台客户档案右栏展示。"""
    store = _contacts_store(request)
    if store is None or not platform or not chat_key:
        return None
    try:
        ci = store.get_ci_by_external(platform, account_id, chat_key)
        if ci is None:
            with store._lock:  # noqa: SLF001
                row = store._conn.execute(  # noqa: SLF001
                    "SELECT * FROM channel_identities "
                    "WHERE channel=? AND external_id=? ORDER BY linked_at ASC LIMIT 1",
                    (platform, chat_key),
                ).fetchone()
            if row is None:
                return None
            from src.contacts.store import _row_to_ci
            ci = _row_to_ci(row)
        contact = store.get_contact(ci.contact_id)
        journey = store.get_journey_by_contact(ci.contact_id)
        events: List[Dict[str, Any]] = []
        if journey is not None:
            events = store.list_events(journey.journey_id, limit=5)
        funnel = journey.funnel_stage if journey else ""
        intimacy = journey.intimacy_score if journey else None
        # Phase 5-4：留资属性 + 老客户识别（同一 Contact 有多渠道身份 / 经留资合并）
        attributes: Dict[str, str] = {}
        try:
            attributes = store.get_contact_attributes(ci.contact_id) or {}
        except Exception:
            attributes = {}
        identity_channels: List[str] = []
        try:
            ids_map = store.list_channel_identities_for_contacts([ci.contact_id])
            for c in ids_map.get(ci.contact_id, []) or []:
                ch = getattr(c, "channel", "")
                if ch and ch not in identity_channels:
                    identity_channels.append(ch)
        except Exception:
            identity_channels = [ci.channel]
        is_returning = (
            len(identity_channels) > 1
            or str(getattr(ci, "linked_via", "")).startswith("prechat_")
        )
        return {
            "contact_id": ci.contact_id,
            "primary_name": (contact.primary_name if contact else "") or "",
            "funnel_stage": funnel,
            "funnel_stage_label": FUNNEL_STAGE_LABELS.get(funnel, funnel),
            "intimacy_score": intimacy,
            "readiness_score": journey.readiness_score if journey else None,
            "engagement_score": journey.engagement_score if journey else None,
            "journey_id": journey.journey_id if journey else "",
            "recent_events": events[:5],
            "channel_identity": ci.to_dict(),
            "attributes": attributes,
            "identity_channels": identity_channels,
            "is_returning": is_returning,
        }
    except Exception:
        logger.debug("contacts enrichment 失败（已忽略）", exc_info=True)
        return None


def _build_contact_timeline(
    request: Request,
    identities: List[Dict[str, Any]],
    msg_limit: int,
    before_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """聚合一个 Contact 名下所有渠道身份的消息为单条时间线（按 ts 升序）。

    每个渠道身份 → conv_id(channel, account_id, external_id) → InboxStore.list_recent_messages。
    取每渠道**最近** msg_limit 条（可用 before_ts 游标向更早翻页），跨渠道合并后取最近
    msg_limit 条，避免大客户拉全量。
    """
    store = _inbox_store(request)
    if store is None or not identities:
        return []
    merged: List[Dict[str, Any]] = []
    per_conv = max(10, min(200, msg_limit))
    for ci in identities:
        channel = str(ci.get("channel") or "")
        account_id = str(ci.get("account_id") or "default")
        external_id = str(ci.get("external_id") or "")
        if not channel or not external_id:
            continue
        cid = conv_id(channel, account_id, external_id)
        try:
            rows = store.list_recent_messages(cid, limit=per_conv, before_ts=before_ts)
        except Exception:
            logger.debug("timeline list_recent_messages 失败 cid=%s", cid, exc_info=True)
            continue
        for m in rows:
            text = str(m.get("text") or m.get("original_text") or "")
            merged.append({
                "channel": channel,
                "platform_label": _PLATFORM_LABELS.get(channel, channel),
                "account_id": account_id,
                "conversation_id": cid,
                "direction": m.get("direction") or "in",
                "text": text,
                "translated_text": (
                    m.get("translated_text") if m.get("translated_text") not in (None, "", text)
                    else ""
                ),
                "ts": m.get("ts") or 0,
            })
    merged.sort(key=lambda x: x.get("ts") or 0)
    if len(merged) > msg_limit:
        merged = merged[-msg_limit:]
    return merged


# ── 快捷回复聚合的「准入 + 显示名」治理（P0 2026-08-18；P1 同日放行变量模板）──
# templates.yaml 是**双用途文件**：坐席话术与机器人运行时模板（error/confirmation/
# gxp_* 查单流程等）同住一个命名空间。旧实现全量倾倒 → 坐席面板出现 `greeting #2`/
# `error.general` 这类机器键名黑话 + `{order_number}` 占位符原样填入的穿帮风险。
# 治理三原则（键名绝不改——skills/base.py 机器人链按键取模板，改键=破坏自动应答）：
#   ① 准入：dict 型值（系统响应映射）/ test / gxp_* → 不进坐席面板；
#      含 {var} 占位符的普通模板 **P1 起放行**——面板填入前有变量补全表单接住
#      （cp-kb _vars 流程），不再一刀切剔除（「带单号查单」对坐席是真需求）；
#   ② 显示名：复用后台「话术模板」页同一套 i18n 词条 tp_nm_<key>（zh+en，
#      templates_page.py pack）——坐席面板与管理页叫法天然一致；无词条的机器键
#      回落正文首句，非 ASCII 键（运营手写人话键）原样展示；
#   ③ 变体：同键多条按「· 备选N」（tp_alt_n）编号，替代 `key #2` 黑话。
_QTPL_SYSTEM_KEYS = frozenset({"test"})
_QTPL_SYSTEM_KEY_PREFIXES = ("gxp_",)
_QTPL_CLAUSE_RE = re.compile(r"[，。！？!?；;：:,…~～\n]")
_QTPL_LABEL_MAX = 14
# 键 → 分类码（前端按分类着色/后续分类筛选）。未登记键与 workspace/messenger 源
# 一律归 custom（自定义/常用）。分类码是稳定契约，展示名由前端 i18n 出。
_QTPL_CATEGORY = {
    "greeting": "greet", "welcome": "greet", "farewell": "greet",
    "order_query": "order", "order_confirm": "order", "status_check": "order",
    "status_query": "order", "payment_remind": "order",
    "price_check": "biz", "rate_query": "biz", "rate_update": "biz",
    "channel_info": "biz", "channel_enable": "biz", "channel_disable": "biz",
    "deposit_success": "biz", "withdraw_apply": "biz", "withdraw_fail": "biz",
    "withdraw_success": "biz",
    "complaint": "aftersale", "help": "aftersale", "maintenance": "aftersale",
    "small_talk": "chat", "small_talk_short": "chat",
}


def _qtpl_clause(text: str, limit: int = _QTPL_LABEL_MAX) -> str:
    """取正文首个短句当兜底显示名（无 tp_nm_* 词条的机器键不再裸奔）。"""
    raw = str(text or "").strip()
    head = _QTPL_CLAUSE_RE.split(raw, 1)[0].strip() or raw
    return head[:limit]


# ── 团队话术多语言变体旁挂档（V1 2026-08-18）────────────────────────────────
# templates.yaml **不能**加语言维度（机器人自动应答共用该文件，且 dict 型值在
# 坐席准入规则里=系统模板）→ 变体独立旁挂 templates_i18n.yaml：
#   <templates.yaml 键>:
#     en:
#       - "人工定稿文本"                    # 纯字符串 = 已审核
#       - {text: "机器起草文本", approved: false}   # 机器稿待确认（允许填入，前端标注）
# 语义与出站机翻链的分工：变体只覆盖**头部高风险话术**的品牌口径；长尾继续走
# 发送时机翻。缺变体一律回落中文原文，绝不阻塞。起草工具：
# scripts/templates_i18n_draft.py（机器起草→运营确认，对齐 KB kb_translations
# 的 auto_translated→确认 心智）。读档 mtime 缓存=热更新免重启。
_QTPL_I18N_FILE = "templates_i18n.yaml"
_QTPL_I18N_CACHE: Dict[str, Any] = {"key": None, "mtime": -1.0, "data": {}}


def _qtpl_i18n_candidates(config_manager) -> List[Any]:
    """与 get_dynamic_templates_config 同一双落点：实例 config → 引擎根回落。"""
    from pathlib import Path
    out: List[Any] = []
    try:
        p = getattr(config_manager, "config_path", None)
        if p:
            out.append(Path(p).parent / _QTPL_I18N_FILE)
    except Exception:
        pass
    out.append(Path(__file__).resolve().parents[3] / "config" / _QTPL_I18N_FILE)
    return out


def _load_templates_i18n(config_manager) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """旁挂档 → {键: {语言: [{text, approved}]}}（mtime 缓存；坏档保旧值绝不抛）。"""
    path = None
    for cand in _qtpl_i18n_candidates(config_manager):
        try:
            if cand.exists():
                path = cand
                break
        except Exception:
            continue
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except Exception:
        return _QTPL_I18N_CACHE["data"] if _QTPL_I18N_CACHE["key"] else {}
    ck = str(path)
    if _QTPL_I18N_CACHE["key"] == ck and _QTPL_I18N_CACHE["mtime"] >= mtime:
        return _QTPL_I18N_CACHE["data"]
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        if isinstance(raw, dict):
            for key, langs in raw.items():
                k = str(key or "").strip()
                if not k or k.startswith("_") or not isinstance(langs, dict):
                    continue
                lang_map: Dict[str, List[Dict[str, Any]]] = {}
                for lang, items in langs.items():
                    lcode = normalize_lang(str(lang or ""))
                    if not lcode or lcode == "zh":
                        continue
                    lst = items if isinstance(items, list) else [items]
                    norm: List[Dict[str, Any]] = []
                    for it in lst:
                        if isinstance(it, str) and it.strip():
                            norm.append({"text": it.strip(), "approved": True})
                        elif isinstance(it, dict):
                            t = str(it.get("text") or "").strip()
                            if t:
                                norm.append({"text": t,
                                             "approved": bool(it.get("approved", False))})
                    if norm:
                        lang_map[lcode] = norm
                if lang_map:
                    data[k] = lang_map
        _QTPL_I18N_CACHE.update({"key": ck, "mtime": mtime, "data": data})
    except Exception:
        logger.debug("templates_i18n.yaml 读取失败（保留旧缓存）", exc_info=True)
        if _QTPL_I18N_CACHE["key"] != ck:
            return {}
    return _QTPL_I18N_CACHE["data"]


def _qtpl_i18n_pick(langs_map: Optional[Dict[str, List[Dict[str, Any]]]]) -> Dict[str, Dict[str, Any]]:
    """每语言只下发一条：已审核优先，无则首条机器稿（前端据 approved 标注「待确认」）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for lcode, items in (langs_map or {}).items():
        pick = next((it for it in items if it.get("approved")), None)
        if pick is None and items:
            pick = items[0]
        if pick:
            out[lcode] = {"text": pick["text"], "approved": bool(pick.get("approved"))}
    return out


def _qtpl_is_system_key(key: str) -> bool:
    k = str(key or "").strip().lower()
    return k in _QTPL_SYSTEM_KEYS or any(k.startswith(p) for p in _QTPL_SYSTEM_KEY_PREFIXES)


def _qtpl_display_label(key: str, first_text: str, translate) -> str:
    """显示名解析：tp_nm_<key> 词条 > 非 ASCII 人话键原样 > 正文首句。"""
    if translate is not None:
        try:
            named = str(translate(f"tp_nm_{key}", "") or "").strip()
        except Exception:
            named = ""
        if named:
            return named
    if re.search(r"[^\x00-\x7f]", key):
        return key
    return _qtpl_clause(first_text)


def _collect_quick_templates(
    config_manager,
    translate: Optional[Callable[..., str]] = None,
) -> List[Dict[str, Any]]:
    """聚合快捷回复：workspace 专属 > messenger approval > templates.yaml（经准入治理）。

    ``translate(key, default, **fmt)``＝请求级 i18n 解析（路由层传 ``tr`` 包装；
    None 时显示名走正文首句兜底，绝不回落机器键名）。响应行除兼容字段
    ``label/text/source`` 外新增 ``key``（templates.yaml 原始键，编辑深链用）、
    ``category``（分类码）、``variant``（同键第 N 条，1 起）。
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def _add(label: str, text: str, source: str = "", *,
             key: str = "", category: str = "custom", variant: int = 1,
             i18n: Optional[Dict[str, Any]] = None) -> None:
        label = str(label or "").strip()
        text = str(text or "").strip()
        if not label or not text:
            return
        dedup = f"{label}\0{text}"
        if dedup in seen:
            return
        seen.add(dedup)
        row: Dict[str, Any] = {
            "label": label, "text": text, "source": source,
            "key": key, "category": category, "variant": variant,
        }
        if i18n:
            row["i18n"] = i18n
        out.append(row)

    cfg: Dict[str, Any] = {}
    if config_manager is not None:
        cfg = getattr(config_manager, "config", None) or {}

    for t in (cfg.get("workspace") or {}).get("quick_templates") or []:
        if isinstance(t, dict):
            _add(t.get("label"), t.get("text"), "workspace")

    for t in (cfg.get("messenger_rpa") or {}).get("approval_templates") or []:
        if isinstance(t, dict):
            _add(t.get("label"), t.get("text"), "messenger")

    if config_manager is not None and hasattr(config_manager, "get_dynamic_templates_config"):
        try:
            dyn = config_manager.get_dynamic_templates_config() or {}
            i18n_all = _load_templates_i18n(config_manager)
            for key, val in dyn.items():
                k = str(key or "").strip()
                if not k or _qtpl_is_system_key(k):
                    continue
                if isinstance(val, str):
                    # 单行模板在 /templates 页保存为纯 str——旧实现漏掉这一形态，
                    # 运营新增单行话术从不出现在面板（静默洞，本轮一并修）。
                    texts = [val]
                elif isinstance(val, list):
                    texts = [x for x in val if isinstance(x, str)]
                else:
                    # dict 型 = 机器人系统响应映射（error/confirmation/success/failure），
                    # 不是坐席话术，整键跳过。
                    continue
                visible = [s.strip() for s in texts if s and s.strip()]
                if not visible:
                    continue
                base = _qtpl_display_label(k, visible[0], translate)
                cat = _QTPL_CATEGORY.get(k.lower(), "custom")
                # 多语言变体挂在**键**上（备选彼此可互换，译稿不与某条备选一一对应）：
                # 同键每张卡带同一份 per-lang 单条精选，填入时刻由前端按会话语言取。
                picks = _qtpl_i18n_pick(i18n_all.get(k))
                for i, text in enumerate(visible):
                    label = base
                    if i:
                        suffix = ""
                        if translate is not None:
                            try:
                                suffix = str(translate("tp_alt_n", "", n=i + 1) or "")
                            except Exception:
                                suffix = ""
                        label = f"{base}{suffix}" if suffix else f"{base} #{i + 1}"
                    _add(label, text, "templates", key=k, category=cat,
                         variant=i + 1, i18n=picks or None)
        except Exception:
            logger.debug("加载 templates.yaml 失败", exc_info=True)

    return out[:60]


def _context_relationship(request: Request, key: str, chat_key: str) -> Dict[str, Any]:
    store = getattr(request.app.state, "context_store", None)
    if store is None or not hasattr(store, "get"):
        return {}
    try:
        ctx = store.get(key)
    except Exception:
        return {}
    rel_root = ctx.get("companion_relationship") if isinstance(ctx, dict) else {}
    if not isinstance(rel_root, dict):
        return {}
    rel = rel_root.get(str(chat_key)) or rel_root.get("_default") or {}
    return rel if isinstance(rel, dict) else {}


# 关系阶段机器码集（与 companion_relationship.STAGE_ORDER 同序；静态复刻避免
# routes→utils 新增依赖——码↔词典对齐由 test_i18n_en_payload_gate 的
# inbox.rel.stage.* 键门禁钉住）。
_REL_STAGE_KEYS = ("initial", "warming", "intimate", "steady")


def _build_profile(request: Request, chat: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    chat_key = str(chat.get("chat_key") or "")
    profile_key = f"{chat.get('platform')}:{chat.get('account_id')}:{chat_key}"
    latest_text = " ".join(str(m.get("text") or "") for m in messages[-5:])
    rel = dict(chat.get("relationship") or {})
    rel_from_ctx = _context_relationship(request, chat_key, chat_key)
    if rel_from_ctx:
        rel.update(rel_from_ctx)
    # i18n P0 缺口回填（2026-08-20，接 08-19 主批）：stage/tags 曾硬编码中文直发
    # 载荷（EN 界面泄漏，四截图事故面之一）。收口口径与主批一致——载荷带机器码
    # （stage_key / tags_keys），展示标签按请求语言出词典；zh 文案与
    # STAGE_LABEL_ZH / FUNNEL_STAGE_LABELS 逐字对齐（同一阶段绝不两种叫法）。
    raw_stage = str(rel.get("stage") or "").strip()
    if raw_stage.lower() in _REL_STAGE_KEYS:
        stage_key = raw_stage.lower()
    elif raw_stage:
        stage_key = ""  # 历史自由文本——原样透传，不猜码不错译
    else:
        stage_key = ("steady" if len(messages) >= 20
                     else "warming" if len(messages) >= 8 else "initial")
    stage = tr(request, f"inbox.rel.stage.{stage_key}") if stage_key else raw_stage
    memories = _memory_bullets(request, profile_key, latest_text) or _memory_bullets(request, chat_key, latest_text)
    contacts = _lookup_contacts_enrichment(
        request,
        str(chat.get("platform") or ""),
        str(chat.get("account_id") or "default"),
        chat_key,
    )
    if contacts:
        if contacts.get("primary_name"):
            display_name = contacts["primary_name"]
        else:
            display_name = chat.get("name") or chat_key
        if contacts.get("intimacy_score") is not None:
            rel["intimacy_score"] = contacts["intimacy_score"]
        if contacts.get("funnel_stage_label"):
            # 漏斗阶段优先于陪伴阶段（原语义不变）；有码走词典、缺码回落原 zh 标签
            fs_code = str(contacts.get("funnel_stage") or "").strip()
            if fs_code:
                stage_key = fs_code.lower()
                stage = tr(request, f"inbox.funnel.{stage_key}",
                           str(contacts["funnel_stage_label"]))
            else:
                stage_key = ""
                stage = contacts["funnel_stage_label"]
    else:
        display_name = chat.get("name") or chat_key
    tag_pairs = _profile_tags(chat, messages, memories, contacts, request=request)
    return {
        "profile_key": profile_key,
        "display_name": display_name,
        "platform": chat.get("platform"),
        "platform_name": chat.get("platform_name"),
        "account_id": chat.get("account_id"),
        "account_label": chat.get("account_label"),
        "chat_key": chat_key,
        "language": chat.get("language") or detect_language(latest_text),
        "country_hint": "",
        "timezone_hint": "",
        "relationship": {
            "stage": stage,
            "stage_key": stage_key,
            "exchange_count": rel.get("exchange_count", len(messages)),
            "intimacy_score": rel.get("intimacy_score"),
            "updated_at": rel.get("updated_at"),
        },
        "activity": {
            "message_count": len(messages),
            "last_ts": chat.get("last_ts") or 0,
            "unread": chat.get("unread") or 0,
        },
        "memories": memories,
        "tags": [p[1] for p in tag_pairs],
        "tags_keys": [p[0] for p in tag_pairs],
        "contacts": contacts,
        "notes": "",
    }


def _profile_tags(
    chat: Dict[str, Any],
    messages: List[Dict[str, Any]],
    memories: List[str],
    contacts: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> List[Tuple[str, str]]:
    """会话画像 tags →（机器码, 按请求语言的展示标签）对列表。

    zh 文案与旧硬编码逐字一致（消费方/测试零漂移）；``request=None``（直调场景）
    回落中文默认——生产唯一调用方 ``_build_profile`` 恒传 request。
    """
    def _t(key: str, default: str, **fmt) -> str:
        if request is not None:
            try:
                return tr(request, key, default, **fmt)
            except Exception:
                pass
        try:
            return default.format(**fmt) if fmt else default
        except Exception:
            return default

    pairs: List[Tuple[str, str]] = []
    lang = str(chat.get("language") or "")
    if lang and lang != "unknown":
        pairs.append((f"lang:{lang}",
                      _t("inbox.profile.tag.lang", "语言:{lang}", lang=lang)))
    if (chat.get("unread") or 0) > 0:
        pairs.append(("unreplied", _t("inbox.profile.tag.unreplied", "待回复")))
    if len(messages) >= 8:
        pairs.append(("warming", _t("inbox.profile.tag.warming", "关系升温")))
    if memories:
        pairs.append(("memory", _t("inbox.profile.tag.memory", "有记忆")))
    if contacts:
        fs = contacts.get("funnel_stage") or ""
        if fs.startswith("LOST"):
            pairs.append(("churn_risk",
                          _t("inbox.profile.tag.churn_risk", "流失风险")))
        elif fs in {"HANDOFF_READY", "HANDOFF_SENT"}:
            pairs.append(("handoff", _t("inbox.profile.tag.handoff", "引流中")))
        elif fs in {"LINE_ENGAGED", "BONDED", "CONVERTED"}:
            pairs.append(("high_value",
                          _t("inbox.profile.tag.high_value", "高价值")))
        intim = contacts.get("intimacy_score")
        if intim is not None and intim >= 70:
            pairs.append(("high_intimacy",
                          _t("inbox.profile.tag.high_intimacy", "高亲密")))
        if contacts.get("is_returning"):
            pairs.append(("returning",
                          _t("inbox.profile.tag.returning", "老客户")))
        if contacts.get("attributes"):
            pairs.append(("has_lead_info",
                          _t("inbox.profile.tag.has_lead_info", "已留资")))
    return pairs[:8]
