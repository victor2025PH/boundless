# -*- coding: utf-8 -*-
"""客服支持通道（实施49 P1-9）。

- ``GET  /api/support/info`` —— 机器码 + 版本 + 产品名 + 官网 + 直传能力位。
- ``POST /api/support/diag-upload`` —— 一键诊断直传，回 6 位短码（``?probe=1`` 探活）。

**为什么另起 ``/api/support/*`` 而不是复用 ``/api/admin/diagnostic-upload``**：
后者是 ``/api/admin/*``，而 ``admin._agent_api_allowed`` 把 ``ROLE_AGENT`` 挡在
admin 命名空间之外 —— 也就是说「一键上传给客服」这个能力对**坐席**（最常报障
的那批人）一直是 403。把 admin 路径开给坐席等于给整个 admin 前缀开口子；新建
一个语义自解释的 support 命名空间，只放「任何登录用户报障都该有」的两个只读/
自助端点，是更小的授权面。

上传实现共用 :mod:`src.utils.diag_upload`（两份实现会让客服拿到的包不等价）。
"""
from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)


def register_support_routes(app, ctx) -> None:
    api_auth = ctx.api_auth
    config_manager = ctx.config_manager

    @app.get("/api/support/info")
    async def api_support_info(request: Request):
        """报障所需的身份信息（机器码/版本/产品/官网）+ 直传能力位。

        机器码取不到（源码态/精简包无 licensing 模块）→ 空串，前端据此隐藏该行
        而不是显示占位符——「显示一个假的机器码」比不显示更坏。
        """
        api_auth(request)
        from src.utils.diag_upload import machine_code, site_url

        version = ""
        try:
            from src.utils.app_identity import app_version
            version = str(app_version() or "")
        except Exception:
            logger.debug("support-info 版本读取失败（已忽略）", exc_info=True)
        # 直传能力位：精简打包可能没带 diagnostic_bundle（打不出包＝按钮必然失败）。
        # 探测失败一律 fail-open——宁可让用户点一次拿到真错，也不平白关掉报障入口。
        upload_ok = True
        try:
            import importlib.util
            upload_ok = importlib.util.find_spec("src.utils.diagnostic_bundle") is not None
        except Exception:
            logger.debug("support-info 直传能力探测失败（按可用处理）", exc_info=True)
        product = ""
        site = ""
        try:
            from src.utils.branding import get_branding
            brand = get_branding(getattr(config_manager, "config", None) or {})
            product = str(brand.get("product_name") or "")
            site = str(brand.get("website_url") or "")
        except Exception:
            logger.debug("support-info 品牌读取失败（已忽略）", exc_info=True)
        return {
            "ok": True,
            "machine_code": machine_code(),
            "version": version,
            "product": product,
            "site": site or site_url(config_manager),
            "upload": bool(upload_ok),
        }

    @app.post("/api/support/diag-upload")
    async def api_support_diag_upload(request: Request, probe: int = 0):
        """一键诊断直传（坐席可用）：本机打包 → 服务端转投官网 → 回 6 位短码。

        ``?probe=1`` 只回 {ok}——前端据此判断后端是否已装载本路由（旧后端 404 →
        回落老的 ``/api/admin/diagnostic-upload``，都没有则整入口隐藏）。

        可选 body ``{"note": "<页面 | 报错文案>"}``＝报障现场：从报错 toast 直接
        点上来时前端会带上，客服因此**不必先问「你当时在做什么」**。
        """
        api_auth(request)
        if probe:
            return {"ok": True}
        from src.web.web_i18n import tr

        note = ""
        try:
            body = await request.json()
            note = (body or {}).get("note") or ""
        except Exception:
            note = ""  # 无 body / 非 JSON 都是合法调用（面板按钮就不带 note）

        from src.utils.diag_upload import build_and_upload, error_detail_for
        out = await build_and_upload(config_manager, note=note)
        if out.get("ok"):
            resp = {"ok": True, "code": str(out.get("code") or "")}
            if out.get("mini"):
                resp["mini"] = True   # 降级 mini 包送达（additive，旧前端零感知）
            return resp
        err = str(out.get("error") or "upload_failed")
        return {"ok": False, "detail": error_detail_for(request, err)}
