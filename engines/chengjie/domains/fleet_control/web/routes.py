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
import re
from pathlib import Path
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from src.fleet.protocol import (
    ACK_STATUSES, DEFAULT_TASK_TTL_SEC, MAX_LONGPOLL_WAIT_SEC, MAX_PULL_LIMIT, PROTO_VERSION, TASK_KINDS,
)
from src.fleet.roompack import build_room_pack
from src.fleet.store import FleetStore, get_store, resolve_download, resolve_fleet_cfg

_INSTALL_PS1 = Path(__file__).resolve().parents[3] / "fleet_agent" / "Install-ChatXAgent.ps1"
_ROOM_KEY_RE = re.compile(r"^rk_[A-Za-z0-9_-]{20,180}$")

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


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_DL_TOKEN = re.compile(r"(/fleet/dl/)[A-Za-z0-9_\-]+")


def redact_download_path(text: str) -> str:
    """Replace a room-pack token in an access-log line. The token is a secret."""
    return _DL_TOKEN.sub(r"\1<redacted>", str(text))


class RedactFleetDownloadFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_download_path(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(redact_download_path(a) if isinstance(a, str) else a for a in args)
        elif isinstance(args, dict):
            record.args = {k: redact_download_path(v) if isinstance(v, str) else v for k, v in args.items()}
        return True


def _install_download_log_redaction() -> None:
    filt = RedactFleetDownloadFilter()
    for name in ("uvicorn.access", "uvicorn", "httpx"):
        log = logging.getLogger(name)
        if not any(isinstance(f, RedactFleetDownloadFilter) for f in log.filters):
            log.addFilter(filt)


def _client_ip(request: Request) -> str:
    """Trust X-Real-IP / X-Forwarded-For only when the TCP peer is this host (nginx)."""
    peer = ""
    if request.client and request.client.host:
        peer = str(request.client.host).strip()
    if peer.lower() in _LOOPBACK_HOSTS:
        real = str(request.headers.get("x-real-ip") or "").strip()
        if real:
            return real.split(",")[0].strip()[:64]
        xff = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff[:64]
    return peer[:64]


_BEARER_NOT_CRED = {"", "pending", "none", "anonymous"}


def _enroll_error(res: Dict[str, Any]) -> HTTPException:
    err = str(res.get("error") or "enroll_failed")
    if err == "proto_incompatible":
        return HTTPException(status_code=426, detail=err)
    if err == "rate_limited":
        return HTTPException(status_code=429, detail=err)
    return HTTPException(status_code=403, detail=err)


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
        """三种接入，协议版本不变：

        * 注册码（body.code，或 Bearer 里的 8 位码）——立刻签发 node_key。Bearer 通道是为了过核心 CSRF。
        * 机房密钥（body.room_key，或 Bearer 以 rk_ 开头）——在次数 / 有效期内自动批准。
        * 两者都没有——记为待批准，不签发 key，不能领任务。
        """
        body = await _json(request)
        st = _store_or_503(config_manager)
        cfg = resolve_fleet_cfg(config_manager)
        bearer = _bearer(request)
        room = str(body.get("room_key") or "").strip()
        code = str(body.get("code") or "").strip()
        if not room and bearer.startswith("rk_"):
            room = bearer
        if not code and bearer not in _BEARER_NOT_CRED and not bearer.startswith("rk_"):
            code = bearer
        common = dict(
            machine_id=str(body.get("machine_id") or ""), host_name=str(body.get("host_name") or ""),
            proto_version=body.get("proto_version"), agent_version=str(body.get("agent_version") or ""),
            app_version=str(body.get("app_version") or ""), os_label=str(body.get("os") or ""),
            meta=body.get("meta") if isinstance(body.get("meta"), dict) else None,
            client_ip=_client_ip(request),
        )
        secret = str(body.get("enroll_secret") or "")
        if room:
            res = st.redeem_room_key(room, enroll_secret=secret, instances=body.get("instances"), **common)
        elif code:
            res = st.enroll(code=code, **common)
        else:
            res = st.request_pending(
                instances=body.get("instances"), enroll_secret=secret,
                ttl_sec=int(cfg.get("pending_ttl_sec") or 0) or None, **common)
        if not res.get("ok"):
            raise _enroll_error(res)
        res["heartbeat_sec"] = cfg["heartbeat_sec"]
        if res.get("node_key"):
            logger.info("[fleet] node enrolled %s host=%s", res.get("node_id"), body.get("host_name"))
        else:
            logger.info("[fleet] pending enrollment host=%s ip=%s", body.get("host_name"), _client_ip(request))
        return res

    @app.post("/api/fleet/enroll/poll")
    async def api_fleet_enroll_poll(request: Request):
        """待批准节点来领结果。未知 / 拒绝 / 过期也返回 200，避免 Agent 把正常等待当成崩溃。"""
        body = await _json(request)
        st = _store_or_503(config_manager)
        res = st.poll_pending(str(body.get("request_id") or _bearer(request) or ""),
                              str(body.get("machine_id") or ""),
                              enroll_secret=str(body.get("enroll_secret") or ""))
        if res.get("node_key"):
            res["heartbeat_sec"] = resolve_fleet_cfg(config_manager)["heartbeat_sec"]
            logger.info("[fleet] pending claimed node=%s", res.get("node_id"))
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

    @app.get("/api/fleet/pending")
    async def api_fleet_pending(request: Request, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        return {"ok": True, "pending": st.list_pending()}

    @app.post("/api/fleet/pending/{request_id}/approve")
    async def api_fleet_pending_approve(request_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        body = await _json(request)
        st = _store_or_503(config_manager)
        res = st.approve_pending(
            request_id, decided_by=_actor(request),
            label=body.get("label") if "label" in body else None,
            group_name=body.get("group_name") if "group_name" in body else None,
            confirm_rotate=bool(body.get("confirm_rotate")))
        if not res.get("ok"):
            if res.get("error") == "confirm_rotate":
                raise HTTPException(status_code=409, detail=res.get("warning") or "confirm_rotate")
            status = 410 if res.get("error") == "expired" else 404
            raise HTTPException(status_code=status, detail=res.get("error") or "not_pending")
        logger.info("[fleet] pending approved node=%s by=%s", res.get("node_id"), _actor(request))
        return res

    @app.post("/api/fleet/pending/{request_id}/reject")
    async def api_fleet_pending_reject(request_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        st = _store_or_503(config_manager)
        res = st.reject_pending(request_id, decided_by=_actor(request))
        if not res.get("ok"):
            raise HTTPException(status_code=404, detail=res.get("error") or "not_pending")
        return res

    @app.post("/api/fleet/room-keys")
    async def api_fleet_room_key_create(request: Request, _=Depends(_api_write("fleet_control"))):
        body = await _json(request)
        st = _store_or_503(config_manager)
        rec = st.create_room_key(label=str(body.get("label") or ""), group_name=str(body.get("group_name") or ""),
                                 max_uses=int(body.get("max_uses") or 50), ttl_hours=int(body.get("ttl_hours") or 168),
                                 created_by=_actor(request))
        cfg = resolve_fleet_cfg(config_manager)
        public = (cfg["public_url"] or str(request.base_url).rstrip("/")).rstrip("/")
        rec["download_url"] = public + rec["download_path"]
        logger.info("[fleet] room key minted id=%s group=%s", rec["key_id"], rec["group_name"])
        return {"ok": True, **rec}

    @app.get("/api/fleet/room-keys")
    async def api_fleet_room_key_list(request: Request, _=Depends(_api_auth)):
        st = _store_or_503(config_manager)
        return {"ok": True, "room_keys": st.list_room_keys()}

    @app.post("/api/fleet/room-keys/{key_id}/revoke")
    async def api_fleet_room_key_revoke(key_id: str, request: Request, _=Depends(_api_write("fleet_control"))):
        st = _store_or_503(config_manager)
        if not st.revoke_room_key(key_id):
            raise HTTPException(status_code=404, detail="room key not found")
        return {"ok": True}

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

    @app.get("/fleet/advanced/Install-ChatXAgent.ps1")
    async def fleet_install_script(request: Request):
        """脚本走附件下载，避免浏览器把 .ps1 当文本打开。"""
        if not _INSTALL_PS1.is_file():
            raise HTTPException(status_code=404, detail="install script not packaged")
        return FileResponse(
            _INSTALL_PS1, media_type="application/octet-stream",
            filename="Install-ChatXAgent.ps1", content_disposition_type="attachment",
            headers={"Cache-Control": "no-cache"})

    @app.get("/fleet/dl/{token}")
    async def fleet_room_download(token: str, request: Request):
        """机房链接。密钥在路径里，应用日志不记这条路径；nginx 对该前缀关 access_log。"""
        if not _ROOM_KEY_RE.match(token or ""):
            raise HTTPException(status_code=404, detail="not found")
        st = _store_or_503(config_manager)
        info = st.room_key_for_download(token)
        if info is None:
            raise HTTPException(status_code=404, detail="not found")
        cfg = resolve_fleet_cfg(config_manager)
        dl = resolve_download(cfg)
        public = (cfg["public_url"] or str(request.base_url).rstrip("/")).rstrip("/")
        setup = str(dl.get("setup_url") or "")
        if not setup:
            base = public.rsplit("/fleet", 1)[0] if public.endswith("/fleet") else public
            setup = base + "/downloads/fleet/ChatXAgentSetup.exe"
        blob = build_room_pack(
            room_key=token, controller=public, setup_url=setup,
            setup_sha256=str(dl.get("setup_sha256") or ""),
            group=str(info.get("group_name") or ""),
            label=str(info.get("label") or ""), expires_at=info.get("expires_at"), max_uses=int(info.get("max_uses") or 1))
        logger.info("[fleet] room pack downloaded id=%s", info.get("key_id"))
        return Response(content=blob, media_type="application/zip", headers={
            "Content-Disposition": "attachment; filename=\"ChatXAgent-room.zip\"",
            "Cache-Control": "no-store",
        })

    _install_download_log_redaction()
    logger.info("fleet_control web routes registered (node enroll/heartbeat/pull/ack + operator console)")


__all__ = ["register_routes"]
