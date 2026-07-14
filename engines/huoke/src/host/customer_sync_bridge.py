# -*- coding: utf-8 -*-
"""L1 (fb_store SQLite) → L2 (中央 PG) 双写门面.

接入方式
--------
greeting bot 在落 fb_store 的同时调本模块的 ``sync_*`` 函数, 把同一个事件
ALSO push 到中央客户画像. 全部 fire_and_forget + 全部 try/except 静默,
**任何失败都不影响调用方主流程** (跟现存 fb_store 写入失败的容错语义一致).

canonical_id 约定 (v1)
----------------------
greeting bot 阶段尚无 fb_uid 抓取, 用现存 ``peer_name`` 字符串. 为避免重名
碰撞 + 与未来 fb_uid 命名空间冲突:

    canonical_source = "facebook_name"
    canonical_id     = f"{device_id}::{peer_name}"

跨 device 的同名客户会成 N 行 (false negative), 但 greeting bot 实际业务里
"同一客户被多 device 加" 极少见. v2 引入 fb_uid 抓取后会:

1. 新 source = "facebook" (用 fb_uid)
2. 引入 customer_aliases 表 merge "facebook_name" → "facebook"

customer_id 用 push_client.compute_customer_id 算 UUIDv5, worker 离线时也
能不阻塞地拿到 ID (主控离线时全走本地 retry queue 兜底).

事件类型命名空间 (L2 customer_events.event_type)
-----------------------------------------------
- ``friend_request_sent`` / ``friend_request_risk``
- ``greeting_sent`` / ``greeting_fallback``

(对应 fb_store 的 CONTACT_EVT_ADD_FRIEND_SENT / *_RISK / *_GREETING_SENT /
GREETING_FALLBACK; 这里去掉 add_friend_ 前缀简化, L2 是 customer 视角)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CANONICAL_SOURCE = "facebook_name"
CHANNEL_FACEBOOK = "facebook"
CHANNEL_MESSENGER = "messenger"

# Phase-3 A/B: 客户分流到 v1/v2. hash(canonical_id) % N 一致性映射,
# 同一客户永远落同一 variant. 后续 chat_brain 按 variant 用不同话术.
AB_VARIANTS = ("v1", "v2")
AB_WINNER_CACHE_TTL_SEC = 300.0  # 5 min
AB_WINNER_TRAFFIC_PCT = 80       # 有 winner 时 80% 流量给赢家


_ab_winner_cache: Dict[str, Any] = {"winner": None, "graduated": False, "fetched_at": 0.0}
_ab_winner_lock = __import__("threading").Lock()


def _fetch_ab_winner_state() -> Dict[str, Any]:
    """Phase-6: 拿 winner + graduated 状态 (5 min cache).

    返回 {"winner": str|None, "graduated": bool}.
    """
    import time as _t
    with _ab_winner_lock:
        rec = _ab_winner_cache
        if _t.time() - rec.get("fetched_at", 0) < AB_WINNER_CACHE_TTL_SEC:
            return {"winner": rec.get("winner"), "graduated": rec.get("graduated", False)}

    # cache 过期, 同步去拉 (一般 < 50ms 因为主控本机)
    try:
        from urllib.request import urlopen
        from src.host.cluster_lock_client import get_coordinator_url
        url = get_coordinator_url().rstrip("/") + "/cluster/customers/ab/winner"
        with urlopen(url, timeout=3.0) as r:
            import json as _j
            data = _j.loads(r.read().decode("utf-8", errors="replace"))
        winner = (data.get("winner") or "").lower()
        if winner not in AB_VARIANTS:
            winner = None
        graduated = bool(data.get("graduated"))
        with _ab_winner_lock:
            _ab_winner_cache["winner"] = winner
            _ab_winner_cache["graduated"] = graduated
            _ab_winner_cache["fetched_at"] = _t.time()
        return {"winner": winner, "graduated": graduated}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] ab winner fetch failed: %s", exc)
        with _ab_winner_lock:
            _ab_winner_cache["fetched_at"] = _t.time()  # 防雪崩
        return {"winner": None, "graduated": False}


def _fetch_ab_winner() -> Optional[str]:
    """Backward-compat helper: 只返 winner."""
    return _fetch_ab_winner_state().get("winner")


def _ab_variant_for(canonical_id: str) -> str:
    """一致性 hash 分流.

    Phase-5: winner 不 graduated 时 80% 流量给赢家, 20% 对照组.
    Phase-6: winner graduated 时 100% 流量给赢家, 退实验.
    没 winner: 50/50.
    """
    if not canonical_id:
        return AB_VARIANTS[0]
    h = sum(ord(c) for c in canonical_id)

    state = _fetch_ab_winner_state()
    winner = state.get("winner")
    graduated = state.get("graduated", False)

    if winner in AB_VARIANTS:
        if graduated:
            # Phase-6: graduated 100% 给 winner
            return winner
        # Phase-5: 80% winner, 20% 对照
        if (h % 100) < AB_WINNER_TRAFFIC_PCT:
            return winner
        else:
            return AB_VARIANTS[1] if winner == AB_VARIANTS[0] else AB_VARIANTS[0]
    return AB_VARIANTS[h % len(AB_VARIANTS)]

# status 状态机 (与 store SQL 守卫一致):
# in_funnel < in_messenger < in_line < accepted_by_human < converted/lost
STATUS_IN_FUNNEL = "in_funnel"
STATUS_IN_MESSENGER = "in_messenger"
STATUS_IN_LINE = "in_line"


def _build_canonical_id(device_id: str, peer_name: str) -> str:
    return f"{device_id}::{peer_name}"


def _safe_worker_id() -> str:
    try:
        from src.host.cluster_lock_client import get_worker_id
        return get_worker_id()
    except Exception:  # noqa: BLE001
        return ""


def _ensure_customer(
    device_id: str,
    peer_name: str,
    *,
    ai_profile: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
) -> Optional[str]:
    """upsert_customer + 返回 customer_id (deterministic UUIDv5).

    status 传了主控会按状态机守卫单调升级 (终态 + 人工接管态不可降级).
    Phase-3: 自动分流 ab_variant 到 ai_profile (v1/v2).
    """
    canonical_id = _build_canonical_id(device_id, peer_name)
    # 自动注入 ab_variant (一致性 hash, 同 customer 总同 variant)
    profile = dict(ai_profile or {})
    profile.setdefault("ab_variant", _ab_variant_for(canonical_id))
    try:
        from src.host.central_push_client import upsert_customer
        return upsert_customer(
            canonical_source=CANONICAL_SOURCE,
            canonical_id=canonical_id,
            primary_name=peer_name,
            worker_id=_safe_worker_id() or None,
            device_id=device_id,
            ai_profile=profile,
            status=status,
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] upsert failed: %s", exc)
        return None


def sync_friend_request_sent(
    device_id: str,
    peer_name: str,
    *,
    status: str = "sent",
    persona_key: Optional[str] = None,
    preset_key: Optional[str] = None,
    source: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[str]:
    """好友请求落事件: status='sent' → friend_request_sent, 'risk' → friend_request_risk.

    返回 customer_id (调试/链路追踪用). None 表示 sync bridge 内部异常 (业务无感).
    """
    if not device_id or not peer_name:
        return None
    cid = _ensure_customer(
        device_id, peer_name,
        ai_profile={"persona_key": persona_key} if persona_key else None,
    )
    if not cid:
        return None
    event_type = "friend_request_sent" if status == "sent" else "friend_request_risk"
    try:
        from src.host.central_push_client import record_event
        record_event(
            customer_id=cid,
            event_type=event_type,
            worker_id=_safe_worker_id(),
            device_id=device_id,
            meta={
                "persona_key": persona_key or "",
                "preset_key": preset_key or "",
                "source": source or "",
                "has_note": bool(note),
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] friend_request event failed: %s", exc)
    return cid


def sync_greeting_sent(
    device_id: str,
    peer_name: str,
    *,
    greeting: str,
    template_id: Optional[str] = None,
    preset_key: Optional[str] = None,
    persona_key: Optional[str] = None,
    phase: Optional[str] = None,
    fallback: bool = False,
    content_lang: Optional[str] = None,
) -> Optional[str]:
    """打招呼成功落事件 + 聊天 (channel=facebook, direction=outgoing).

    fallback=True 时事件类型用 greeting_fallback (messenger 降级路径).
    返回 customer_id.
    """
    if not device_id or not peer_name or not greeting:
        return None
    cid = _ensure_customer(
        device_id, peer_name,
        ai_profile={"persona_key": persona_key} if persona_key else None,
    )
    if not cid:
        return None

    event_type = "greeting_fallback" if fallback else "greeting_sent"
    worker_id = _safe_worker_id()
    try:
        from src.host.central_push_client import record_event, record_chat
        record_event(
            customer_id=cid,
            event_type=event_type,
            worker_id=worker_id,
            device_id=device_id,
            meta={
                "template_id": template_id or "",
                "preset_key": preset_key or "",
                "persona_key": persona_key or "",
                "phase": phase or "",
                "msg_len": len(greeting),
            },
            fire_and_forget=True,
        )
        record_chat(
            customer_id=cid,
            channel=CHANNEL_FACEBOOK,
            direction="outgoing",
            content=greeting,
            content_lang=content_lang,
            ai_generated=False,
            template_id=template_id,
            worker_id=worker_id or None,
            device_id=device_id,
            meta={
                "preset_key": preset_key or "",
                "persona_key": persona_key or "",
                "phase": phase or "",
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] greeting push failed: %s", exc)
    return cid


# ── Messenger 入站 / 出站 / 引流 / handoff ────────────────────────────

def _detect_lang_safe(text: str) -> Optional[str]:
    """调 lang_detect 失败/为空时返 None, 不阻塞 push."""
    if not text:
        return None
    try:
        from src.ai.lang_detect import detect_language
        v = (detect_language(text) or "").strip()
        return v or None
    except Exception:  # noqa: BLE001
        return None


def _maybe_update_priority_from_emotion(
    customer_id: str,
    persona_key: str,
    recent_messages: List[Dict[str, str]],
) -> None:
    """Phase-5/7: 根据 emotion 评分实时更新 priority_tag.

    Phase-5 (overall):
        overall < 0.3 → low
        overall > 0.7 → high

    Phase-7 (多维独立 trigger, 优先级高于 overall, 因为更敏感):
        frustration > 0.7 → low (强烈不耐烦, 一维就够判)
        trust > 0.6 AND interest > 0.6 → high (双高 = 真有戏)

    LLM 失败 fallback 不动.
    """
    if not customer_id or not recent_messages:
        return
    try:
        from src.ai.chat_emotion_scorer import score_emotion
        result = score_emotion(recent_messages, persona_key=persona_key)
        if result.get("fallback"):
            return  # LLM 失败不操作
        trust = float(result.get("trust") or 0.5)
        interest = float(result.get("interest") or 0.5)
        frustration = float(result.get("frustration") or 0.5)
        overall = float(result.get("overall") or 0.5)

        # Phase-7: 多维独立判定 (覆盖 Phase-5 overall 判定)
        if frustration > 0.7:
            _push_priority(customer_id, "low")
            _emit_priority_event(customer_id, "low",
                                  reason="frustration_high",
                                  scores={"frustration": frustration})
            return
        if trust > 0.6 and interest > 0.6:
            _push_priority(customer_id, "high")
            _emit_priority_event(customer_id, "high",
                                  reason="trust_interest_dual_high",
                                  scores={"trust": trust, "interest": interest})
            return

        # Phase-5 overall 兜底
        if overall < 0.3:
            _push_priority(customer_id, "low")
        elif overall > 0.7:
            _push_priority(customer_id, "high")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] emotion priority failed: %s", exc)


def _emit_priority_event(
    customer_id: str,
    priority_tag: str,
    *,
    reason: str,
    scores: Dict[str, float],
) -> None:
    """Phase-7: 推 priority_changed 事件给主控 → SSE 广播给 L3 看板."""
    try:
        from src.host.central_push_client import record_event
        record_event(
            customer_id=customer_id,
            event_type="priority_changed",
            worker_id=_safe_worker_id(),
            device_id="",
            meta={
                "priority_tag": priority_tag,
                "reason": reason,
                "scores": scores,
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] priority event push failed: %s", exc)


def sync_messenger_incoming(
    device_id: str,
    peer_name: str,
    *,
    content: str,
    content_lang: Optional[str] = None,
    peer_type: str = "friend",
) -> Optional[str]:
    """对方发来 messenger 消息. 入站时升级 status='in_messenger'
    (主控状态机守卫: 已是 in_line/接管态/终态不会被降级).

    peer_type: 'friend' (check_messenger_inbox) / 'stranger' (check_message_requests).
    """
    if not device_id or not peer_name or not content:
        return None
    cid = _ensure_customer(
        device_id, peer_name, status=STATUS_IN_MESSENGER,
    )
    if not cid:
        return None
    lang = content_lang or _detect_lang_safe(content)
    worker_id = _safe_worker_id()
    try:
        from src.host.central_push_client import record_event, record_chat
        record_event(
            customer_id=cid,
            event_type="message_received",
            worker_id=worker_id,
            device_id=device_id,
            meta={"peer_type": peer_type, "msg_len": len(content)},
            fire_and_forget=True,
        )
        record_chat(
            customer_id=cid,
            channel=CHANNEL_MESSENGER,
            direction="incoming",
            content=content,
            content_lang=lang,
            ai_generated=False,
            worker_id=worker_id or None,
            device_id=device_id,
            meta={"peer_type": peer_type},
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] messenger_incoming push failed: %s", exc)

    # Phase-5: 根据当前消息 + 历史几条算 emotion, 更新 priority
    # 异步 fire-and-forget 不阻塞主流程
    try:
        from concurrent.futures import ThreadPoolExecutor
        global _emotion_executor
        try:
            _emotion_executor
        except NameError:
            _emotion_executor = ThreadPoolExecutor(max_workers=2,
                                                    thread_name_prefix="emo")
        # 简化: 只用当前 incoming 一条 + last outgoing 1 条 (从 fb_store 拉太重)
        msgs = [{"role": "user", "content": content}]
        _emotion_executor.submit(_maybe_update_priority_from_emotion,
                                  cid, "jp_female_midlife", msgs)
    except Exception:
        pass

    return cid


_emotion_executor = None


def sync_messenger_outgoing(
    device_id: str,
    peer_name: str,
    *,
    content: str,
    ai_decision: str = "reply",
    ai_generated: bool = True,
    template_id: Optional[str] = None,
    content_lang: Optional[str] = None,
    intent_tag: Optional[str] = None,
) -> Optional[str]:
    """B worker AI 回复 / 模板回复发出后调.

    ai_decision: 'reply' (AI 生成) / 'wa_referral' (引流话术) / 'skip' (不回, 一般不调本函数).
    ai_generated 跟 ai_decision 解耦: ChatBrain 生成的是 True; 模板/snippet 是 False.
    """
    if not device_id or not peer_name or not content:
        return None
    cid = _ensure_customer(
        device_id, peer_name, status=STATUS_IN_MESSENGER,
    )
    if not cid:
        return None
    lang = content_lang or _detect_lang_safe(content)
    worker_id = _safe_worker_id()
    try:
        from src.host.central_push_client import record_event, record_chat
        record_event(
            customer_id=cid,
            event_type="messenger_message_sent",
            worker_id=worker_id,
            device_id=device_id,
            meta={
                "ai_decision": ai_decision,
                "ai_generated": ai_generated,
                "template_id": template_id or "",
                "intent_tag": intent_tag or "",
                "msg_len": len(content),
            },
            fire_and_forget=True,
        )
        record_chat(
            customer_id=cid,
            channel=CHANNEL_MESSENGER,
            direction="outgoing",
            content=content,
            content_lang=lang,
            ai_generated=ai_generated,
            template_id=template_id,
            worker_id=worker_id or None,
            device_id=device_id,
            meta={
                "ai_decision": ai_decision,
                "intent_tag": intent_tag or "",
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] messenger_outgoing push failed: %s", exc)
    return cid


def sync_wa_referral_sent(
    device_id: str,
    peer_name: str,
    *,
    channel: str,
    content: Optional[str] = None,
    content_lang: Optional[str] = None,
    intent_tag: Optional[str] = None,
) -> Optional[str]:
    """引流话术发出 (WhatsApp / LINE / Telegram). channel = 'whatsapp'/'line'/'telegram'.

    刻意**不**升级 status — wa_referral 是"我发了话术", 客户没必跟过去;
    真正交接给人工要看 sync_handoff_to_line. 只记 event, 不写 chat
    (chat 已由 sync_messenger_outgoing 写过).
    """
    if not device_id or not peer_name:
        return None
    cid = _ensure_customer(device_id, peer_name)
    if not cid:
        return None
    try:
        from src.host.central_push_client import record_event
        record_event(
            customer_id=cid,
            event_type="wa_referral_sent",
            worker_id=_safe_worker_id(),
            device_id=device_id,
            meta={
                "channel": channel,
                "intent_tag": intent_tag or "",
                "content_lang": content_lang or "",
                "msg_len": len(content) if content else 0,
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] wa_referral push failed: %s", exc)
    return cid


def _push_priority(customer_id: str, priority_tag: str) -> None:
    """Phase-4: 通过 HTTP 调主控更新 priority_tag (worker 不直连 PG)."""
    if not customer_id or priority_tag not in ("high", "medium", "low"):
        return
    try:
        from src.host.central_push_client import _http_post_json
        _http_post_json(
            f"/cluster/customers/{customer_id}/priority",
            {"priority_tag": priority_tag},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] priority push failed: %s", exc)


def sync_handoff_to_line(
    device_id: str,
    peer_name: str,
    *,
    ai_summary: str,
    from_stage: str = "messenger",
    to_stage: str = "line",
    meta: Optional[Dict[str, Any]] = None,
    receiver_account_key: str = "",
    persona_key: str = "",
    snippet_sent: str = "",
    conversation_snapshot: Optional[List[Dict[str, Any]]] = None,
    write_lead_handoff: bool = True,
) -> Optional[str]:
    """messenger → line (或类似) 真正发起人机交接 handoff.

    升级客户 status='in_line'. 调 push_client.initiate_handoff (sync,
    因为 handoff_id 业务层要留档).

    ai_summary 强烈建议传 — 人工接管时第一眼看到这一段决定接不接.
    PR-2 的 worker 端 ai_summary 是简化拼接 (persona + last_in/out),
    L3 dashboard 实时拉时再补 LLM 总结.

    Phase-2: 双写 lead_handoffs SQLite (本机) — 让 SPA 引流后台 inbox
    也能看到 L2 数据. write_lead_handoff=False 时只写 PG 不写 SQLite
    (单独测试场景用).

    返回 PG 端 handoff_id (sync 调用; None 表示 push 失败 / 早退).
    """
    if not device_id or not peer_name or not ai_summary:
        return None
    cid = _ensure_customer(
        device_id, peer_name, status=STATUS_IN_LINE,
    )
    if not cid:
        return None

    pg_handoff_id: Optional[str] = None
    try:
        from src.host.central_push_client import initiate_handoff
        pg_handoff_id = initiate_handoff(
            customer_id=cid,
            from_stage=from_stage,
            to_stage=to_stage,
            initiating_worker_id=_safe_worker_id(),
            initiating_device_id=device_id,
            ai_summary=ai_summary,
            meta=meta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] PG handoff initiate failed: %s", exc)

    # Phase-4: handoff = 高意向客户, 立刻标 high priority
    _push_priority(cid, "high")

    # Phase-2: 双写 lead_handoffs (SQLite). 失败不影响 PG 那边.
    if write_lead_handoff:
        try:
            from src.host.lead_mesh.handoff import create_handoff
            create_handoff(
                canonical_id=cid or _build_canonical_id(device_id, peer_name),
                source_agent=_safe_worker_id() or "customer_sync_bridge",
                source_device=device_id,
                channel=to_stage,
                receiver_account_key=receiver_account_key,
                persona_key=persona_key,
                conversation_snapshot=conversation_snapshot or [],
                snippet_sent=snippet_sent or ai_summary[:200],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[customer_sync] SQLite handoff create failed: %s", exc)

    return pg_handoff_id


def build_simple_summary(
    *,
    persona_key: Optional[str] = None,
    intent_tag: Optional[str] = None,
    last_incoming: Optional[str] = None,
    last_outgoing: Optional[str] = None,
    max_snippet: int = 80,
) -> str:
    """简易 ai_summary 拼接 (PR-2 不调 LLM, L3 dashboard 拉时再补).

    格式: 'persona=hostess_jp | intent=referral | last_in: ... | last_out: ...'
    """
    parts = []
    if persona_key:
        parts.append(f"persona={persona_key}")
    if intent_tag:
        parts.append(f"intent={intent_tag}")
    if last_incoming:
        parts.append(f"last_in: {last_incoming[:max_snippet]}")
    if last_outgoing:
        parts.append(f"last_out: {last_outgoing[:max_snippet]}")
    return " | ".join(parts) or "(no context)"
