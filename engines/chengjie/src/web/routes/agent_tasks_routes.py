"""坐席新手任务流 API（WP-7 后端半件，2026-08-18）。

三个端点（flag ``onboarding.agent_tasks`` 关＝全部 404，与 /welcome 同款门）：
- ``GET  /api/agent-tasks``           当前坐席状态（首读钉老坐席豁免）
- ``POST /api/agent-tasks/complete``  {task} 标记完成（幂等）
- ``POST /api/agent-tasks/dismiss``   手动收起（逃生门）

前端卡片（sidebar 线施工区）只消费 ``show`` 字段 + 在 5 个动作 handler 里
打 complete；埋点走既有 ui-event 通道（前缀 ``agt_``）复盘完成率。
逻辑全在 ``src.utils.agent_tasks``（可单测），路由只做鉴权/取用户/翻译。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_agent_tasks_routes(app, *, api_auth, config_manager=None,
                                user_store=None) -> None:
    """挂载坐席新手任务端点（任意登录坐席可用，按会话用户名隔离）。"""

    def _cfg() -> Dict[str, Any]:
        return getattr(config_manager, "config", None) or {}

    def _gate(request: Request) -> None:
        # flag 关＝裸 404（「无此页」语义，与 /welcome 同款；老实例零可见变化）
        from src.utils.agent_tasks import agent_tasks_enabled
        if not agent_tasks_enabled(_cfg()):
            raise HTTPException(404)

    def _username(request: Request) -> str:
        try:
            return str(request.session.get("username") or "").strip() or "?"
        except Exception:
            return "?"

    def _created_at(username: str) -> str:
        try:
            if user_store is None:
                return ""
            u = user_store.get_user(username)
            return str((u or {}).get("created_at") or "")
        except Exception:
            return ""

    @app.get("/api/agent-tasks")
    async def api_agent_tasks_status(request: Request, _=Depends(api_auth)):
        _gate(request)
        from src.utils.agent_tasks import user_status
        uname = _username(request)
        out = user_status(uname, account_created_at=_created_at(uname))
        out["ok"] = True
        return out

    @app.post("/api/agent-tasks/complete")
    async def api_agent_tasks_complete(request: Request, _=Depends(api_auth)):
        _gate(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        task = str((body or {}).get("task") or "").strip()
        from src.utils.agent_tasks import complete_task
        try:
            out = complete_task(_username(request), task)
        except ValueError:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="task"))
        out["ok"] = True
        return out

    @app.post("/api/agent-tasks/dismiss")
    async def api_agent_tasks_dismiss(request: Request, _=Depends(api_auth)):
        _gate(request)
        from src.utils.agent_tasks import dismiss
        out = dismiss(_username(request))
        out["ok"] = True
        return out
