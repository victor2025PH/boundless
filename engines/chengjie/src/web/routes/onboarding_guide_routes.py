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
    from src.integrations.douyin_official import (DEFAULT_OAUTH_CALLBACK_PATH, MODE, PLATFORM, douyin_cfg,
                                                  token_state)
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
            accounts.append({
                "account_id": str(acc.get("account_id") or ""), "label": str(acc.get("label") or ""),
                "token_state": token_state(meta, t), "scope": str(meta.get("scope") or ""),
                "access_days_left": (max(0, int((aexp - t) // 86400)) if aexp else None),
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
        q = request.query_params
        return templates.TemplateResponse(request, "help_onboarding.html", {
            "guide": guide, "slugs": list(SLUGS), "webhook_url": webhook_url, "panel": panel,
            "flash": {"saved": q.get("saved") == "1", "connected": str(q.get("connected") or ""),
                      "error": str(q.get("error") or "")},
        })

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


__all__ = ["register_onboarding_guide_routes", "douyin_connect_panel"]
