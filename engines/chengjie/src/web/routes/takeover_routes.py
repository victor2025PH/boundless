# -*- coding: utf-8 -*-
"""会话级接管 API（``/api/takeover/*``，驾驶舱 P0 2026-08-13）。

四个端点服务「接管 → 人工聊 → 交还」闭环（核心语义在 ``src/inbox/takeover.py``）：

  GET  /api/takeover/active —— 全部进行中接管 + 观测统计（工作台徽章/驾驶舱/
       桌面壳计时条轮询共用；前端 404-探测 → 旧后端永久隐藏按钮，零噪音）。
  GET  /api/takeover/status —— 单会话接管状态（会话头按钮渲染）。
  POST /api/takeover/start  —— 接管（viewer 只读拒绝）：档位切 manual + 取消
       在途草稿 + 打标签，一个动作打包。
  POST /api/takeover/end    —— 交还：恢复接管前档位 + 摘标签 + 记时长。

响应 detail 全走 ``tr()`` 请求级 i18n（0 硬编码中文，棘轮门禁口径）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.takeover_routes")

_ROLE_VIEWER = "viewer"


def register_takeover_routes(app, api_auth, config_manager=None):
    """挂载会话接管 API。``api_auth``＝登录校验依赖。"""

    def _full_cfg(request: Request) -> Dict[str, Any]:
        cm = config_manager
        if cm is None:
            cm = getattr(request.app.state, "config_manager", None)
        cfg = getattr(cm, "config", None) if cm is not None else None
        return cfg if isinstance(cfg, dict) else {}

    def _store(request: Request) -> Any:
        return getattr(request.app.state, "inbox_store", None)

    def _require_write(request: Request) -> None:
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.tko.readonly"))

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web-admin")
        except Exception:
            return "web-admin"

    async def _body_cid(request: Request) -> str:
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        cid = str(body.get("conversation_id") or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.tko.conv_required"))
        return cid

    @app.get("/api/takeover/active")
    async def api_takeover_active(request: Request, _=Depends(api_auth)):
        """全部进行中接管 + 统计（只读）。"""
        from src.inbox import takeover as tk
        return {"ok": True, "active": tk.list_active(),
                "stats": tk.takeover_stats()}

    @app.get("/api/takeover/status")
    async def api_takeover_status(
            request: Request, conversation_id: str = "", _=Depends(api_auth)):
        """单会话接管状态（无接管 → entry=None）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.tko.conv_required"))
        from src.inbox import takeover as tk
        return {"ok": True, "conversation_id": cid,
                "entry": tk.get_takeover(cid)}

    @app.post("/api/takeover/start")
    async def api_takeover_start(request: Request, _=Depends(api_auth)):
        """接管会话。body: ``{conversation_id}``。幂等：已接管返回现状。"""
        _require_write(request)
        cid = await _body_cid(request)
        store = _store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.tko.store_unready"))
        from src.inbox import takeover as tk
        res = tk.start_takeover(store, cid, by=_actor(request))
        if not res.get("ok"):
            raise HTTPException(
                503, tr(request, "err.tko.start_failed",
                        reason=str(res.get("reason") or "")))
        logger.info("[takeover] %s start %s (already=%s)",
                    _actor(request), cid, bool(res.get("already")))
        return {"ok": True, "already": bool(res.get("already")),
                "entry": res.get("entry")}

    @app.post("/api/takeover/end")
    async def api_takeover_end(request: Request, _=Depends(api_auth)):
        """交还会话。body: ``{conversation_id}``。"""
        _require_write(request)
        cid = await _body_cid(request)
        store = _store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.tko.store_unready"))
        from src.inbox import takeover as tk
        res = tk.end_takeover(store, cid, by=_actor(request),
                              config=_full_cfg(request))
        if not res.get("ok"):
            reason = str(res.get("reason") or "")
            if reason == "not_active":
                raise HTTPException(409, tr(request, "err.tko.not_active"))
            raise HTTPException(
                503, tr(request, "err.tko.end_failed", reason=reason))
        logger.info("[takeover] %s end %s duration=%.0fs",
                    _actor(request), cid, float(res.get("duration_sec") or 0))
        return {"ok": True, "duration_sec": res.get("duration_sec"),
                "restored_mode": res.get("restored_mode")}
