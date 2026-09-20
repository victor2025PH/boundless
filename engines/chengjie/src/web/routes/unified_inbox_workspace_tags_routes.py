"""统一收件箱——标签体系 / 会话级标签·摘要·归档路由域（巨石拆分 slice 15）。

把"标签聚合(tags/tag-stats) + 预设标签库 CRUD(tag-library) + 会话级 AI 摘要 / 标签读写 /
归档(conv/{id}/summarize|tags|archive)"这一内聚子域，从 ``register_unified_inbox_routes``
巨型闭包中外移为 ``register_workspace_tags_routes(app, *, api_auth)``，由主 register 在
**原位置**顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下：services 存储/聊天助手、normalizer.store_message_to_obj；event_bus 为 handler
内局部 import（P28 标签/归档事件外发）。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.inbox.normalizer import store_message_to_obj
from src.web.routes.unified_inbox_services import (
    _contacts_gateway,
    _contacts_store,
    _get_chat_assistant_service,
    _inbox_store,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_workspace_tags_routes(app, *, api_auth) -> None:
    """挂载标签体系 / 会话级标签·摘要·归档端点（/api/workspace/tags|tag-stats|tag-library*、conv/{id}/summarize|tags|archive）。"""

    @app.get("/api/workspace/tags")
    async def api_workspace_tags(request: Request, limit: int = 100):
        """全部标签 + 使用计数 + 预设库颜色（标签自动补全/快筛/上色）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "tags": []}
        return {"ok": True, "tags": store.list_all_tags(limit=max(1, min(300, int(limit or 100))))}

    @app.get("/api/workspace/tag-stats")
    async def api_workspace_tag_stats(request: Request):
        """T2：会话级标签统计（count / unread / platforms），用于概览 strip。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "stats": []}
        try:
            stats = inbox.tag_stats()
        except Exception:
            logger.debug("tag-stats 失败（已忽略）", exc_info=True)
            stats = []
        return {"ok": True, "stats": stats}

    @app.get("/api/workspace/tag-library")
    async def api_workspace_tag_library_list(request: Request):
        """预设标签库（名称/颜色/排序）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "library": []}
        return {"ok": True, "library": gw.list_tag_library()}

    @app.post("/api/workspace/tag-library")
    async def api_workspace_tag_library_upsert(request: Request, _=Depends(api_auth)):
        """新增/更新预设标签：{tag, color?, sort_order?}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        tag = str(body.get("tag") or "").strip()
        if not tag:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="tag"))
        ok = gw.upsert_tag_library(
            tag, color=str(body.get("color") or ""),
            sort_order=int(body.get("sort_order") or 0),
        )
        return {"ok": ok, "library": gw.list_tag_library()}

    @app.delete("/api/workspace/tag-library/{tag}")
    async def api_workspace_tag_library_delete(
        tag: str, request: Request, _=Depends(api_auth),
    ):
        """从预设库删除一个标签（不影响已打在客户上的标签）。"""
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        return {"ok": gw.delete_tag_library(tag), "library": gw.list_tag_library()}

    @app.post("/api/workspace/tags/remove-from-all")
    async def api_workspace_tag_remove_from_all(
        request: Request, _=Depends(api_auth),
    ):
        """把某标签从**所有**会话上摘除（2026-08-23 标签治理闭环）。

        上方库删除刻意不动已打标签（注释钉死该语义），于是测试期乱打的
        「123/333」会永远挂在筛选条上（strip 按在用标签生成）——本端点是唯一的
        全量摘除入口。viewer 拒写（与 batch/tags 同口径）；ops_events 留审计。
        标签库条目**不动**：摘干净后是否删库条目由运营在管理器里另行决定。
        """
        try:
            role = str(request.session.get("role", "") or "")
        except Exception:
            role = ""
        if role == "viewer":
            raise HTTPException(403, tr(request, "err.perm.viewer_readonly"))
        body = await request.json()
        tag = str((body or {}).get("tag") or "").strip()
        if not tag:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="tag"))
        store = _inbox_store(request)
        if store is None or not hasattr(store, "remove_tag_from_all_conversations"):
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        removed = int(store.remove_tag_from_all_conversations(tag) or 0)
        try:
            actor = str(request.session.get("username", "") or "") or "api"
        except Exception:
            actor = "api"
        logger.info("[tags] 标签 %r 已从 %d 个会话摘除 by=%s", tag, removed, actor)
        try:
            from src.ops.ops_events import get_ops_event_store
            _oes = get_ops_event_store()
            if _oes is not None:
                _oes.record(
                    platform="workspace", account_id="", kind="tag_remove_all",
                    reason="ok", detail=f"tag={tag};removed={removed};by={actor}")
        except Exception:
            logger.debug("[tags] remove-from-all 审计落账失败（忽略）", exc_info=True)
        return {"ok": True, "tag": tag, "removed": removed}

    # ── T1: 会话级标签 + 归档 API ─────────────────────────────────────

    @app.post("/api/workspace/conv/{conversation_id}/summarize")
    async def api_conv_summarize(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """Phase 19：为会话生成 AI 摘要并持久化到 conversation_meta.summary。

        调用 ChatAssistantService.analyze（与 inbox/analyze 同服务），
        以会话最近30条消息作为上下文，生成一句话概括。结果写库后返回。
        """
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        msgs = store.list_recent_messages(conversation_id, limit=30)
        if not msgs:
            return {"ok": True, "summary": ""}
        msg_objs = [store_message_to_obj(r) for r in msgs]
        # 取最后一条入站文字为代表性文本
        last_in = next((m for m in reversed(msg_objs) if m.get("direction") == "in"
                        and m.get("text")), None)
        text = str((last_in or msg_objs[-1]).get("text") or "")
        try:
            svc = _get_chat_assistant_service(request)
            analysis = await svc.analyze(text=text, messages=msg_objs)
            summary = str(getattr(analysis, "summary", "") or "").strip()
            if not summary:
                # Fallback: truncate last user message as summary
                summary = text[:80] + ("…" if len(text) > 80 else "")
        except Exception:
            logger.debug("conv summarize AI 调用失败（已忽略）", exc_info=True)
            summary = text[:80] + ("…" if len(text) > 80 else "")
        store.save_conv_summary(conversation_id, summary)
        return {"ok": True, "summary": summary}

    @app.get("/api/workspace/conv/{conversation_id}/tags")
    async def api_conv_tags_get(conversation_id: str, request: Request):
        """T1：获取单个会话的标签列表。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "tags": []}
        return {"ok": True, "tags": store.get_conv_tags(conversation_id)}

    @app.put("/api/workspace/conv/{conversation_id}/tags")
    async def api_conv_tags_put(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """T1+P28：覆写会话标签列表，广播 conv_tagged 事件供 Webhook 外发。"""
        body = await request.json()
        tags = [str(t) for t in (body.get("tags") or []) if str(t).strip()]
        # Q-4（#267）：「作息外 · 到点重新拟稿」是读侧计算标签，前端整表回写时剥掉不落库
        try:
            from src.inbox.work_hours_gate import strip_off_hours_hold_tags
            tags = strip_off_hours_hold_tags(tags)
        except Exception:
            pass
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        # 实施74（实施69 P1-1）：摘掉「需人工」时元数据同清（防陈旧 tooltip 复活）。
        # Q-17（#277②）：会话头「我知道了（摘标）」/ 手摘标签走的就是本口——改为经
        # ``clear_needs_human(actor=agent_ack)``：元数据同清 + risk_hold 同步解除（Q-3 口径，此前
        # 本口漏了）+ 登记同类 30 分钟冷却 + 落 ``[needs_human] 摘标`` 日志。标签数组仍按 body 覆写。
        try:
            from src.integrations.protocol_autoreply import HANDOFF_TAG, clear_needs_human
            if HANDOFF_TAG not in tags and HANDOFF_TAG in (store.get_conv_tags(conversation_id) or []):
                if not clear_needs_human(store, conversation_id, actor="agent_ack") \
                        and hasattr(store, "set_handoff_meta"):
                    store.set_handoff_meta(conversation_id, None)
        except Exception:
            logger.debug("handoff_meta 清除失败（忽略）", exc_info=True)
        ok = store.set_conv_tags(conversation_id, tags)
        # P28：广播标签变更事件
        if ok:
            try:
                from src.integrations.shared.event_bus import get_event_bus
                import time as _t
                get_event_bus().publish("conv_tagged", {
                    "conversation_id": conversation_id,
                    "tags": tags,
                    "ts": _t.time(),
                })
            except Exception:
                pass
        return {"ok": ok, "tags": tags}

    @app.post("/api/workspace/conv/{conversation_id}/stop-contact/confirm")
    async def api_conv_stop_contact_confirm(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """Q-31 #317：会话头「已处理 · 归档并移除」——停联会话的「我知道了」。

        ＝``stop_contact.confirm_stop_contact``：摘「需人工」+「客户要求停联」从 conv_tags 移到
        conv_meta.stop_contact_at（历史可查、``frozen_reason`` 改读它，告别「最多一条」守卫不弱化）
        + 归档。与 PUT /tags 摘标同一 actor 口径（agent_ack）；广播 conv_tagged / conv_archived。
        """
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        try:
            _user = str(request.session.get("username") or "")
        except Exception:
            _user = ""
        actor = f"agent_ack:{_user}".rstrip(":")
        try:
            from src.inbox.stop_contact import confirm_stop_contact
            out = confirm_stop_contact(store, conversation_id, actor=actor)
        except Exception:
            logger.debug("stop-contact confirm 失败", exc_info=True)
            return {"ok": False, "error": "confirm_failed"}
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            bus = get_event_bus()
            bus.publish("conv_tagged", {"conversation_id": conversation_id,
                                        "tags": store.get_conv_tags(conversation_id), "ts": _t.time()})
            if out.get("archived"):
                bus.publish("conv_archived", {"conversation_id": conversation_id,
                                              "archived": True, "ts": _t.time()})
        except Exception:
            pass
        return {"ok": bool(out.get("ok")), "error": out.get("error", ""),
                "tags": store.get_conv_tags(conversation_id), "archived": bool(out.get("archived")),
                "stop_contact_at": float(out.get("stop_contact_at") or 0),
                "frozen": bool(out.get("stop_contact_at") or 0)}

    @app.patch("/api/workspace/conv/{conversation_id}/archive")
    async def api_conv_archive(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """T1+P28：归档/取消归档会话，并广播 conv_archived 事件供 Webhook 外发。"""
        body = await request.json()
        archived = bool(body.get("archived", True))
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        ok = store.set_conv_archived(
            conversation_id, archived,
            source="api:conv_archive",
            actor=str(request.session.get("username") or ""),
        )
        if ok:
            # P34：归档时自动触发 QA 评分计算（异步非阻塞）
            if archived:
                try:
                    import asyncio as _aio
                    _aio.get_event_loop().run_in_executor(
                        None, store.compute_and_store_qa_score, conversation_id
                    )
                except Exception:
                    pass
            # P28：广播会话归档事件（修正 EventBus API 调用签名）
            try:
                from src.integrations.shared.event_bus import get_event_bus
                import time as _t
                get_event_bus().publish("conv_archived", {
                    "conversation_id": conversation_id,
                    "archived": archived,
                    "ts": _t.time(),
                })
            except Exception:
                pass
        return {"ok": ok, "archived": archived}
