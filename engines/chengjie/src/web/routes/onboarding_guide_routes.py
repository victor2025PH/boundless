# -*- coding: utf-8 -*-
"""渠道接入教程页（2026-09-08，老板拍板「在页面做出教程和链接」）。

- ``GET /help/onboarding/{slug}``（session auth）：抖音企业版 / TikTok / 付款方式；数据源
  ``src/assistant/onboarding_guides.py``（与小智问答同一份）。未知 slug → 404。
- 页面按 ``request.state.ui_lang`` 选中英；抖音页把本实例的 webhook 地址按当前 Host 算好给用户复制。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request


def register_onboarding_guide_routes(app, page_auth, templates) -> None:
    @app.get("/help/onboarding/{slug}")
    async def onboarding_guide_page(slug: str, request: Request, _=Depends(page_auth)):
        from src.assistant.onboarding_guides import SLUGS, guide_for
        lang = str(getattr(request.state, "ui_lang", "") or "zh")
        guide = guide_for(slug, lang)
        if guide is None:
            raise HTTPException(status_code=404, detail="unknown guide")
        # 抖音 webhook 地址：按本实例对外 Host 生成（反代/中继场景可在页面手改域名）
        webhook_url = ""
        if slug == "douyin":
            try:
                from src.integrations.douyin_official import douyin_cfg
                cfg = getattr(getattr(request.app.state, "config_manager", None), "config", None) or {}
                path = douyin_cfg(cfg)["webhook_path"]
                host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
                scheme = request.headers.get("x-forwarded-proto") or "https"
                webhook_url = f"{scheme}://{host}{path}" if host else path
            except Exception:
                webhook_url = "/webhook/douyin"
        return templates.TemplateResponse(request, "help_onboarding.html", {
            "guide": guide, "slugs": list(SLUGS), "webhook_url": webhook_url,
        })


__all__ = ["register_onboarding_guide_routes"]
