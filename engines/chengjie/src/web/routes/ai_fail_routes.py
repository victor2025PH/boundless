# -*- coding: utf-8 -*-
"""「AI 本轮未生成」灰标的读 / 清端点（Q-14 #262 B，2026-09-09）。

- ``GET  /api/unified-inbox/ai-last-fail?cid=``       单会话灰标（无 → ``{ok, fail: null}``）
- ``GET  /api/unified-inbox/ai-last-fail/all``         全部灰标 ``{cid: rec}``（会话列表批量标记）
- ``POST /api/unified-inbox/ai-last-fail/clear``       body ``{cid}``：坐席点「重试起草」成功后清标
  （重生成走既有 ``POST /api/drafts/{draft_id}/regenerate``，本模块不重复造起草链）

灰标本身由起草侧写（``autodraft_helpers`` → ``ai_fail_marker.consume_and_mark``），
下一次该会话 AI 成功自动清；这里只是把它暴露给工作台。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import HTTPException, Request

from src.inbox import ai_fail_marker

logger = logging.getLogger(__name__)


def register_ai_fail_routes(app, *, api_auth) -> None:
    def _store(request: Request):
        st = getattr(request.app.state, "inbox_store", None)
        if st is None:
            raise HTTPException(503, "inbox store not ready")
        return st

    @app.get("/api/unified-inbox/ai-last-fail")
    async def api_ai_last_fail(request: Request, cid: str = ""):
        api_auth(request)
        cid = str(cid or "").strip()
        if not cid:
            raise HTTPException(400, "cid required")
        rec = ai_fail_marker.get(_store(request), cid)
        return {"ok": True, "cid": cid, "fail": rec}

    @app.get("/api/unified-inbox/ai-last-fail/all")
    async def api_ai_last_fail_all(request: Request):
        api_auth(request)
        marks = ai_fail_marker.all_marks(_store(request))
        return {"ok": True, "count": len(marks), "fails": marks}

    @app.post("/api/unified-inbox/ai-last-fail/clear")
    async def api_ai_last_fail_clear(request: Request):
        api_auth(request)
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        cid = str((body or {}).get("cid") or "").strip()
        if not cid:
            raise HTTPException(400, "cid required")
        ok = ai_fail_marker.clear(_store(request), cid)
        logger.info("[ai_fail] clear conv=%s by=agent ok=%s", cid, ok)
        return {"ok": bool(ok), "cid": cid}


__all__ = ["register_ai_fail_routes"]
