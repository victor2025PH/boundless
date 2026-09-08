# -*- coding: utf-8 -*-
"""渠道接入教程页 + 抖音企业号「接入面板」（2026-09-08，老板拍板「在页面做出教程和链接」）。

- ``GET /help/onboarding/{slug}``（session auth）：抖音企业版 / TikTok / 付款方式；数据源
  ``src/assistant/onboarding_guides.py``（与小智问答同一份）。未知 slug → 404。
- 抖音页附「接入面板」，把教程第 6 步从「改配置文件」升级为三个动作（实施96 P1-1 收尾）：
  ① ``POST /help/onboarding/douyin/credentials``：填 client_key / client_secret → ``save_overlay_patch`` 落
     ``config.local.yaml``（不碰主配置）并即时进内存；``enabled`` 一并置真（webhook 挂载 / worker 注册在启动期做，
     页面提示重启生效）。
  ② ``GET /help/onboarding/douyin/authorize``：生成防篡改 state，302 去抖音扫码授权页。
  ③ ``GET /webhook/douyin/oauth/callback``（公开，抖音回跳）：校验 state → code 换令牌 → 账号
     ``douyin/<open_id>`` mode=official 落注册表（meta 原子合并）→ 302 回教程页带结果。
- 页面按 ``request.state.ui_lang`` 选中英；webhook / 回调地址按当前 Host（或 X-Forwarded-*）算好给用户复制。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

_MASK_KEEP = 4


def _public_base(request: Request) -> str:
    """对外基地址：反代场景信 X-Forwarded-*，否则按本次请求的 scheme/host（如实，不猜 https）。"""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    scheme = request.headers.get("x-forwarded-proto") or str(request.url.scheme or "http")
    return f"{scheme}://{host}" if host else ""


def _mask(secret: str) -> str:
    s = str(secret or "")
    if not s:
        return ""
    return ("*" * max(4, len(s) - _MASK_KEEP)) + s[-_MASK_KEEP:]


def _config_manager(request: Request) -> Any:
    return getattr(request.app.state, "config_manager", None)


def _config(request: Request) -> Dict[str, Any]:
    return getattr(_config_manager(request), "config", None) or {}


def _audit(request: Request, action: str, target: str, old: str, new: str, *, actor: str = "") -> None:
    try:
        store = getattr(request.app.state, "audit_store", None)
        if store is None:
            return
        who = actor
        if not who:
            try:
                who = str(request.session.get("username") or "web_admin")
            except Exception:
                who = "web_admin"
        store.log(who, action, target, old, new)
    except Exception:
        logger.debug("[onboarding] 审计写入失败", exc_info=True)


def _save_patch(config_manager: Any, patch: Dict[str, Any]) -> bool:
    saver = getattr(config_manager, "save_overlay_patch", None)
    ok = saver(patch) if callable(saver) else None
    if not isinstance(ok, bool):
        # 简化桩 / 无 overlay 能力：至少合进内存再整文件保存
        try:
            cfg = getattr(config_manager, "config", None)
            if isinstance(cfg, dict):
                for k, v in patch.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass
        save = getattr(config_manager, "save", None)
        ok = bool(save()) if callable(save) else False
    return bool(ok)


def douyin_connect_panel(request: Request, *, registry: Any = None, now: Optional[float] = None) -> Dict[str, Any]:
    """教程页「接入面板」数据：凭证状态（脱敏）、回调/webhook 地址、已授权账号及令牌状态。"""
    from src.integrations.douyin_official import (DEFAULT_OAUTH_CALLBACK_PATH, MODE, PLATFORM, REAUTH_WARN_DAYS,
                                                  douyin_cfg, reauth_days_left, token_state)
    cfg = douyin_cfg(_config(request))
    base = _public_base(request)
    accounts = []
    try:
        reg = registry
        if reg is None:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
        t = float(now if now is not None else time.time())
        for acc in reg.list(PLATFORM):
            if str(acc.get("mode") or "") not in ("", MODE):
                continue
            meta = dict(acc.get("meta") or {})
            if not meta.get("access_token") and not meta.get("refresh_token"):
                continue
            aexp = float(meta.get("access_expires_at") or 0)
            reauth_days = reauth_days_left(meta, t)
            accounts.append({
                "account_id": str(acc.get("account_id") or ""), "label": str(acc.get("label") or ""),
                "token_state": token_state(meta, t), "scope": str(meta.get("scope") or ""),
                "access_days_left": (max(0, int((aexp - t) // 86400)) if aexp else None),
                "reauth_days_left": reauth_days,
                "reauth_soon": bool(reauth_days is not None and reauth_days <= REAUTH_WARN_DAYS),
                "authorized_at": float(meta.get("authorized_at") or 0),
            })
    except Exception:
        logger.debug("[onboarding] 读取抖音账号失败", exc_info=True)
    return {
        "has_key": bool(cfg["client_key"]), "has_secret": bool(cfg["client_secret"]), "enabled": cfg["enabled"],
        "client_key": cfg["client_key"], "client_secret_masked": _mask(cfg["client_secret"]),
        "callback_url": f"{base}{DEFAULT_OAUTH_CALLBACK_PATH}" if base else DEFAULT_OAUTH_CALLBACK_PATH,
        "webhook_url": f"{base}{cfg['webhook_path']}" if base else cfg["webhook_path"],
        "accounts": accounts,
    }


def tiktok_connect_panel(request: Request, *, registry: Any = None) -> Dict[str, Any]:
    """TikTok 面板数据：应用凭证状态、webhook 地址、已登记 Business Account 及按注册地算出的能力。"""
    from src.integrations.tiktok_official import MODE, PLATFORM, tiktok_cfg
    from src.integrations.tiktok_regions import capabilities
    cfg = tiktok_cfg(_config(request))
    base = _public_base(request)
    accounts = []
    try:
        reg = registry
        if reg is None:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
        for acc in reg.list(PLATFORM):
            if str(acc.get("mode") or "") not in ("", MODE):
                continue
            meta = dict(acc.get("meta") or {})
            caps = capabilities(meta.get("region"))
            accounts.append({"account_id": str(acc.get("account_id") or ""), "label": str(acc.get("label") or ""),
                             "has_token": bool(meta.get("access_token")), "region": caps["region"],
                             "dm_api": caps["dm_api"], "media_send": caps["media_send"], "shop_site": caps["shop_site"],
                             "alternatives": caps["alternatives"]})
    except Exception:
        logger.debug("[onboarding] 读取 TikTok 账号失败", exc_info=True)
    return {"has_app_id": bool(cfg["app_id"]), "has_secret": bool(cfg["secret"]), "enabled": cfg["enabled"],
            "app_id": cfg["app_id"], "secret_masked": _mask(cfg["secret"]),
            "webhook_url": f"{base}{cfg['webhook_path']}" if base else cfg["webhook_path"], "accounts": accounts}


def register_onboarding_guide_routes(app, page_auth, templates) -> None:
    @app.get("/help/onboarding/{slug}")
    async def onboarding_guide_page(slug: str, request: Request, _=Depends(page_auth)):
        from src.assistant.onboarding_guides import SLUGS, guide_for
        lang = str(getattr(request.state, "ui_lang", "") or "zh")
        guide = guide_for(slug, lang)
        if guide is None:
            raise HTTPException(status_code=404, detail="unknown guide")
        panel: Dict[str, Any] = {}
        webhook_url = ""
        if slug == "douyin":
            try:
                panel = douyin_connect_panel(request)
                webhook_url = panel["webhook_url"]
            except Exception:
                logger.debug("[onboarding] 抖音接入面板构建失败", exc_info=True)
                webhook_url = "/webhook/douyin"
        elif slug == "tiktok":
            try:
                panel = tiktok_connect_panel(request)
                webhook_url = panel["webhook_url"]
            except Exception:
                logger.debug("[onboarding] TikTok 接入面板构建失败", exc_info=True)
        q = request.query_params
        return templates.TemplateResponse(request, "help_onboarding.html", {
            "guide": guide, "slugs": list(SLUGS), "webhook_url": webhook_url, "panel": panel,
            "flash": {"saved": q.get("saved") == "1", "connected": str(q.get("connected") or ""),
                      "error": str(q.get("error") or ""), "region": str(q.get("region") or "")},
        })

    @app.post("/help/onboarding/tiktok/account")
    async def onboarding_tiktok_account(request: Request, _=Depends(page_auth),
                                        business_id: str = Form(""), access_token: str = Form(""),
                                        region: str = Form(""), app_id: str = Form(""), secret: str = Form(""),
                                        webhook_secret: str = Form("")):
        """登记 TikTok Business Account（business_id + access_token + 注册地）并可顺手存应用凭证。

        令牌来自 TikTok for Business 开发者门户（Business Messaging 授权流程以控制台为准，本面板不猜 OAuth 端点）；
        region 必填——注册地决定私信 API 能不能用，这是 TikTok 接入最先要说清的一件事。"""
        from src.integrations.tiktok_official import MODE, PLATFORM
        from src.integrations.tiktok_regions import capabilities, normalize_region
        bid = str(business_id or "").strip()
        tok = str(access_token or "").strip()
        reg_code = normalize_region(region)
        if not bid:
            return RedirectResponse("/help/onboarding/tiktok?error=missing_business_id", status_code=303)
        if not reg_code:
            return RedirectResponse("/help/onboarding/tiktok?error=missing_region", status_code=303)
        cm = _config_manager(request)
        patch: Dict[str, Any] = {}
        for k, v in (("app_id", app_id), ("secret", secret), ("webhook_secret", webhook_secret)):
            if str(v or "").strip():
                patch[k] = str(v).strip()
        if patch:
            patch["enabled"] = True
            if cm is None or not _save_patch(cm, {"tiktok": patch}):
                return RedirectResponse("/help/onboarding/tiktok?error=save_failed", status_code=303)
        try:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
            existing = reg.get(PLATFORM, bid) or {}
            meta: Dict[str, Any] = {"region": reg_code}
            if tok:
                meta["access_token"] = tok
            reg.upsert(PLATFORM, bid, mode=MODE, status="active",
                       label=(existing.get("label") or f"TikTok Business {bid[:8]}"), meta=meta, merge_meta=True)
        except Exception:
            logger.warning("[onboarding] TikTok 账号登记失败", exc_info=True)
            return RedirectResponse("/help/onboarding/tiktok?error=registry_failed", status_code=303)
        caps = capabilities(reg_code)
        _audit(request, "tiktok_account_register", f"tiktok/{bid}", "",
               f"region={reg_code} dm_api={caps['dm_api']} token={'y' if tok else 'n'}")
        return RedirectResponse(f"/help/onboarding/tiktok?connected={bid}&region={reg_code}", status_code=303)

    @app.post("/help/onboarding/douyin/credentials")
    async def onboarding_douyin_credentials(request: Request, _=Depends(page_auth),
                                            client_key: str = Form(""), client_secret: str = Form("")):
        from src.integrations.douyin_official import douyin_cfg
        cm = _config_manager(request)
        cur = douyin_cfg(getattr(cm, "config", None) or {})
        key = str(client_key or "").strip()
        secret = str(client_secret or "").strip()
        if not key:
            return RedirectResponse("/help/onboarding/douyin?error=missing_key", status_code=303)
        patch: Dict[str, Any] = {"douyin": {"client_key": key, "enabled": True}}
        # 密钥留空 = 保留已配置的（脱敏回显不可能原样提交）
        if secret:
            patch["douyin"]["client_secret"] = secret
        elif not cur["client_secret"]:
            return RedirectResponse("/help/onboarding/douyin?error=missing_secret", status_code=303)
        if cm is None or not _save_patch(cm, patch):
            return RedirectResponse("/help/onboarding/douyin?error=save_failed", status_code=303)
        _audit(request, "douyin_credentials_save", "douyin.client_key", cur["client_key"], key)
        return RedirectResponse("/help/onboarding/douyin?saved=1", status_code=303)

    @app.get("/help/onboarding/douyin/authorize")
    async def onboarding_douyin_authorize(request: Request, _=Depends(page_auth)):
        from src.integrations.douyin_official import (DEFAULT_OAUTH_CALLBACK_PATH, authorize_url, douyin_cfg,
                                                      oauth_state)
        cfg = douyin_cfg(_config(request))
        if not cfg["client_key"] or not cfg["client_secret"]:
            return RedirectResponse("/help/onboarding/douyin?error=missing_credentials", status_code=303)
        base = _public_base(request)
        redirect_uri = f"{base}{DEFAULT_OAUTH_CALLBACK_PATH}"
        return RedirectResponse(authorize_url(cfg["client_key"], redirect_uri, oauth_state(cfg["client_secret"])),
                                status_code=302)

    @app.get("/webhook/douyin/oauth/callback")
    async def douyin_oauth_callback(request: Request):
        """抖音回跳（公开路径，无会话）：state 防篡改 + 10 分钟时效；成功后 302 回教程页。"""
        from src.integrations.douyin_official import complete_oauth, douyin_cfg, verify_oauth_state
        q = request.query_params
        cfg_all = _config(request)
        cfg = douyin_cfg(cfg_all)
        if not verify_oauth_state(cfg["client_secret"], q.get("state")):
            return RedirectResponse("/help/onboarding/douyin?error=bad_state", status_code=303)
        if q.get("error") or not q.get("code"):
            return RedirectResponse(f"/help/onboarding/douyin?error=denied:{str(q.get('error') or 'no_code')[:40]}",
                                    status_code=303)
        res = await complete_oauth(str(q.get("code")), config=cfg_all)
        if not res.get("ok"):
            err = f"{res.get('error')}:{res.get('error_code') or ''}".rstrip(":")
            return RedirectResponse(f"/help/onboarding/douyin?error={err[:60]}", status_code=303)
        _audit(request, "douyin_oauth_connected", f"douyin/{res['open_id']}", "", str(res.get("scope") or ""),
               actor="douyin-oauth")
        return RedirectResponse(f"/help/onboarding/douyin?connected={res['open_id']}", status_code=303)


__all__ = ["register_onboarding_guide_routes", "douyin_connect_panel", "tiktok_connect_panel"]
