"""fleet_control 域 web 路由（由 admin._register_domain_routes 按激活域自动挂载 → 只有
``domain=fleet_control`` 的主控实例才有这些端点；智聊业务实例永远没有）。

契约 docs/FLEET_CONTROL_CONTRACT.md。两组端点、两套鉴权：

节点侧（Agent 出站调用；``Authorization: Bearer <node_key>``，注册时签发、只对本节点有效）：
    POST /api/fleet/enroll            {code, machine_id, host_name, proto_version, agent_version, ...}  （无鉴权，凭注册码）
    POST /api/fleet/heartbeat         心跳（白名单键）→ {ok, server_time, server_proto, has_tasks}
    GET  /api/fleet/tasks/pull?limit=&wait=   长轮询领任务 → {tasks: [信封...]}
    POST /api/fleet/tasks/ack         {task_id, status: done|failed|rejected, result, detail} → {ok}（幂等）

运营侧（主控后台；复用核心 ``ctx.api_auth`` / ``api_write_factory("fleet_control")``）：
    POST /api/fleet/enroll-codes      {label, group_name, ttl_min} → 注册码
    GET  /api/fleet/enroll-codes
    GET  /api/fleet/nodes  /api/fleet/nodes/{node_id}  /api/fleet/nodes/{node_id}/heartbeats
    POST /api/fleet/nodes/{node_id}/tasks   {kind, payload, target, ttl_sec}
    POST /api/fleet/nodes/{node_id}/revoke  POST /api/fleet/nodes/{node_id}  {label, group_name}
    GET  /api/fleet/tasks?node_id=&status=&kind=   GET /api/fleet/tasks/{task_id}   POST /api/fleet/tasks/{task_id}/cancel
    GET  /api/fleet/overview
页面：
    GET  /fleet/            独立主页（下载 + 三步安装 + 主控地址；不需登录，不动官网现有页）
    GET  /fleet/console     节点机群控制台（页面权限走核心 PAGE_PERMISSIONS）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.fleet.protocol import (
    ACK_STATUSES, DEFAULT_TASK_TTL_SEC, MAX_LONGPOLL_WAIT_SEC, MAX_PULL_LIMIT, PROTO_VERSION, TASK_KINDS,
)
from src.fleet.store import FleetStore, get_store, resolve_download, resolve_fleet_cfg

logger = logging.getLogger("FleetControlWebRoutes")


def _store_or_503(config_manager: Any) -> FleetStore:
    st = get_store(config_manager)
    if st is None:
        raise HTTPException(status_code=503, detail="fleet store unavailable")
    return st


def _bearer(request: Request) -> str:
    h = str(request.headers.get("authorization") or "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return ""


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _actor(request: Request) -> str:
    try:
        u = request.session.get("user") if hasattr(request, "session") else None
        if isinstance(u, dict):
            return str(u.get("username") or u.get("name") or "operator")
        if isinstance(u, str) and u:
            return u
    except Exception:
        pass
    return "operator"


def register_routes(app, ctx) -> None:
    config_manager = ctx.config_manager
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write_factory
    _page_auth = ctx.page_auth
    templates = ctx.templates

    def node_auth(request: Request) -> Dict[str, Any]:
        st = _store_or_503(config_manager)
        node = st.authenticate(_bearer(request))
        if node is None:
            raise HTTPException(status_code=401, detail="invalid node key")
        return node

    # ── 节点侧 ────────────────────────────────────────────────────────────
    @app.post("/api/fleet/enroll")
    async def api_fleet_enroll(request: Request):
        """注册码即凭证：允许放在 Authorization: Bearer <code>（节点还没有 node_key，
        走 Bearer 通道以复用核心 CSRF 中间件的 bearer 放行），也允许放在 body.code。"""
        body = await _json(request)
        st = _store_or_503(config_manager)
        res = st.enroll(
            code=str(body.get("code") or _bearer(request) or ""), machine_id=str(body.get("machine_id") or ""),
            host_name=str(body.get("host_name") or ""), proto_version=body.get("proto_version"),
            agent_version=str(body.get("agent_version") or ""), app_version=str(body.get("app_version") or ""),
            os_label=str(body.get("os") or ""),
            meta=body.get("meta") if isinstance(body.get("meta"), dict) else None,
        )
        if not res.get("ok"):
            code = 426 if res.get("error") == "proto_incompatible" else 403
            raise HTTPException(status_code=code, detail=res.get("error") or "enroll_failed")
        cfg = resolve_fleet_cfg(config_manager)
        res["heartbeat_sec"] = cfg["heartbeat_sec"]
        logger.info("[fleet] node enrolled %s host=%s", res["node_id"], body.get("host_name"))
        return res

    @app.post("/api/fleet/heartbeat")
    async def api_fleet_heartbeat(request: Request, node=Depends(node_auth)):
        body = await _json(request)
        st = _store_or_503(config_manager)
        out = st.heartbeat(node["node_id"], body)
        out["has_tasks"] = st.has_queued(node["node_id"])
        out["heartbeat_sec"] = resolve_fleet_cfg(config_manager)["heartbeat_sec"]
        return out

    @app.get("/api/fleet/tasks/pull")
    async def api_fleet_tasks_pull(request: Request, limit: int = MAX_PULL_LIMIT, wait: int = 0,
                                   node=Depends(node_auth)):
        st = _store_or_503(config_manager)
        wait = max(0, min(MAX_LONGPOLL_WAIT_SEC, int(wait or 0)))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait
        proto = request.headers.get("x-fleet-proto") or node.get("proto_version")
        while True:
            tasks = st.pull(node["node_id"], limit=limit, node_proto=proto)
            if tasks or loop.time() >= deadline:
                break
            await asyncio.sleep(1.0)
        return {"ok": True, "tasks": tasks, "server_proto": PROTO_VERSION}

    @app.post("/api/fleet/tasks/ack")
    async def api_fleet_tasks_ack(request: Request, node=Depends(node_auth)):
        body = await _json(request)
        tid = str(body.get("task_id") or "").strip()
        status = str(body.get("status") or "").strip().lower()
        if not tid or status not in ACK_STATUSES:
            raise HTTPException(status_code=400, detail="task_id / status(done|failed|rejected) 必填")
        st = _store_or_503(config_manager)
        rec = st.ack(tid, node_id=node["node_id"], status=status,
                     result=body.get("result") if isinstance(body.get("result"), dict) else None,
                     detail=str(body.get("detail") or ""))
        # 未知 / 不属于本节点的 task_id 也 200（fail-soft，Agent 不必重试）
        return {"ok": True, "known": rec is not None, "status": rec["status"] if rec else None}

    # ── 运营侧 ────────────────────────────────────────────────────────────
    @app.post("/api/fleet/enroll-codes")
    async def api_fleet_enroll_code_create(request: Request, _=Depends(_api_write("fleet_control"))):
        body = await _json(request)
        st = _store_or_503(config_manager)
        cfg = resolve_fleet_cfg(config_manager)
        code = st.create_enroll_code(label=str(body.get("label") or ""), group_name=str(body.get("group_name") or ""),
                                     created_by=_actor(request),
                                     ttl_min=int(body.get("ttl_min") or cfg["enroll_code_ttl_min"]))
        return {"ok": True, **code, "controller_url": cfg["public_url"]}

    @app.get("/api/fleet/enroll-codes")
    async def api_fleet_enroll_code_list(request: Request, include_used: bool = False, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        return {"ok": True, "codes": st.list_enroll_codes(include_used=include_used)}

    @app.get("/api/fleet/nodes")
    async def api_fleet_nodes(request: Request, group: str = "", include_revoked: bool = True, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        return {"ok": True, "nodes": st.list_nodes(group_name=group, include_revoked=include_revoked)}

    @app.get("/api/fleet/nodes/{node_id}")
    async def api_fleet_node(node_id: str, request: Request, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        node = st.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")
        node["tasks"] = st.list_tasks(node_id=node_id, limit=30)
        return {"ok": True, "node": node}

    @app.get("/api/fleet/nodes/{node_id}/heartbeats")
    async def api_fleet_node_heartbeats(node_id: str, request: Request, limit: int = 50, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        return {"ok": True, "heartbeats": st.heartbeat_history(node_id, limit=limit)}

    @app.post("/api/fleet/nodes/{node_id}")
    async def api_fleet_node_update(node_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        body = await _json(request)
        st = _store_or_503(config_manager)
        ok = st.update_node(node_id, label=body.get("label") if "label" in body else None,
                            group_name=body.get("group_name") if "group_name" in body else None)
        if not ok:
            raise HTTPException(status_code=404, detail="node not found or nothing to update")
        return {"ok": True, "node": st.get_node(node_id)}

    @app.post("/api/fleet/nodes/{node_id}/revoke")
    async def api_fleet_node_revoke(node_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        st = _store_or_503(config_manager)
        if not st.revoke(node_id):
            raise HTTPException(status_code=404, detail="node not found")
        return {"ok": True}

    @app.post("/api/fleet/nodes/{node_id}/tasks")
    async def api_fleet_node_task(node_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        body = await _json(request)
        kind = str(body.get("kind") or "").strip().lower()
        if kind not in TASK_KINDS:
            raise HTTPException(status_code=400, detail=f"kind 只能是 {'/'.join(TASK_KINDS)}")
        st = _store_or_503(config_manager)
        rec = st.enqueue(node_id, kind,
                         payload=body.get("payload") if isinstance(body.get("payload"), dict) else None,
                         target=body.get("target") if isinstance(body.get("target"), dict) else None,
                         ttl_sec=body.get("ttl_sec") or DEFAULT_TASK_TTL_SEC, created_by=_actor(request))
        if rec is None:
            raise HTTPException(status_code=409, detail="节点不存在 / 已吊销")
        return {"ok": True, "task": rec}

    @app.get("/api/fleet/tasks")
    async def api_fleet_tasks(request: Request, node_id: str = "", status: str = "", kind: str = "",
                              limit: int = 100, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        return {"ok": True, "tasks": st.list_tasks(node_id=node_id, status=status, kind=kind, limit=limit)}

    @app.get("/api/fleet/tasks/{task_id}")
    async def api_fleet_task(task_id: str, request: Request, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        rec = st.get_task(task_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"ok": True, "task": rec}

    @app.post("/api/fleet/tasks/{task_id}/cancel")
    async def api_fleet_task_cancel(task_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        st = _store_or_503(config_manager)
        return {"ok": st.cancel(task_id)}

    @app.get("/api/fleet/overview")
    async def api_fleet_overview(request: Request, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        out = st.overview()
        out["download"] = resolve_download(resolve_fleet_cfg(config_manager))
        return {"ok": True, **out}

    # ── 页面 ──────────────────────────────────────────────────────────────
    @app.get("/fleet/", response_class=HTMLResponse)
    @app.get("/fleet", response_class=HTMLResponse, include_in_schema=False)
    async def fleet_home_page(request: Request):
        cfg = resolve_fleet_cfg(config_manager)
        public_url = cfg["public_url"] or str(request.base_url).rstrip("/")
        return templates.TemplateResponse(request, "fleet_home.html",
                                          {"download": resolve_download(cfg), "public_url": public_url,
                                           "proto_version": PROTO_VERSION})

    @app.get("/fleet/console", response_class=HTMLResponse)
    async def fleet_console_page(request: Request, _=Depends(_page_auth)):
        cfg = resolve_fleet_cfg(config_manager)
        return templates.TemplateResponse(request, "fleet_console.html",
                                          {"public_url": cfg["public_url"] or str(request.base_url).rstrip("/"),
                                           "heartbeat_sec": cfg["heartbeat_sec"]})

    logger.info("fleet_control web routes registered (node enroll/heartbeat/pull/ack + operator console)")


__all__ = ["register_routes"]
