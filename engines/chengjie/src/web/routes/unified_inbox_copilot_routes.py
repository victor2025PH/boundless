"""统一收件箱——客户互动积分 / 坐席 AI 副驾路由域（巨石拆分 slice 19）。

把两段连续且共享依赖面的子域，从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_copilot_routes(app, *, api_auth)``，由主 register 在**原位置**顺序调用：

- Phase 41 客户互动积分与成就：``contact/{id}/engagement`` (GET/POST)
- Phase 42 坐席 AI 副驾（打字辅助）：``conv/{id}/copilot-prefill`` + ``conv/{id}/reply-suggest``

（Phase 40 剧本话题引擎已于 2026-08 整体下线：script-suggestions + script-topics CRUD 路由、
``src/inbox/conversation_script.py`` 引擎与 ``script_topics`` 表一并移除。）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + copilot 链路专项断言）。

依赖全部朝下：context Copilot 族（slice 4 已成模块：_build_copilot_context /
_maybe_polish_copilot / _record_copilot_impression_if_prefill）、auth._agent_from_request、
services 存储；积分/副驾引擎均为 handler 内局部 import。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, Request

from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_context import (
    _build_copilot_context,
    _maybe_polish_copilot,
    _record_copilot_impression_if_prefill,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_copilot_routes(app, *, api_auth) -> None:
    """挂载互动积分 / AI 副驾端点（engagement、copilot-prefill、reply-suggest）。"""

    # ─── Phase 41: 客户互动积分与成就 ───────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/engagement")
    async def api_contact_engagement_get(contact_id: str, request: Request):
        """CC1:读取客户互动积分（无则返回空）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "engagement": None}
        data = store.get_contact_engagement(contact_id)
        if data is None:
            return {"ok": True, "contact_id": contact_id, "engagement": None, "computed": False}
        from src.inbox.engagement_scorer import _ACHIEVEMENT_DEFS, EngagementScorer
        level_name, _ = EngagementScorer._level_for(int(data.get("points") or 0))
        ach_details = [
            {**_ACHIEVEMENT_DEFS.get(aid, {"name": aid, "icon": "🏅", "desc": ""}),
             "id": aid, "unlocked": True}
            for aid in (data.get("achievements") or [])
        ]
        for aid, defn in _ACHIEVEMENT_DEFS.items():
            if aid not in (data.get("achievements") or []):
                ach_details.append({**defn, "id": aid, "unlocked": False})
        return {
            "ok": True,
            "contact_id": contact_id,
            "computed": True,
            "engagement": {
                **data,
                "level_name": level_name,
                "achievement_details": ach_details,
                "is_vip": int(data.get("points") or 0) >= 600,
            },
        }

    @app.post("/api/workspace/contact/{contact_id}/engagement")
    async def api_contact_engagement_compute(contact_id: str, request: Request, _=Depends(api_auth)):
        """CC1:重新计算并存储互动积分。"""
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        result = store.compute_and_store_engagement(contact_id)
        return {"ok": True, "contact_id": contact_id, "engagement": result}

    # ─── Phase 42: 坐席 AI 副驾（打字辅助） ─────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/copilot-prefill")
    async def api_conv_copilot_prefill(
        conversation_id: str,
        request: Request,
        trigger: str = "open",
        workflow_text: str = "",
        workflow_chain_name: str = "",
        workflow_step: int = 0,
        mention_body: str = "",
        mention_from: str = "",
        polish: bool = True,
    ):
        """P49/P52：事件驱动 Copilot 预填（可选 LLM 润色）。"""
        api_auth(request)
        store = _inbox_store(request)
        ctx = _build_copilot_context(
            request, conversation_id, store,
            trigger=trigger.strip(),
            workflow_text=workflow_text,
            workflow_chain_name=workflow_chain_name,
            workflow_step=workflow_step,
            mention_body=mention_body,
            mention_from=mention_from,
        )
        last_customer = ""
        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 20""",
                    (conversation_id,),
                ).fetchall()
                for r in rows:
                    if r["direction"] in ("in", "inbound") and r["text"]:
                        last_customer = str(r["text"])
                        break
            except Exception:
                pass
        templates: List[Dict[str, Any]] = []
        if store is not None:
            try:
                templates = store.list_templates(limit=50, active_only=True)
            except Exception:
                pass
        from src.inbox.reply_copilot import ReplyCopilot
        result = ReplyCopilot().suggest(
            partial_text="",
            last_customer_msg=last_customer,
            stage=ctx["stage"],
            templates=templates,
            context=ctx,
            limit=4,
        )
        payload = {
            "ok": True,
            "conversation_id": conversation_id,
            "trigger": ctx.get("trigger") or trigger,
            "stage": ctx["stage"],
            **result,
            "context": ctx,
        }
        payload = await _maybe_polish_copilot(
            request, payload,
            conversation_id=conversation_id,
            partial_text="",
            last_customer_msg=last_customer,
            polish_requested=bool(polish),
        )
        agent_id, _ = _agent_from_request(request)
        _record_copilot_impression_if_prefill(
            store, conversation_id, agent_id, payload, partial_text="",
        )
        return payload

    @app.post("/api/workspace/conv/{conversation_id}/reply-suggest")
    async def api_conv_reply_suggest(conversation_id: str, request: Request, _=Depends(api_auth)):
        """CC1/P49：实时回复补全（规则 + 模板 + 阶段/工作链/@mention 联动）。"""
        body = await request.json()
        partial = str(body.get("partial") or body.get("text") or "")
        recent = body.get("messages") if isinstance(body.get("messages"), list) else []

        last_customer = ""
        for m in reversed(recent):
            if isinstance(m, dict) and m.get("direction") in ("in", "inbound") and m.get("text"):
                last_customer = str(m["text"])
                break

        templates: List[Dict[str, Any]] = []
        store = _inbox_store(request)
        ctx = _build_copilot_context(
            request, conversation_id, store,
            trigger=str(body.get("trigger") or ""),
            workflow_text=str(body.get("workflow_text") or ""),
            workflow_chain_name=str(body.get("workflow_chain_name") or ""),
            workflow_step=int(body.get("workflow_step") or 0),
            mention_body=str(body.get("mention_body") or ""),
            mention_from=str(body.get("mention_from") or ""),
        ) if store is not None else {}

        if store is not None:
            try:
                templates = store.list_templates(limit=50, active_only=True)
            except Exception:
                pass

        from src.inbox.reply_copilot import ReplyCopilot
        result = ReplyCopilot().suggest(
            partial_text=partial,
            last_customer_msg=last_customer,
            stage=ctx.get("stage") or "initial",
            recent_messages=recent,
            templates=templates,
            context=ctx,
            limit=4 if not partial else 3,
        )
        polish_req = bool(body.get("polish"))
        payload = {
            "ok": True,
            "conversation_id": conversation_id,
            "partial": partial,
            "stage": ctx.get("stage") or "initial",
            **result,
            "context": ctx,
        }
        if polish_req and not partial.strip():
            payload = await _maybe_polish_copilot(
                request, payload,
                conversation_id=conversation_id,
                partial_text=partial,
                last_customer_msg=last_customer,
                polish_requested=True,
            )
        else:
            payload["polished"] = False
        if not partial.strip() and store is not None:
            agent_id, _ = _agent_from_request(request)
            _record_copilot_impression_if_prefill(
                store, conversation_id, agent_id, payload, partial_text=partial,
            )
        return payload
