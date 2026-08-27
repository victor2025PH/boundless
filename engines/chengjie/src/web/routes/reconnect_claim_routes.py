# -*- coding: utf-8 -*-
"""回连认领路由（账号资产保全 P1，2026-08-19）。

- GET  /api/admin/asset/reconnect/inventory   — 某账号「加回清单」+ 句柄覆盖率读数
- GET  /api/admin/asset/reconnect/candidates  — 新账号 × 旧账号 认领候选（分层置信）
- POST /api/admin/asset/reconnect/claim       — 单对认领（身份 link + 记忆合流 + contacts 并档）
- POST /api/admin/asset/reconnect/auto-claim  — 批量认领高置信候选（exact_peer/username；支持 dry_run）
- GET  /api/admin/asset/reconnect/claims      — 认领台账（审计留痕）

匹配 / 认领 / 台账逻辑全在 ``src.contacts.reconnect_claim``（单一事实源），
这里是薄路由。与 P0 封号管道松耦合：``old_account_id`` 显式传参，封号状态
落地后前端可把被封账号直接填进来。
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_AUTO_CLAIM_BATCH_CAP = 200


def _operator(request: Request) -> str:
    try:
        sess = getattr(request, "session", None) or {}
        return str(sess.get("username") or sess.get("agent_id") or "admin")
    except Exception:
        return "admin"


def _deps(request: Request):
    """(inbox_store, cpi, episodic, gateway, contacts_store) —— 全部可缺席。"""
    app_ = request.app
    inbox = getattr(app_.state, "inbox_store", None)
    cpi = epi = None
    try:
        from src.web.web_context import resolve_skill_manager
        sm = resolve_skill_manager(
            getattr(app_.state, "telegram_client", None), app_)
        cpi = getattr(sm, "_cpi", None) if sm else None
        epi = getattr(sm, "_episodic_store", None) if sm else None
    except Exception:
        logger.debug("reconnect routes: skill_manager unavailable", exc_info=True)
    gw = cs = None
    try:
        from src.web.routes.unified_inbox_services import (
            _contacts_gateway, _contacts_store,
        )
        gw, cs = _contacts_gateway(request), _contacts_store(request)
    except Exception:
        logger.debug("reconnect routes: contacts unavailable", exc_info=True)
    return inbox, cpi, epi, gw, cs


def _require(request: Request, value: str, field: str) -> str:
    v = str(value or "").strip()
    if not v:
        raise HTTPException(400, tr(request, "err.ws.field_required", field=field))
    return v


def _slim(row) -> dict:
    return {
        "conversation_id": row.get("conversation_id"),
        "chat_key": row.get("chat_key"),
        "display_name": row.get("display_name"),
        "username": row.get("username"),
        "phone": row.get("phone"),
        "language": row.get("language"),
        "last_ts": row.get("last_ts"),
        "first_seen": row.get("first_seen"),
        "contact_id": row.get("contact_id") or "",
    }


def register_reconnect_claim_routes(app, *, api_auth) -> None:
    """挂载回连认领端点（管理员 API 鉴权，与联系人合并同责任边界）。"""

    @app.get("/api/admin/asset/reconnect/inventory")
    async def api_reconnect_inventory(
        request: Request, platform: str = "", account_id: str = "",
        limit: int = 500,
    ):
        api_auth(request)
        from src.contacts import reconnect_claim as rc
        platform = _require(request, platform, "platform")
        account_id = _require(request, account_id, "account_id")
        inbox, *_ = _deps(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        rows = rc.fetch_account_conversations(inbox, platform, account_id)
        limit = max(1, min(2000, int(limit or 500)))
        return {
            "ok": True,
            "platform": platform,
            "account_id": account_id,
            "coverage": rc.handle_coverage(rows),
            "rows": [_slim(r) for r in rows[:limit]],
            "scanned": len(rows),
        }

    @app.get("/api/admin/asset/reconnect/candidates")
    async def api_reconnect_candidates(
        request: Request, platform: str = "",
        new_account_id: str = "", old_account_id: str = "",
    ):
        api_auth(request)
        from src.contacts import reconnect_claim as rc
        platform = _require(request, platform, "platform")
        new_account_id = _require(request, new_account_id, "new_account_id")
        old_account_id = _require(request, old_account_id, "old_account_id")
        if new_account_id == old_account_id:
            raise HTTPException(
                400, tr(request, "err.asset.same_account"))
        inbox, cpi, *_ = _deps(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        old_rows = rc.fetch_account_conversations(inbox, platform, old_account_id)
        new_rows = rc.fetch_account_conversations(inbox, platform, new_account_id)
        cands = rc.match_candidates(new_rows, rc.build_match_index(old_rows), cpi=cpi)
        return {
            "ok": True,
            "candidates": cands,
            "auto_claim_min": rc.AUTO_CLAIM_MIN,
            "old_scanned": len(old_rows),
            "new_scanned": len(new_rows),
        }

    @app.post("/api/admin/asset/reconnect/claim")
    async def api_reconnect_claim(request: Request):
        api_auth(request)
        from src.contacts import reconnect_claim as rc
        body = await request.json()
        old_cid = _require(
            request, body.get("old_conversation_id"), "old_conversation_id")
        new_cid = _require(
            request, body.get("new_conversation_id"), "new_conversation_id")
        inbox, cpi, epi, gw, cs = _deps(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        for cid in (old_cid, new_cid):
            try:
                found = inbox.get_conversation(cid)
            except Exception:
                found = None
            if not found:
                raise HTTPException(
                    404, tr(request, "err.asset.conv_not_found", cid=cid))
        res = rc.claim(
            inbox_store=inbox, cpi=cpi, episodic_store=epi,
            old_conversation_id=old_cid, new_conversation_id=new_cid,
            contacts_store=cs, gateway=gw,
            operator=_operator(request),
            matched_on=str(body.get("matched_on") or "manual"),
        )
        if not res.get("ok"):
            raise HTTPException(
                409, tr(request, "err.asset.claim_failed",
                        reason=str(res.get("error") or "unknown")))
        return {"ok": True, **res}

    @app.post("/api/admin/asset/reconnect/auto-claim")
    async def api_reconnect_auto_claim(request: Request):
        api_auth(request)
        from src.contacts import reconnect_claim as rc
        body = await request.json()
        platform = _require(request, body.get("platform"), "platform")
        new_account_id = _require(
            request, body.get("new_account_id"), "new_account_id")
        old_account_id = _require(
            request, body.get("old_account_id"), "old_account_id")
        if new_account_id == old_account_id:
            raise HTTPException(400, tr(request, "err.asset.same_account"))
        dry_run = bool(body.get("dry_run"))
        inbox, cpi, epi, gw, cs = _deps(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        old_rows = rc.fetch_account_conversations(inbox, platform, old_account_id)
        new_rows = rc.fetch_account_conversations(inbox, platform, new_account_id)
        cands = rc.match_candidates(new_rows, rc.build_match_index(old_rows), cpi=cpi)
        eligible = [
            c for c in cands
            if float(c.get("confidence") or 0) >= rc.AUTO_CLAIM_MIN
            and c.get("already_linked") is not True
        ][:_AUTO_CLAIM_BATCH_CAP]
        if dry_run:
            return {"ok": True, "dry_run": True, "eligible": eligible,
                    "total_candidates": len(cands)}
        operator = _operator(request)
        items = []
        claimed = 0
        for c in eligible:
            res = rc.claim(
                inbox_store=inbox, cpi=cpi, episodic_store=epi,
                old_conversation_id=str(c["old_conversation_id"]),
                new_conversation_id=str(c["new_conversation_id"]),
                contacts_store=cs, gateway=gw, operator=operator,
                matched_on=",".join(c.get("matched_on") or []) or "auto",
            )
            if res.get("ok") and not res.get("already_linked"):
                claimed += 1
            items.append(res)
        return {
            "ok": True, "dry_run": False,
            "claimed": claimed, "attempted": len(items),
            "total_candidates": len(cands), "items": items,
        }

    @app.get("/api/admin/asset/reconnect/claims")
    async def api_reconnect_claims(request: Request, limit: int = 100):
        api_auth(request)
        from src.contacts import reconnect_claim as rc
        return {"ok": True, "claims": rc.get_ledger().all(limit=limit)}
