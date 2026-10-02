# -*- coding: utf-8 -*-
"""AI 接回时补答「接管期间客户没人回的那句」（2026-10-02，173 @anlhe 实测）。

人工接管期间（manual）入站不拟稿；接回（takeover_rearm 自动接回 / 坐席下拉切回）只
改档位，已经过去的入站不会再触发拟稿——客户最后一句话没人回，要等他再发一条 AI 才
开口（现场体感＝「手动发一条 AI 就死了」）。

接回那一刻：会话最后一条消息是客户入站、且之后没有任何出站 → 用原拟稿产线
（``app.state.auto_draft_cb``，与复班补觉同一回调）按**当前档位**补拟一稿：全自动由
AutosendWorker 照常投递，半自动进待审。``skip_companion_yield=True``：这条消息当时 A 线
因 manual 已让位，接回后再让位＝无人回。

只做私聊（群拟稿闸在回调里也会再拦一次）、只补 ``MAX_AGE_SEC`` 内的入站（隔天的旧话
不追答）、会话已有 pending 稿不补（防双稿）。任何异常吞掉，不影响切档主链。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MAX_AGE_SEC = 24 * 3600.0


def catchup_on_resume(
    app_state: Any, store: Any, conversation_id: str, *,
    mode: str, by: str = "", now: Optional[float] = None,
) -> Dict[str, Any]:
    """接回后补拟一稿。返回 ``{dispatched, reason}``（reason 供日志/回包排查）。"""
    out: Dict[str, Any] = {"dispatched": False, "reason": ""}
    cid = str(conversation_id or "").strip()
    if not cid or store is None or str(mode or "") in ("", "manual"):
        out["reason"] = "not_applicable"
        return out
    cb = getattr(app_state, "auto_draft_cb", None)
    if not callable(cb):
        out["reason"] = "no_draft_cb"
        return out
    try:
        conv = store.get_conversation(cid) or {}
        if str(conv.get("chat_type") or "") in ("group", "channel", "supergroup"):
            out["reason"] = "group"
            return out
        try:
            msgs = store.list_recent_messages(cid, limit=5, include_deleted=False)
        except TypeError:
            msgs = store.list_recent_messages(cid, limit=5)
        if not msgs:
            out["reason"] = "no_messages"
            return out
        last = msgs[-1]
        if str(last.get("direction") or "") != "in":
            out["reason"] = "last_is_outbound"
            return out
        text = str(last.get("text") or "").strip()
        if not text:
            out["reason"] = "no_text"
            return out
        ts_now = float(now or time.time())
        if ts_now - float(last.get("ts") or 0.0) > MAX_AGE_SEC:
            out["reason"] = "stale"
            return out
        if hasattr(store, "conversations_with_pending_drafts") and \
                cid in store.conversations_with_pending_drafts([cid]):
            out["reason"] = "has_pending_draft"
            return out
        parts = cid.split(":", 2)
        conv_arg = {
            "conversation_id": cid,
            "platform": str(conv.get("platform") or (parts[0] if len(parts) == 3 else "")),
            "account_id": str(conv.get("account_id")
                              or (parts[1] if len(parts) == 3 else "") or "default"),
            "chat_key": str(conv.get("chat_key") or (parts[2] if len(parts) == 3 else "")),
        }
        try:
            from src.inbox.draft_trigger import note as _trig_note
            _trig_note(cid, "takeover_rearm")
        except Exception:
            pass
        cb(conv_arg, text, skip_companion_yield=True)
        out["dispatched"] = True
        out["reason"] = "dispatched"
        logger.info("[draft] regen conv=%s reason=takeover_rearm by=%s（接回补答客户未回的最后一句）",
                    cid, by or "-")
    except Exception:
        logger.debug("[resume_catchup] 补答失败 cid=%s（忽略）", cid, exc_info=True)
        out["reason"] = "error"
    return out


__all__ = ["MAX_AGE_SEC", "catchup_on_resume"]
