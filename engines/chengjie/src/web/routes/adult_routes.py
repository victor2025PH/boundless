# -*- coding: utf-8 -*-
"""成人内容分级 · 工作台「一键人设口吻软回应」取词端点（Q-15 #271 D，2026-09-10）。

- ``GET /api/unified-inbox/adult/soft-reply?cid=``  → ``{ok, cid, text, lang, level, hit, policy}``
  只**取词不发送**：前端把 text 填进输入框走既有 ``sendMsg()`` / 发送路由（人工出站 → 自动摘
  「需人工」标 + 解除 risk_hold，与坐席手打一条完全同路）。语言：最近一条入站嗅探 → 会话语言 → en。
  级别 / 命中词来自 ``handoff_meta.reason``（``adult:<level>:<hit>``）。
- ``GET /api/unified-inbox/adult/grade?text=``      → ``grade()`` 直读（自检 / 排障；不落库）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import HTTPException, Request

from src.inbox import adult_grader

logger = logging.getLogger(__name__)


def _last_inbound_text(store: Any, cid: str) -> str:
    try:
        rows = store.list_recent_messages(cid, limit=12) or []
    except Exception:
        return ""
    for m in reversed(rows):
        try:
            if str(m.get("direction") or "") == "in" and str(m.get("text") or "").strip():
                return str(m.get("text") or "")
        except Exception:
            continue
    return ""


def register_adult_routes(app, *, api_auth) -> None:
    def _store(request: Request):
        st = getattr(request.app.state, "inbox_store", None)
        if st is None:
            raise HTTPException(503, "inbox store not ready")
        return st

    def _cfg(request: Request) -> Dict[str, Any]:
        try:
            cm = getattr(request.app.state, "config_manager", None)
            c = getattr(cm, "config", None)
            return c if isinstance(c, dict) else {}
        except Exception:
            return {}

    @app.get("/api/unified-inbox/adult/soft-reply")
    async def api_adult_soft_reply(request: Request, cid: str = ""):
        api_auth(request)
        cid = str(cid or "").strip()
        if not cid:
            raise HTTPException(400, "cid required")
        store = _store(request)
        cfg = _cfg(request)
        try:
            conv = dict(store.get_conversation(cid) or {})
        except Exception:
            conv = {}
        if not conv:
            parts = cid.split(":", 2)
            conv = {"platform": parts[0] if parts else "", "account_id": parts[1] if len(parts) > 1 else "",
                    "chat_key": parts[2] if len(parts) > 2 else ""}
        conv["conversation_id"] = cid
        peer_text = _last_inbound_text(store, cid)
        lang = adult_grader.sniff_lang(peer_text, str(conv.get("language") or conv.get("peer_lang") or ""))
        persona = adult_grader.resolve_persona(conv, cfg)
        text = adult_grader.soft_reply_for(persona, lang, cid=cid)
        hm: Dict[str, Any] = {}
        try:
            if hasattr(store, "get_handoff_meta"):
                hm = dict(store.get_handoff_meta(cid) or {})
        except Exception:
            hm = {}
        parts = adult_grader.card_label_parts(hm.get("reason"))
        policy, policy_src = adult_grader.adult_policy_of(persona, cfg)
        logger.info("[adult] soft_reply_pick conv=%s lang=%s level=%s policy=%s by=agent",
                    cid, lang, parts.get("level") or "-", policy)
        return {"ok": True, "cid": cid, "text": text, "lang": lang,
                "level": parts.get("level") or "", "hit": parts.get("hit") or "",
                "policy": policy, "policy_source": policy_src}

    @app.get("/api/unified-inbox/adult/grade")
    async def api_adult_grade(request: Request, text: str = "", lang: str = ""):
        api_auth(request)
        g = adult_grader.grade(str(text or ""), str(lang or "") or None)
        return {"ok": True, **g}


__all__ = ["register_adult_routes"]
