# -*- coding: utf-8 -*-
"""驾驶舱 API（``/api/cockpit/*``，cockpit P1 2026-08-13）。

单端点服务驾驶舱页 30s 轮询：介入优先级队列（四源聚合，语义在
``src/inbox/cockpit.py``）+ 接管统计。KPI 与账号健康刻意**不在此重造**——
前端另拉既有 ``/api/workspace/dashboard``（等待/超时口径）与
``/api/accounts/fleet-health``（机群灯），单一口径不分叉。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.cockpit_routes")


def register_cockpit_routes(app, api_auth, config_manager=None):
    """挂载驾驶舱 API。``api_auth``＝登录校验依赖。"""

    def _cfg(request: Request):
        cm = config_manager
        if cm is None:
            cm = getattr(request.app.state, "config_manager", None)
        cfg = getattr(cm, "config", None) if cm is not None else None
        return cfg if isinstance(cfg, dict) else {}

    @app.get("/api/cockpit/overview")
    async def api_cockpit_overview(
            request: Request, force: int = 0, _=Depends(api_auth)):
        """介入队列 + 接管统计（30s TTL 缓存；``force=1`` 绕过重算）。

        ``caps`` 是前端特性探测位（P2 起）：模板热更新先于重启上线的中间态里，
        旧后端没有 resolve 端点 → 前端见不到 caps 就不渲染「已处理」按钮，
        与 reply-settings 守卫卡的 feat 探测同一模式。
        """
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.tko.store_unready"))
        from src.inbox.cockpit import cockpit_snapshot
        snap = cockpit_snapshot(store, _cfg(request), force=bool(force))
        return {"ok": True, "caps": {"resolve": True, "snooze": True}, **snap}

    @app.post("/api/cockpit/resolve")
    async def api_cockpit_resolve(request: Request, _=Depends(api_auth)):
        """「已处理」＝摘掉会话的「需人工」标签（needs_human 卡片唯一清除出口）。

        标签是协议链打的、没有任何自动过期语义 → 没有这个出口，积压卡会永远
        占屏（上线首日实测 12～34 天陈尸）。幂等：标签已不在 → removed=false
        不再写。写后失效驾驶舱快照缓存，下一拉立即反映。
        """
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.tko.store_unready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = str((body or {}).get("conversation_id") or "").strip()
        if not cid:
            raise HTTPException(400, tr(
                request, "err.ws.field_required", field="conversation_id"))
        from src.integrations.protocol_autoreply import HANDOFF_TAG
        tags = list(store.get_conv_tags(cid) or [])
        removed = HANDOFF_TAG in tags
        if removed:
            store.set_conv_tags(cid, [t for t in tags if t != HANDOFF_TAG])
            from src.inbox.cockpit import invalidate_cache
            invalidate_cache()
            try:
                from src.inbox.cockpit_hold import note_cleared
                note_cleared(cid, "resolve")
            except Exception:
                logger.debug("[cockpit] 今日清掉记账失败（忽略）", exc_info=True)
            logger.info("[cockpit] resolve needs_human: %s", cid)
        return {"ok": True, "removed": removed}

    def _cid_or_400(request: Request, body: dict) -> str:
        cid = str((body or {}).get("conversation_id") or "").strip()
        if not cid:
            raise HTTPException(400, tr(
                request, "err.ws.field_required", field="conversation_id"))
        return cid

    @app.post("/api/cockpit/snooze")
    async def api_cockpit_snooze(request: Request, _=Depends(api_auth)):
        """「先不回」：客户在等的这一句先离开主列表。新的一句进来会自己回来。"""
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.tko.store_unready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = _cid_or_400(request, body if isinstance(body, dict) else {})
        from src.inbox.cockpit import collect_intervention_queue, invalidate_cache
        from src.inbox.cockpit_hold import item_fingerprint, remember_snooze
        q = collect_intervention_queue(store, _cfg(request))
        pool = list(q.get("items") or []) + list(q.get("snoozed") or [])
        it = next((i for i in pool if str(i.get("conversation_id") or "") == cid), None)
        if not it or it.get("kind") != "waiting":
            raise HTTPException(409, tr(request, "err.ck.not_waiting"))
        remember_snooze(cid, item_fingerprint(it))
        invalidate_cache()
        return {"ok": True}

    @app.post("/api/cockpit/unsnooze")
    async def api_cockpit_unsnooze(request: Request, _=Depends(api_auth)):
        """把「先不回」收回主列表。"""
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.tko.store_unready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = _cid_or_400(request, body if isinstance(body, dict) else {})
        from src.inbox.cockpit import invalidate_cache
        from src.inbox.cockpit_hold import forget_snooze
        forget_snooze(cid)
        invalidate_cache()
        return {"ok": True}

    @app.post("/api/cockpit/cleared")
    async def api_cockpit_cleared(request: Request, _=Depends(api_auth)):
        """页内发出一句之后记账。发送本身走统一收件箱，这里只记「今天清掉」。

        ``src=inbox``：完整对话里发送成功后也来这里记同一笔（how=reply）。只有发
        之前这个人在「客户在等」里才算，同一轮等待只算一次（判据见
        ``cockpit.inbox_reply_clears``）；发送失败前端不会来。会话放在
        ``reply_conversation_id`` 而不是 ``conversation_id``：模板热更新先于重启
        上线，没有这条判据的旧后端会回 400，而不是把收件箱里每一句都记成清掉。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if str(body.get("src") or "").strip() == "inbox":
            cid = str(body.get("reply_conversation_id") or "").strip()
            if not cid:
                raise HTTPException(400, tr(
                    request, "err.ws.field_required",
                    field="reply_conversation_id"))
            from src.inbox.cockpit import invalidate_cache, note_inbox_reply
            store = getattr(request.app.state, "inbox_store", None)
            if store is None or not note_inbox_reply(store, cid):
                return {"ok": True, "counted": False}
            invalidate_cache()
            from src.inbox.cockpit_hold import cleared_today
            return {"ok": True, "counted": True, "cleared_today": cleared_today()}
        cid = _cid_or_400(request, body)
        how = str(body.get("how") or "reply").strip()
        if how not in ("reply", "resolve", "handback"):
            how = "reply"
        from src.inbox.cockpit import invalidate_cache
        from src.inbox.cockpit_hold import cleared_today, note_cleared
        note_cleared(cid, how)
        invalidate_cache()
        return {"ok": True, "counted": True, "cleared_today": cleared_today()}
