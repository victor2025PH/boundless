# -*- coding: utf-8 -*-
"""渠道接入教程页 + 抖音企业号「接入面板」（2026-09-08，老板拍板「在页面做出教程和链接」）。

- ``GET /workspace/onboarding/{slug}``（session auth，工作台壳 ``workspace_base.html``）：抖音企业版 / TikTok / 付款方式；
  数据源 ``src/assistant/onboarding_guides.py``（与小智问答同一份）。未知 slug → 404。旧路径
  ``/help/onboarding/{slug}`` 301（实施99 P0-1 迁壳，2026-09-08）。
- 抖音页附「接入面板」，把教程第 6 步从「改配置文件」升级为三个动作（实施96 P1-1 收尾）：
  ① ``POST /workspace/onboarding/douyin/credentials``：填 client_key / client_secret → ``save_overlay_patch`` 落
     ``config.local.yaml``（不碰主配置）并即时进内存；``enabled`` 一并置真，随后 ``_hot_mount`` 即时挂 webhook 路由
     并注册官方 worker 工厂（实施99 P1-1，不再要求重启）。
  ② ``GET /workspace/onboarding/douyin/authorize``：生成防篡改 state，302 去抖音扫码授权页。
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
    from src.integrations.tiktok_official import (DEFAULT_OAUTH_CALLBACK_PATH, MODE, PLATFORM, REAUTH_WARN_DAYS,
                                                  get_state_store, reauth_days_left, tiktok_cfg, tiktok_me_link,
                                                  token_state, webhook_silence)
    from src.integrations.tiktok_regions import capabilities
    cfg = tiktok_cfg(_config(request))
    base = _public_base(request)
    accounts = []
    try:
        reg = registry
        if reg is None:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
        t = time.time()
        try:
            st = get_state_store(cfg["state_db_path"] or None)
        except Exception:
            st = None
        for acc in reg.list(PLATFORM):
            if str(acc.get("mode") or "") not in ("", MODE):
                continue
            meta = dict(acc.get("meta") or {})
            aid = str(acc.get("account_id") or "")
            caps = capabilities(meta.get("region"))
            days = reauth_days_left(meta, t)
            stats = st.stats(aid) if st is not None else {}
            sil = webhook_silence(stats, t)
            accounts.append({"account_id": aid, "label": str(acc.get("label") or ""),
                             "username": str(meta.get("username") or ""),
                             "me_link": tiktok_me_link(meta.get("username")),
                             "has_token": bool(meta.get("access_token")), "token_state": token_state(meta, t),
                             "auto_refresh": bool(meta.get("refresh_token")),
                             "reauth_days_left": days, "reauth_soon": bool(days is not None and days <= REAUTH_WARN_DAYS),
                             "events_total": int(stats.get("events_total") or 0), "webhook_silent": sil["silent"],
                             "silent_hours": int(sil["silent_sec"] // 3600),
                             "ref_counts": (st.ref_counts(aid, since_ts=t - 30 * 86400, limit=5) if st is not None else {}),
                             "region": caps["region"], "dm_api": caps["dm_api"], "media_send": caps["media_send"],
                             "shop_site": caps["shop_site"], "alternatives": caps["alternatives"]})
    except Exception:
        logger.debug("[onboarding] 读取 TikTok 账号失败", exc_info=True)
    return {"has_app_id": bool(cfg["app_id"]), "has_secret": bool(cfg["secret"]), "enabled": cfg["enabled"],
            "app_id": cfg["app_id"], "secret_masked": _mask(cfg["secret"]),
            "webhook_url": f"{base}{cfg['webhook_path']}" if base else cfg["webhook_path"],
            "callback_url": f"{base}{DEFAULT_OAUTH_CALLBACK_PATH}" if base else DEFAULT_OAUTH_CALLBACK_PATH,
            "accounts": accounts}


def _route_mounted(request: Request, path: str, method: str = "POST") -> bool:
    for r in getattr(request.app, "routes", []):
        if getattr(r, "path", "") == path and method in (getattr(r, "methods", None) or {method}):
            return True
    return False


def _hot_mount(request: Request, platform: str) -> Dict[str, bool]:
    """保存凭证后即时装载（实施99 P1-1）：挂 webhook 路由 + 注册官方 worker 工厂，不再要求重启。

    两个注册函数本身幂等（工厂查重；路由这里先查再挂）；编排器监督循环下一次 ``sync()`` 会按注册表拉起账号。
    多进程部署只对当前进程生效——自检里 ``restart_required`` 仍会如实反映其它进程的状态。"""
    app = request.app
    cm = _config_manager(request)
    cfg = getattr(cm, "config", None) or {}
    out = {"routes": False, "worker": False}
    try:
        if platform == "douyin":
            from src.integrations.douyin_official import (douyin_cfg, register_douyin_official_worker,
                                                          register_douyin_routes)
            path = douyin_cfg(cfg)["webhook_path"]
            if not _route_mounted(request, path):
                register_douyin_routes(app, cm, getattr(app.state, "telegram_client", None))
            out["routes"] = _route_mounted(request, path)
            out["worker"] = bool(register_douyin_official_worker(cfg))
        elif platform == "tiktok":
            from src.integrations.tiktok_official import (register_tiktok_official_worker, register_tiktok_routes,
                                                          tiktok_cfg)
            path = tiktok_cfg(cfg)["webhook_path"]
            if not _route_mounted(request, path):
                register_tiktok_routes(app, cm, getattr(app.state, "telegram_client", None))
            out["routes"] = _route_mounted(request, path)
            out["worker"] = bool(register_tiktok_official_worker(cfg))
    except Exception:
        logger.warning("[onboarding] %s 热挂载失败", platform, exc_info=True)
    return out


async def _hot_mount_and_sync(request: Request, platform: str) -> Dict[str, bool]:
    """热挂载后立即让编排器同步一次注册表（账号不用等下一轮监督循环）；编排器未运行则跳过。"""
    out = _hot_mount(request, platform)
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is not None:
            await orch.sync()
            out["synced"] = True
    except Exception:
        logger.debug("[onboarding] 热挂载后编排器 sync 失败", exc_info=True)
    return out


def _mark_auto(slug: str, step_no: int, note: str = "") -> None:
    """处理器在事实发生时写自动进度（如 webhook 注册成功 → 第 2 步 done）。失败只记日志。"""
    try:
        from src.web.onboarding_progress import get_progress
        get_progress().set(slug, step_no, state="done", source="auto", note=note)
    except Exception:
        logger.debug("[onboarding] 自动进度写入失败 %s/%s", slug, step_no, exc_info=True)


def _public_is_https(base: str) -> bool:
    if not base.startswith("https://"):
        return False
    host = base[len("https://"):].split("/", 1)[0].split(":", 1)[0].lower()
    return not (host in ("localhost", "testserver") or host.startswith(("127.", "10.", "192.168.", "0.")))


def onboarding_status(request: Request, slug: str) -> Dict[str, Any]:
    """接入自检（实施99 P0-2）：凭证 / 路由挂载 / worker 注册 / 公网 https / 账号令牌态 / 首条进线 → 状态灯 + 当前步。

    灯色：grey 未开始 · blue 进行中 · amber 等动作（需重启 / 令牌快到期 / webhook 静默）· red 阻塞（地区不可用 / 令牌失效）
    · green 已连通（收到过事件）。"""
    from src.integrations import account_orchestrator as ao
    base = _public_base(request)
    checks: Dict[str, Any] = {"public_https": _public_is_https(base)}
    light, step, hint = "grey", 1, ""
    if slug == "tiktok":
        panel = tiktok_connect_panel(request)
        from src.integrations.tiktok_official import tiktok_cfg
        cfg = tiktok_cfg(_config(request))
        accs = panel["accounts"]
        checks.update({
            "credentials_configured": bool(panel["has_app_id"] and panel["has_secret"]), "enabled": panel["enabled"],
            "webhook_mounted": _route_mounted(request, cfg["webhook_path"]),
            "worker_registered": ao.get_worker_factory("tiktok", "official") is not None,
            "accounts_total": len(accs),
            "accounts_authorized": sum(1 for a in accs if a["token_state"] != "needs_reauth" and a["has_token"]),
            "accounts_needs_reauth": sum(1 for a in accs if a["token_state"] == "needs_reauth" or not a["has_token"]),
            "accounts_region_blocked": sum(1 for a in accs if a["dm_api"] is False),
            "accounts_reauth_soon": sum(1 for a in accs if a["reauth_soon"]),
            "webhook_silent": sum(1 for a in accs if a["webhook_silent"]),
            "first_inbound_seen": any(a["events_total"] > 0 for a in accs),
        })
        checks["restart_required"] = bool(checks["credentials_configured"] and checks["enabled"]
                                          and not (checks["webhook_mounted"] and checks["worker_registered"]))
        total_steps = 5   # 与 onboarding_guides tiktok 步骤数一致（第 5 步＝验收）
        if not checks["credentials_configured"]:
            light, step, hint = "grey", 1, "credentials"
        elif not accs:
            light, step, hint = "blue", 4, "authorize"
        elif checks["accounts_region_blocked"] == len(accs):
            light, step, hint = "red", 4, "region_blocked"
        elif checks["accounts_authorized"] == 0:
            light, step, hint = "red", 4, "needs_reauth"
        elif checks["restart_required"]:
            light, step, hint = "amber", 4, "restart_required"
        elif not checks["first_inbound_seen"]:
            light, step, hint = "blue", 5, "await_first_dm"
        elif checks["webhook_silent"] or checks["accounts_reauth_soon"]:
            light, step, hint = "amber", 5, ("webhook_silent" if checks["webhook_silent"] else "reauth_soon")
        else:
            light, step, hint = "green", 5, "connected"
    elif slug == "douyin":
        panel = douyin_connect_panel(request)
        from src.integrations.douyin_official import douyin_cfg
        cfg = douyin_cfg(_config(request))
        accs = panel["accounts"]
        checks.update({
            "credentials_configured": bool(panel["has_key"] and panel["has_secret"]), "enabled": panel["enabled"],
            "webhook_mounted": _route_mounted(request, cfg["webhook_path"]),
            "worker_registered": ao.get_worker_factory("douyin", "official") is not None,
            "accounts_total": len(accs),
            "accounts_authorized": sum(1 for a in accs if a["token_state"] != "needs_reauth"),
            "accounts_needs_reauth": sum(1 for a in accs if a["token_state"] == "needs_reauth"),
            "accounts_reauth_soon": sum(1 for a in accs if a.get("reauth_soon")),
        })
        checks["restart_required"] = bool(checks["credentials_configured"] and checks["enabled"]
                                          and not (checks["webhook_mounted"] and checks["worker_registered"]))
        total_steps = 7
        if not checks["credentials_configured"]:
            light, step, hint = "grey", 1, "credentials"
        elif not accs:
            light, step, hint = "blue", 6, "authorize"
        elif checks["accounts_authorized"] == 0:
            light, step, hint = "red", 6, "needs_reauth"
        elif checks["restart_required"]:
            light, step, hint = "amber", 6, "restart_required"
        elif checks["accounts_reauth_soon"]:
            light, step, hint = "amber", 7, "reauth_soon"
        else:
            light, step, hint = "blue", 7, "await_first_dm"
    else:
        return {"slug": slug, "light": "grey", "step": 0, "total_steps": 0, "hint": "", "checks": checks, "steps": [],
                "progress": {"done": 0, "total": 0}}
    # ── 进度合并（实施99 P1-2）：自检现算的事实 > 手动勾选 > todo；审核类步骤到期未完成 → overdue ──
    from src.web.onboarding_progress import get_progress, merge_steps
    try:
        from src.assistant.onboarding_guides import guide_for
        g = guide_for(slug, "zh") or {}
        total_steps = max(total_steps, len(g.get("steps") or [])) if g.get("steps") else total_steps
    except Exception:
        pass
    now = time.time()
    auto: Dict[int, str] = {}
    if slug == "tiktok":
        if checks["credentials_configured"]:
            auto[4] = "done" if checks["accounts_authorized"] > 0 else "doing"
        if checks.get("first_inbound_seen"):
            auto[5] = "done"
    elif slug == "douyin":
        if checks["credentials_configured"]:
            auto[6] = "done" if checks["accounts_authorized"] > 0 else "doing"
    try:
        manual = get_progress().all(slug)
    except Exception:
        manual = {}
    steps = merge_steps(total_steps, manual, auto, now)
    done = sum(1 for s in steps if s["state"] == "done")
    overdue = [s["n"] for s in steps if s["overdue"]]
    blocked = [s["n"] for s in steps if s["state"] == "blocked" and not s["auto"]]
    if light in ("grey", "blue") and steps:
        # 灯色仍由事实决定；进度只影响「当前步」：第一个未完成的步
        pending = [s["n"] for s in steps if s["state"] != "done"]
        if pending and step < pending[0]:
            step = pending[0]
    if light not in ("red",) and overdue:
        light, hint = "amber", "review_overdue"
        step = overdue[0]
    elif light in ("grey", "blue") and blocked:
        light, hint = "amber", "step_blocked"
        step = blocked[0]
    return {"slug": slug, "light": light, "step": step, "total_steps": total_steps, "hint": hint, "checks": checks,
            "public_base": base, "steps": steps, "progress": {"done": done, "total": total_steps},
            "overdue_steps": overdue}


def register_onboarding_guide_routes(app, page_auth, templates) -> None:
    @app.get("/api/onboarding/{slug}/status")
    async def onboarding_status_api(slug: str, request: Request, _=Depends(page_auth)):
        from src.assistant.onboarding_guides import SLUGS
        if slug not in SLUGS:
            raise HTTPException(status_code=404, detail="unknown guide")
        return onboarding_status(request, slug)

    @app.get("/workspace/onboarding/{slug}")
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
        status: Dict[str, Any] = {}
        if slug in ("douyin", "tiktok"):
            try:
                status = onboarding_status(request, slug)
            except Exception:
                logger.debug("[onboarding] 自检失败", exc_info=True)
        q = request.query_params
        ctx: Dict[str, Any] = {
            "guide": guide, "slugs": list(SLUGS), "webhook_url": webhook_url, "panel": panel, "status": status,
            "flash": {"saved": q.get("saved") == "1", "connected": str(q.get("connected") or ""),
                      "error": str(q.get("error") or ""), "region": str(q.get("region") or ""),
                      "webhook": str(q.get("webhook") or "")},
        }
        # 工作台壳（workspace_base.html）需要的最小上下文；其余键缺省即安全（Jinja 非 strict）
        try:
            ctx["user_name"] = request.session.get("username") or ""
            ctx["user_display_name"] = request.session.get("display_name") or ctx["user_name"]
        except Exception:
            ctx["user_name"] = ctx["user_display_name"] = ""
        ctx.setdefault("is_supervisor", True)
        return templates.TemplateResponse(request, "help_onboarding.html", ctx)

    @app.post("/workspace/onboarding/tiktok/account")
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
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_business_id", status_code=303)
        if not reg_code:
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_region", status_code=303)
        cm = _config_manager(request)
        patch: Dict[str, Any] = {}
        for k, v in (("app_id", app_id), ("secret", secret), ("webhook_secret", webhook_secret)):
            if str(v or "").strip():
                patch[k] = str(v).strip()
        if patch:
            patch["enabled"] = True
            if cm is None or not _save_patch(cm, {"tiktok": patch}):
                return RedirectResponse("/workspace/onboarding/tiktok?error=save_failed", status_code=303)
            await _hot_mount_and_sync(request, "tiktok")
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
            return RedirectResponse("/workspace/onboarding/tiktok?error=registry_failed", status_code=303)
        caps = capabilities(reg_code)
        _audit(request, "tiktok_account_register", f"tiktok/{bid}", "",
               f"region={reg_code} dm_api={caps['dm_api']} token={'y' if tok else 'n'}")
        return RedirectResponse(f"/workspace/onboarding/tiktok?connected={bid}&region={reg_code}", status_code=303)

    @app.post("/workspace/onboarding/tiktok/credentials")
    async def onboarding_tiktok_credentials(request: Request, _=Depends(page_auth),
                                            app_id: str = Form(""), secret: str = Form("")):
        """保存开发者应用凭证（app_id / secret）到 overlay 并开启 tiktok.enabled；密钥留空＝保留。"""
        from src.integrations.tiktok_official import tiktok_cfg
        cm = _config_manager(request)
        cur = tiktok_cfg(getattr(cm, "config", None) or {})
        aid = str(app_id or "").strip()
        sec = str(secret or "").strip()
        if not aid:
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_app_id", status_code=303)
        patch: Dict[str, Any] = {"tiktok": {"app_id": aid, "enabled": True}}
        if sec:
            patch["tiktok"]["secret"] = sec
        elif not cur["secret"]:
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_secret", status_code=303)
        if cm is None or not _save_patch(cm, patch):
            return RedirectResponse("/workspace/onboarding/tiktok?error=save_failed", status_code=303)
        _audit(request, "tiktok_credentials_save", "tiktok.app_id", cur["app_id"], aid)
        await _hot_mount_and_sync(request, "tiktok")
        return RedirectResponse("/workspace/onboarding/tiktok?saved=1", status_code=303)

    @app.get("/workspace/onboarding/tiktok/authorize")
    async def onboarding_tiktok_authorize(request: Request, _=Depends(page_auth), region: str = ""):
        """一键授权：注册地必选（决定能力）→ 防篡改 state 带上 region → 302 去 TikTok 授权页。"""
        from src.integrations.tiktok_official import DEFAULT_OAUTH_CALLBACK_PATH, authorize_url, oauth_state, tiktok_cfg
        from src.integrations.tiktok_regions import normalize_region
        cfg = tiktok_cfg(_config(request))
        if not cfg["app_id"] or not cfg["secret"]:
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_credentials", status_code=303)
        reg_code = normalize_region(region)
        if not reg_code:
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_region", status_code=303)
        redirect_uri = f"{_public_base(request)}{DEFAULT_OAUTH_CALLBACK_PATH}"
        return RedirectResponse(authorize_url(cfg["app_id"], redirect_uri, oauth_state(cfg["secret"], reg_code)),
                                status_code=302)

    @app.get("/webhook/tiktok/oauth/callback")
    async def tiktok_oauth_callback(request: Request):
        """TikTok 回跳（公开路径）：state 校验 → code 换令牌 → 账号落注册表 → 顺手把 webhook 回调注册到 TikTok。"""
        from src.integrations.tiktok_official import (DEFAULT_OAUTH_CALLBACK_PATH, complete_oauth, ensure_webhook,
                                                      tiktok_cfg, verify_oauth_state)
        q = request.query_params
        cfg_all = _config(request)
        cfg = tiktok_cfg(cfg_all)
        region = verify_oauth_state(cfg["secret"], q.get("state"))
        if region is None:
            return RedirectResponse("/workspace/onboarding/tiktok?error=bad_state", status_code=303)
        if q.get("error") or not q.get("code"):
            err = str(q.get("error_description") or q.get("error") or "no_code")[:40]
            return RedirectResponse(f"/workspace/onboarding/tiktok?error=denied:{err}", status_code=303)
        base = _public_base(request)
        res = await complete_oauth(str(q.get("code")), config=cfg_all, redirect_uri=f"{base}{DEFAULT_OAUTH_CALLBACK_PATH}",
                                   region=region)
        if not res.get("ok"):
            if res.get("error") == "ungranted_scopes":
                return RedirectResponse("/workspace/onboarding/tiktok?error=ungranted_scopes", status_code=303)
            err = f"{res.get('error')}:{res.get('error_code') or ''}".rstrip(":")
            return RedirectResponse(f"/workspace/onboarding/tiktok?error={err[:60]}", status_code=303)
        _audit(request, "tiktok_oauth_connected", f"tiktok/{res['open_id']}", "",
               f"region={region} scope={res.get('scope')}", actor="tiktok-oauth")
        wh = "skip"
        try:
            r = await ensure_webhook(cfg_all, f"{base}{cfg['webhook_path']}")
            wh = "ok" if r.get("ok") else f"fail:{r.get('error_code') or r.get('error')}"
            if r.get("ok"):
                _mark_auto("tiktok", 2, "webhook registered via API")
        except Exception:
            logger.debug("[onboarding] TikTok webhook 注册异常", exc_info=True)
            wh = "fail:exception"
        return RedirectResponse(f"/workspace/onboarding/tiktok?connected={res['open_id']}&region={region}&webhook={wh}",
                                status_code=303)

    @app.post("/workspace/onboarding/tiktok/webhook")
    async def onboarding_tiktok_webhook(request: Request, _=Depends(page_auth)):
        """手动（重新）把私信 webhook 回调注册到 TikTok（应用级，用 app_id/secret）。"""
        from src.integrations.tiktok_official import ensure_webhook, tiktok_cfg
        cfg_all = _config(request)
        cfg = tiktok_cfg(cfg_all)
        if not cfg["app_id"] or not cfg["secret"]:
            return RedirectResponse("/workspace/onboarding/tiktok?error=missing_credentials", status_code=303)
        url = f"{_public_base(request)}{cfg['webhook_path']}"
        try:
            r = await ensure_webhook(cfg_all, url)
        except Exception:
            logger.debug("[onboarding] TikTok webhook 注册异常", exc_info=True)
            r = {"ok": False, "error": "exception"}
        if not r.get("ok"):
            return RedirectResponse(f"/workspace/onboarding/tiktok?error=webhook:{r.get('error_code') or r.get('error')}",
                                    status_code=303)
        _audit(request, "tiktok_webhook_register", "tiktok.webhook", "", url)
        _mark_auto("tiktok", 2, "webhook registered via API")
        return RedirectResponse("/workspace/onboarding/tiktok?webhook=ok", status_code=303)

    @app.post("/workspace/onboarding/{slug}/step/{n}")
    async def onboarding_mark_step(slug: str, n: int, request: Request, _=Depends(page_auth),
                                   state: str = Form(""), due_days: str = Form(""), note: str = Form("")):
        """手动勾选步骤（实施99 P1-2）：done / submitted（预计 N 天出结果）/ blocked / todo（重置）。自动检测的步骤不接受手动值。"""
        from src.assistant.onboarding_guides import SLUGS, guide_for
        from src.web.onboarding_progress import STATES, get_progress
        if slug not in SLUGS:
            raise HTTPException(status_code=404, detail="unknown guide")
        g = guide_for(slug, "zh") or {}
        total = len(g.get("steps") or [])
        if not (1 <= int(n) <= total):
            raise HTTPException(status_code=404, detail="unknown step")
        st = str(state or "").strip().lower()
        if st not in STATES:
            raise HTTPException(status_code=400, detail="bad state")
        try:
            cur = onboarding_status(request, slug)
            if any(s["n"] == int(n) and s["auto"] for s in cur.get("steps") or []):
                return RedirectResponse(f"/workspace/onboarding/{slug}?error=step_auto#obg-step-{n}", status_code=303)
        except HTTPException:
            raise
        except Exception:
            logger.debug("[onboarding] 自检失败（勾选前）", exc_info=True)
        due_ts = 0.0
        if st == "submitted":
            try:
                days = max(0, min(90, int(str(due_days or "5").strip() or 5)))
            except ValueError:
                days = 5
            due_ts = time.time() + days * 86400
        prog = get_progress()
        if st == "todo":
            prog.clear(slug, int(n))
        else:
            prog.set(slug, int(n), state=st, source="manual", note=str(note or ""), due_ts=due_ts)
        _audit(request, "onboarding_step_mark", f"{slug}/step{n}", "", st)
        return RedirectResponse(f"/workspace/onboarding/{slug}?step={n}#obg-step-{n}", status_code=303)

    @app.get("/help/onboarding/{slug}")
    async def onboarding_guide_page_legacy(slug: str, request: Request):
        """旧路径 301 → 工作台壳新路径（保留查询串：授权回调等仍带 ?connected= / ?error=）。"""
        q = str(request.url.query or "")
        return RedirectResponse(f"/workspace/onboarding/{slug}" + (f"?{q}" if q else ""), status_code=301)

    @app.post("/workspace/onboarding/douyin/credentials")
    async def onboarding_douyin_credentials(request: Request, _=Depends(page_auth),
                                            client_key: str = Form(""), client_secret: str = Form("")):
        from src.integrations.douyin_official import douyin_cfg
        cm = _config_manager(request)
        cur = douyin_cfg(getattr(cm, "config", None) or {})
        key = str(client_key or "").strip()
        secret = str(client_secret or "").strip()
        if not key:
            return RedirectResponse("/workspace/onboarding/douyin?error=missing_key", status_code=303)
        patch: Dict[str, Any] = {"douyin": {"client_key": key, "enabled": True}}
        # 密钥留空 = 保留已配置的（脱敏回显不可能原样提交）
        if secret:
            patch["douyin"]["client_secret"] = secret
        elif not cur["client_secret"]:
            return RedirectResponse("/workspace/onboarding/douyin?error=missing_secret", status_code=303)
        if cm is None or not _save_patch(cm, patch):
            return RedirectResponse("/workspace/onboarding/douyin?error=save_failed", status_code=303)
        _audit(request, "douyin_credentials_save", "douyin.client_key", cur["client_key"], key)
        await _hot_mount_and_sync(request, "douyin")
        return RedirectResponse("/workspace/onboarding/douyin?saved=1", status_code=303)

    @app.get("/workspace/onboarding/douyin/authorize")
    async def onboarding_douyin_authorize(request: Request, _=Depends(page_auth)):
        from src.integrations.douyin_official import (DEFAULT_OAUTH_CALLBACK_PATH, authorize_url, douyin_cfg,
                                                      oauth_state)
        cfg = douyin_cfg(_config(request))
        if not cfg["client_key"] or not cfg["client_secret"]:
            return RedirectResponse("/workspace/onboarding/douyin?error=missing_credentials", status_code=303)
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
            return RedirectResponse("/workspace/onboarding/douyin?error=bad_state", status_code=303)
        if q.get("error") or not q.get("code"):
            return RedirectResponse(f"/workspace/onboarding/douyin?error=denied:{str(q.get('error') or 'no_code')[:40]}",
                                    status_code=303)
        res = await complete_oauth(str(q.get("code")), config=cfg_all)
        if not res.get("ok"):
            err = f"{res.get('error')}:{res.get('error_code') or ''}".rstrip(":")
            return RedirectResponse(f"/workspace/onboarding/douyin?error={err[:60]}", status_code=303)
        _audit(request, "douyin_oauth_connected", f"douyin/{res['open_id']}", "", str(res.get("scope") or ""),
               actor="douyin-oauth")
        return RedirectResponse(f"/workspace/onboarding/douyin?connected={res['open_id']}", status_code=303)


__all__ = ["register_onboarding_guide_routes", "douyin_connect_panel", "tiktok_connect_panel", "onboarding_status"]
