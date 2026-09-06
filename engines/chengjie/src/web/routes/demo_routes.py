"""C1-2 试用/Demo 模式：示例数据铺设 / 清空 / 状态 API。

- ``GET  /api/admin/demo``       —— demo 数据现状（present + 计数）。
- ``POST /api/admin/demo/seed``  —— 一键铺示例数据（跨多渠道/多坐席/多处置）。
- ``POST /api/admin/demo/clear`` —— 一键清空（按 demo: 命名空间，绝不碰真实数据）。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def _inbox(request: Request):
    return getattr(request.app.state, "inbox_store", None)


def _contacts(request: Request):
    """Contacts store（子系统未启用时 None → 漏斗演示自动跳过）。"""
    contacts = getattr(request.app.state, "contacts", None)
    return getattr(contacts, "store", None) if contacts is not None else None


def _kb(config_manager):
    try:
        from src.utils.kb_registry import get_kb_store
        return get_kb_store(config_manager, require_exists=False)
    except Exception:
        logger.debug("demo KB store 取用失败（已忽略）", exc_info=True)
        return None


def _account_registry():
    try:
        from src.integrations.account_registry import get_account_registry
        return get_account_registry()
    except Exception:
        return None


def register_demo_routes(app, *, api_auth, config_manager=None) -> None:
    @app.get("/api/admin/demo")
    async def api_admin_demo_status(request: Request):
        api_auth(request)
        from src.utils.demo_seeder import demo_status
        st = demo_status(_inbox(request), contacts_store=_contacts(request),
                         account_registry=_account_registry())
        st["ok"] = True
        return st

    @app.post("/api/admin/demo/seed")
    async def api_admin_demo_seed(request: Request):
        """L-4 B（#197 / D-L7）：只有「无真实账号的空工作区」才允许铺演示数据——
        有真实会话/账号时 409（全自动账号会把假会话当真客户真发）。清空不设限。"""
        api_auth(request)
        from src.utils.demo_seeder import real_workspace_footprint, seed_demo
        fp = real_workspace_footprint(_inbox(request), _account_registry())
        if fp["real_conversations"] > 0 or fp["real_accounts"] > 0:
            from src.web.web_i18n import tr
            raise HTTPException(409, tr(request, "err.demo.workspace_not_empty",
                                        c=fp["real_conversations"], a=fp["real_accounts"]))
        try:
            body = await request.json()
        except Exception:
            body = {}
        days = int((body or {}).get("days") or 14)
        return seed_demo(_inbox(request), days=days,
                         kb_store=_kb(config_manager), config_manager=config_manager,
                         contacts_store=_contacts(request))

    @app.post("/api/admin/demo/clear")
    async def api_admin_demo_clear(request: Request):
        api_auth(request)
        from src.utils.demo_seeder import clear_demo
        return clear_demo(_inbox(request), contacts_store=_contacts(request))
