"""开发者工具页面路由（Phase E1 续拆，从 admin.py 抽出）。

端点：
  GET  /developer
  POST /developer/auth
  POST /developer/logout

依赖：templates / require_auth / config_manager（经 AdminRouteContext）。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

_DEV_PASSWORD = "Along2026"


def register_developer_page_routes(app, ctx) -> None:
    templates = ctx.templates
    config_manager = ctx.config_manager
    _require_auth = ctx.require_auth

    @app.get("/developer", response_class=HTMLResponse)
    async def developer_page(request: Request):
        _require_auth(request)
        dev_unlocked = request.session.get("dev_unlocked", False)
        page_ctx: dict = {"dev_unlocked": dev_unlocked, "dev_error": ""}
        if dev_unlocked:
            cfg = config_manager.config or {}
            wb = cfg.get("web_admin", {}) if isinstance(cfg.get("web_admin"), dict) else {}
            # L-6 D：Session 密钥行显真实状态（桌面首启已自动生成 / 仍是默认值），
            # 不再一律「留空保留原值」占位——值本身绝不进模板。
            try:
                from src.utils.config_manager import ConfigManager
                secret_default = ConfigManager.web_secret_is_default(wb.get("secret_key"))
            except Exception:
                secret_default = True
            page_ctx.update({
                "ai": cfg.get("ai", {}),
                "voice_ai": (
                    ((cfg.get("messenger_rpa") or {}).get("voice_output") or {})
                    if isinstance(cfg.get("messenger_rpa"), dict)
                    else {}
                ),
                "wb": wb,
                "wb_secret_state": "default" if secret_default else "set",
                "tg": cfg.get("telegram", {}),
                "notif": cfg.get("notifications", cfg.get("webhook", {})),
            })
        return templates.TemplateResponse(request, "developer.html", page_ctx)

    @app.post("/developer/auth", response_class=HTMLResponse)
    async def developer_auth(request: Request):
        _require_auth(request)
        form = await request.form()
        password = (form.get("password") or "").strip()
        if password == _DEV_PASSWORD:
            request.session["dev_unlocked"] = True
            return RedirectResponse("/developer", status_code=303)
        return templates.TemplateResponse(request, "developer.html", {
            "dev_unlocked": False,
            "dev_error": "密码错误，请重试",
        })

    @app.post("/developer/logout")
    async def developer_logout(request: Request):
        _require_auth(request)
        request.session.pop("dev_unlocked", None)
        # 开发者模式（L-4 A）随密码闸一起关：它只在 dev_unlocked 为真时生效，
        # 这里顺手清掉，避免下次解锁时上一个人留下的视角直接回来。
        request.session.pop("developer_mode", None)
        return RedirectResponse("/developer", status_code=303)
