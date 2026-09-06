"""内部功能界面显隐路由（2026-08-14，配套 src/web/ui_visibility.py）。

- ``GET  /api/desktop/ui-flags``        —— 桌面壳/静态副驾消费的只读旗标。
  刻意**免鉴权**：只回四个布尔（零密钥零业务数据），桌面壳 main 进程在
  webview 会话建立前就要读它决定竖栏/页签条形态；fail 场景客户端按
  「全隐藏」兜底，与服务端缺省一致。
- ``GET  /api/developer/ui-visibility`` —— 开发者页读当前值（登录 + dev 解锁）。
- ``POST /api/developer/ui-visibility`` —— 按键写 overlay（登录 + dev 解锁；
  经 ``ConfigManager.set_overlay_flag`` 原子写 + 热合并，免重启生效）。
- ``POST /api/developer/developer-mode`` —— 开发者模式开关（2026-09-06 L-4 A /
  D-L2）：**只写 session**（``developer_mode``），不落 overlay——客户机上的临时
  排障视角，退出登录 / ``/developer/logout`` 即自动关；读口径见
  ``ui_visibility.resolve_developer_mode``。

写入口单一：只有本路由写 ``ui_visibility.*`` 与 session ``developer_mode``；
每次写落 INFO 审计行。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Body, Depends, HTTPException, Request

from src.web.ui_visibility import (DEFAULTS, DEVELOPER_MODE_KEY,
                                   resolve_developer_mode, resolve_ui_flavor,
                                   resolve_ui_visibility)
from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.ui_visibility_routes")


def register_ui_visibility_routes(app, auth_dep, config_manager=None) -> None:
    def _cfg_root() -> Dict[str, Any]:
        try:
            cfg = getattr(config_manager, "config", None)
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _require_dev(request: Request) -> None:
        try:
            unlocked = bool(request.session.get("dev_unlocked", False))
        except Exception:
            unlocked = False
        if not unlocked:
            raise HTTPException(403, tr(request, "err.uiv.dev_locked"))

    @app.get("/api/desktop/ui-flags")
    async def desktop_ui_flags():
        return {"ok": True, "flags": resolve_ui_visibility(_cfg_root())}

    @app.get("/api/developer/ui-visibility")
    async def get_ui_visibility(request: Request, _auth=Depends(auth_dep)):
        _require_dev(request)
        return {"ok": True, "flags": resolve_ui_visibility(_cfg_root())}

    @app.post("/api/developer/ui-visibility")
    async def set_ui_visibility(
        request: Request,
        payload: Dict[str, Any] = Body(...),
        _auth=Depends(auth_dep),
    ):
        _require_dev(request)
        key = str((payload or {}).get("key") or "").strip()
        if key not in DEFAULTS:
            raise HTTPException(404, tr(request, "err.uiv.unknown_key", name=key))
        want = bool((payload or {}).get("visible"))
        setter = getattr(config_manager, "set_overlay_flag", None)
        if not callable(setter):
            raise HTTPException(500, tr(request, "err.uiv.write_failed"))
        ok, _msg = setter(f"ui_visibility.{key}", want)
        if not ok:
            raise HTTPException(500, tr(request, "err.uiv.write_failed"))
        actor = ""
        try:
            actor = request.session.get("username", "")
        except Exception:
            pass
        logger.info("ui_visibility toggle: %s = %s (by %s)", key, want, actor or "?")
        return {"ok": True, "flags": resolve_ui_visibility(_cfg_root())}

    @app.post("/api/developer/developer-mode")
    async def set_developer_mode(
        request: Request,
        payload: Dict[str, Any] = Body(...),
        _auth=Depends(auth_dep),
    ):
        _require_dev(request)
        want = bool((payload or {}).get("on"))
        try:
            if want:
                request.session[DEVELOPER_MODE_KEY] = True
            else:
                request.session.pop(DEVELOPER_MODE_KEY, None)
        except Exception:
            raise HTTPException(500, tr(request, "err.uiv.write_failed"))
        actor = ""
        try:
            actor = request.session.get("username", "")
        except Exception:
            pass
        logger.info("developer_mode = %s (by %s)", want, actor or "?")
        return {
            "ok": True,
            "developer_mode": resolve_developer_mode(request.session),
            "flavor": resolve_ui_flavor(_cfg_root()),
        }
