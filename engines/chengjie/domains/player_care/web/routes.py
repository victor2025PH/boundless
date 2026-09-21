"""player_care 域 web 路由（由 admin._register_domain_routes 按激活域自动挂载 → 只有
``domain=player_care`` 的实例才有这些端点；story_matrix 实例永远没有）。

B4 commandbus 智聊侧端点（契约 huoke ``docs/CHATX_COMMANDBUS_CONTRACT.md`` §2，只读）：

    GET  /api/commandbus/pull?account=<手机侧标识>&limit=N
         → {"available": true, "commands": [<信封>...]}     （无待执行 → "commands": []）
    POST /api/commandbus/ack  {"command_id", "status": done|failed|rejected, "detail"}
         → {"available": true}                                （幂等；未知 id 也 200，fail-soft）

鉴权与 leadbus / replybus 同源：``Authorization: Bearer <BOUNDLESS_BUS_TOKEN>``（ctx.api_auth）。
commandbus 未开（``player_care.commandbus.enabled=false``）→ pull 永远空、ack 只回 available，
手机侧 poll 任务空转无害。

运营侧（本域内部，非契约）：
    POST /api/player-care/commands   {"kind": reengage|stop|note, "account", "phone", ...}   写权限
    GET  /api/player-care/commands?account=&status=&limit=                                 查看出箱
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

from ..commandbus import (
    ACK_STATUSES, KIND_NOTE, KIND_REENGAGE, KIND_STOP, KINDS, CommandOutbox,
    get_outbox, reengage_pool, resolve_commandbus_cfg, send_note, send_reengage, send_stop,
)

logger = logging.getLogger("PlayerCareWebRoutes")


def _outbox_for(config_manager: Any, *, require_enabled: bool) -> Optional[CommandOutbox]:
    try:
        return get_outbox(config_manager, require_enabled=require_enabled)
    except Exception:
        logger.debug("[player_care] 出箱不可用", exc_info=True)
        return None


def register_routes(app, ctx) -> None:
    config_manager = ctx.config_manager
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write_factory

    # ── 契约端点（手机侧调） ─────────────────────────────────────────────────
    @app.get("/api/commandbus/pull")
    async def api_commandbus_pull(request: Request, account: str = "", limit: int = 20,
                                  _=Depends(_api_auth)):
        ob = _outbox_for(config_manager, require_enabled=False)
        if ob is None or not resolve_commandbus_cfg(config_manager)["enabled"]:
            return {"available": True, "commands": []}
        try:
            cmds = ob.pull(account, limit)
        except Exception:
            logger.warning("[player_care] commandbus pull 失败", exc_info=True)
            cmds = []
        return {"available": True, "commands": cmds}

    @app.post("/api/commandbus/ack")
    async def api_commandbus_ack(request: Request, _=Depends(_api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cid = str(body.get("command_id") or "").strip()
        status = str(body.get("status") or "").strip().lower()
        detail = str(body.get("detail") or "")
        if not cid or status not in ACK_STATUSES:
            raise HTTPException(status_code=400, detail="command_id / status(done|failed|rejected) 必填")
        ob = _outbox_for(config_manager, require_enabled=False)
        if ob is not None:
            try:
                ob.ack(cid, status, detail)
            except Exception:
                logger.warning("[player_care] commandbus ack 失败", exc_info=True)
        return {"available": True}

    # ── 运营侧：手工入箱 / 查看 ────────────────────────────────────────────
    @app.post("/api/player-care/commands")
    async def api_player_care_enqueue(request: Request, _=Depends(_api_write("player_care"))):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON body 必填")
        kind = str(body.get("kind") or "").strip().lower()
        account = str(body.get("account") or "").strip()
        phone = str(body.get("phone") or "").strip()
        if kind not in KINDS:
            raise HTTPException(status_code=400, detail=f"kind 只能是 {'/'.join(KINDS)}")
        if not account:
            raise HTTPException(status_code=400, detail="account 必填（手机侧标识）")
        if not resolve_commandbus_cfg(config_manager)["enabled"]:
            raise HTTPException(status_code=409, detail="player_care.commandbus.enabled=false")
        ob = _outbox_for(config_manager, require_enabled=True)
        if ob is None:
            raise HTTPException(status_code=503, detail="出箱不可用")
        env: Optional[Dict[str, Any]]
        if kind == KIND_STOP:
            env = send_stop(ob, account=account, phone=phone, reason=str(body.get("reason") or "operator"),
                            created_by="operator")
        elif kind == KIND_REENGAGE:
            msgs = body.get("messages")
            if not isinstance(msgs, list) or not msgs:
                msgs = reengage_pool(str(body.get("lang") or ""))
            env = send_reengage(ob, account=account, phone=phone, messages=[str(m) for m in msgs],
                                reason=str(body.get("reason") or "operator"), created_by="operator")
        else:
            env = send_note(ob, account=account, phone=phone, text=str(body.get("text") or ""),
                            created_by="operator")
        if env is None:
            if kind == KIND_REENGAGE and ob.is_stopped(phone):
                raise HTTPException(status_code=409, detail="该号已 STOP，reengage 被拒")
            raise HTTPException(status_code=400, detail="参数不合法（phone / text 缺失）")
        return {"ok": True, "command": env}

    @app.get("/api/player-care/commands")
    async def api_player_care_commands(request: Request, account: str = "", status: str = "",
                                       limit: int = 50, _=Depends(_api_auth)):
        ob = _outbox_for(config_manager, require_enabled=False)
        cfg = resolve_commandbus_cfg(config_manager)
        if ob is None:
            return {"enabled": cfg["enabled"], "commands": [], "stats": {}}
        try:
            return {"enabled": cfg["enabled"], "commands": ob.list_recent(account=account, status=status, limit=limit),
                    "stats": ob.stats()}
        except Exception:
            logger.debug("[player_care] 出箱查询失败", exc_info=True)
            return {"enabled": cfg["enabled"], "commands": [], "stats": {}}

    logger.info("player_care web routes registered (commandbus pull/ack + operator commands)")


__all__ = ["register_routes"]
