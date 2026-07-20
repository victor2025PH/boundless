"""统一收件箱——批量操作 / 通知中心路由域（巨石拆分 slice 26）。

把 ``register_unified_inbox_routes`` 巨型闭包中相邻的两段子域整体外移为
``register_batch_notif_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 23 批量操作：``batch/archive`` + ``batch/tags`` + ``batch/assign``
- Phase 24 通知中心：``notifications`` (GET) + ``notifications/read`` (POST)
  （SSE 实时推送在别处；此处仅断线重连后的历史同步 + badge 已读标记）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 26 端点契约断言）。

依赖全部朝下：services._inbox_store（通知中心仅读 app.state.notif_queue）。
只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _session_agent
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _deny_viewer_write(request: Request) -> None:
    """十二期：viewer（只读账号）禁止批量写操作——归档/打标/转派均改数据。

    与坐席工作台角色约定一致（web_user_store：viewer 只读不接管）；此前三个
    batch 端点仅 api_auth 放行，viewer 也能改写会话归属，属权限缺口。
    """
    if _session_agent(request).get("role", "") == "viewer":
        raise HTTPException(403, tr(request, "err.perm.viewer_readonly"))


def register_batch_notif_routes(app, *, api_auth) -> None:
    """挂载批量操作（归档/标签/分配）+ 通知中心（历史/已读）端点。"""

    # ─── Phase 23: 批量操作 ─────────────────────────────────────────────────

    @app.post("/api/workspace/batch/archive")
    async def api_batch_archive(request: Request, _=Depends(api_auth)):
        """P23：批量归档/取消归档会话。

        Body: {conversation_ids: [str, ...], archived: bool}
        返回: {ok: true, updated: int}
        """
        _deny_viewer_write(request)
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        archived = bool(body.get("archived", True))
        if not cids:
            return {"ok": False, "error": tr(request, "err.ws.field_required", field="conversation_ids")}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        updated = 0
        for cid in cids[:200]:  # 单次上限 200 条
            try:
                ok = store.set_conv_archived(cid, archived)
                if ok:
                    updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "archived": archived}

    @app.post("/api/workspace/batch/tags")
    async def api_batch_tags(request: Request, _=Depends(api_auth)):
        """P23：批量修改会话标签。

        Body: {conversation_ids: [str, ...], tags: [str, ...],
               mode: 'set'|'add'|'remove'}
          mode=set  → 替换全部标签
          mode=add  → 追加（去重）
          mode=remove → 删除指定标签
        返回: {ok: true, updated: int}
        """
        _deny_viewer_write(request)
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        tags = [str(t) for t in (body.get("tags") or []) if str(t).strip()]
        mode = str(body.get("mode", "add")).lower()
        if mode not in ("set", "add", "remove"):
            mode = "add"
        if not cids:
            return {"ok": False, "error": tr(request, "err.ws.field_required", field="conversation_ids")}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        import json as _json
        updated = 0
        for cid in cids[:200]:
            try:
                current = store.get_conv_tags(cid) or []
                if mode == "set":
                    new_tags = tags
                elif mode == "add":
                    new_tags = list(dict.fromkeys(current + tags))  # 保序去重
                else:  # remove
                    rm = set(tags)
                    new_tags = [t for t in current if t not in rm]
                ok = store.set_conv_tags(cid, new_tags)
                if ok:
                    updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "mode": mode}

    @app.post("/api/workspace/batch/assign")
    async def api_batch_assign(request: Request, _=Depends(api_auth)):
        """P23：批量分配会话给坐席。

        Body: {conversation_ids: [str, ...], agent_id: str}
        返回: {ok: true, updated: int}
        """
        _deny_viewer_write(request)
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        agent_id = str(body.get("agent_id") or "").strip()
        if not cids or not agent_id:
            return {"ok": False, "error": tr(request, "err.ws.field_required", field="conversation_ids / agent_id")}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        # 十二期修复：原实现 update_conv_meta(cid, {dict}) 与方法签名（keyword-only）
        # 不符，每次 TypeError 被吞 → 接口自 P23 起静默空转（恒 updated=0）。
        # 改走 AgentCoordinator.claim(force=True)：与单会话认领同一事实源（claims 表），
        # 「谁在处理」徽章 /「我的」筛选立即生效；TTL 语义沿用认领（转派≠永久占有）。
        from src.workspace.agent_coordinator import AgentCoordinator
        coord = AgentCoordinator.from_request(request)
        target_name = agent_id
        try:
            for p in coord.list_presence():
                if str(p.get("agent_id") or "") == agent_id and p.get("display_name"):
                    target_name = str(p["display_name"])
                    break
        except Exception:
            pass
        operator = _session_agent(request)
        updated = 0
        for cid in cids[:200]:
            try:
                result = coord.claim(cid, agent_id, agent_name=target_name, force=True)
                if result.get("ok"):
                    updated += 1
            except Exception:
                logger.debug("batch assign claim 失败（已忽略）: %s", cid, exc_info=True)
        # 转派留痕（复用草稿审计事件流，主管在审计/时间线可回溯「谁把谁转给了谁」）
        if updated:
            try:
                store.record_draft_audit(
                    "", action="assign", agent_id=operator.get("agent_id", ""),
                    reason=f"→ {agent_id}（{updated} 会话）",
                    conversation_id=cids[0] if len(cids) == 1 else "",
                )
            except Exception:
                logger.debug("assign 审计写入失败（已忽略）", exc_info=True)
        return {"ok": True, "updated": updated, "agent_id": agent_id}

    # ─── Phase 24: 通知中心（SSE 事件广播） ───────────────────────────────

    @app.get("/api/workspace/notifications")
    async def api_workspace_notifications(
        request: Request,
        limit: int = 50,
    ):
        """P24：获取最近通知（SSE 事件历史，存于内存队列）。

        前端在 SSE 断线重连后调用此接口同步缺漏事件。
        """
        # 通知队列挂在 app.state.notif_queue（由 SSE 推送时顺带写入）
        queue: list = getattr(request.app.state, "notif_queue", [])
        limit = max(1, min(200, int(limit or 50)))
        # P8：随历史一并回传该坐席「已读水位线」，前端据此跨设备恢复已读状态
        read_at = 0
        try:
            store = _inbox_store(request)
            if store is not None:
                agent = _session_agent(request)
                read_at = int(store.get_agent_prefs(agent["agent_id"]).get("notif_read_at") or 0)
        except Exception:
            logger.debug("读取 notif_read_at 失败（已忽略）", exc_info=True)
        return {"ok": True, "notifications": queue[-limit:], "read_at": read_at}

    @app.post("/api/workspace/notifications/read")
    async def api_workspace_notifications_read(request: Request, _=Depends(api_auth)):
        """P24/P8：标记所有通知为已读 —— 写「已读水位线」到坐席偏好（跨设备保留）。"""
        now_ms = int(__import__("time").time() * 1000)
        read_at = now_ms
        try:
            store = _inbox_store(request)
            if store is not None:
                agent = _session_agent(request)
                read_at = int(store.set_notif_read_at(agent["agent_id"], now_ms) or now_ms)
        except Exception:
            logger.debug("写 notif_read_at 失败（仅前端生效）", exc_info=True)
        return {"ok": True, "read_at": read_at}
