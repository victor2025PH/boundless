# -*- coding: utf-8 -*-
"""会话级模型路由 API（2026-09-12，composer 模型选择器 · 键盘图标左侧「模型 ▾」）。

  GET  /api/unified-inbox/conv-model-route?platform&account_id&chat_key
        → 本会话路由 + 端点事实 + 可选项 + 授权（conv_route.describe）
  POST /api/unified-inbox/conv-model-route
        body {platform, account_id, chat_key, profile?, depth?, effort?, thinking?,
              bypass_safety?, model?, clear?}
        ``model``＝标准模式下「用谁答」（``ai.models`` 档名，""＝主链；2026-09-12 拆
        「模型 / 模式」双面板后新增；不在目录 → 400 ``X-Deny-Reason: unknown_model``）
        → 归一落库（app_settings KV ``conv_model_route:<cid>``）；切到无限制受
          ``licensing.feature_gate`` ``unrestricted_model`` 闸（闸关＝放行）
  GET  /api/ai/model-route/health?force=0
        → 无限制端点在线态（1-token chat ping，60s TTL；173 网关 GET /v1/models 会 RST）
  GET  /api/ai/model-route/stats
        → 观测：无限制会话数 / 守卫跳过计数 / 离线挂起数（ops 卡 & 排障）

单一事实源在 :mod:`src.ai.conv_route`；本文件只做 HTTP 壳：鉴权（api_auth）、
坐席身份（_session_agent → updated_by 审计）、状态码语义。与 ``conv-xlate-out``
（B67 会话级「发→X」）同一参数三元组与错误码风格，前端可复用同一套 hydrate 范式。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, HTTPException, Request

from src.ai import conv_route
from src.web.routes.unified_inbox_auth import _session_agent
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def _lock_hit(feature: str) -> None:
    """授权闸拦截计数（E6 定价信号；与 workflows / drafts 守卫同口径）。绝不抛。"""
    try:
        from src.web.feature_lock_stats import get_feature_lock_stats
        get_feature_lock_stats().record(feature, "api")
    except Exception:
        pass


def register_conv_model_route_routes(app, *, api_auth: Callable,
                                     config_manager: Any = None) -> None:

    def _cfg() -> Dict[str, Any]:
        try:
            c = getattr(config_manager, "config", None)
            return c if isinstance(c, dict) else {}
        except Exception:
            logger.debug("[conv_model_route] config_manager.config unreadable", exc_info=True)
            return {}

    def _agent(request: Request) -> str:
        try:
            return str(_session_agent(request).get("agent_id") or "agent")
        except Exception:
            logger.debug("[conv_model_route] no session agent, using 'agent'", exc_info=True)
            return "agent"

    def _cid(platform: Any, account_id: Any, chat_key: Any) -> str:
        return conv_route.conv_id(str(platform or ""), str(account_id or "default"),
                                  str(chat_key or ""))

    @app.get("/api/unified-inbox/conv-model-route")
    async def api_conv_model_route_get(
        request: Request, platform: str = "", account_id: str = "default",
        chat_key: str = "", _=Depends(api_auth),
    ):
        """读会话路由。缺席＝标准档（``route.profile == "standard"``，旋钮全空＝跟随全局）。"""
        cid = _cid(platform, account_id, chat_key)
        if not cid:
            return {"ok": False, "error": "bad_conversation"}
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        out = conv_route.describe(ibx, cid, _cfg())
        out.update({"ok": True, "conversation_id": cid})
        return out

    @app.post("/api/unified-inbox/conv-model-route")
    async def api_conv_model_route_set(request: Request, _=Depends(api_auth)):
        """写会话路由（patch 语义：只动给了的字段）。

        - ``clear:true`` → 删键回标准档；
        - ``profile:"unrestricted"`` / ``bypass_safety:true`` 需 ``ai.unrestricted.enabled``
          且授权闸放行，否则 403（``error=disabled`` / ``feature_locked``，前端灰显 + 锁标）；
        - 归一规则见 :func:`conv_route.normalize`（切档自动灌默认旋钮、非法值回落）。
        """
        try:
            body = await request.json()
        except Exception:
            logger.debug("[conv_model_route] non-JSON body, treating as empty", exc_info=True)
            body = {}
        if not isinstance(body, dict):
            body = {}
        cid = _cid(body.get("platform"), body.get("account_id"), body.get("chat_key"))
        if not cid:
            return {"ok": False, "error": "bad_conversation"}
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        by = _agent(request)
        cfg = _cfg()

        if body.get("clear"):
            conv_route.clear(ibx, cid, by=by)
            out = conv_route.describe(ibx, cid, cfg)
            out.update({"ok": True, "conversation_id": cid, "cleared": True})
            return out

        patch = {k: body[k] for k in ("profile", "depth", "effort", "thinking", "bypass_safety",
                                      "model")
                 if k in body}
        if not patch:
            out = conv_route.describe(ibx, cid, cfg)
            out.update({"ok": True, "conversation_id": cid, "noop": True})
            return out

        # 模型档必须在目录里（主链 "" 恒可选）：指向不存在的档＝「路由到空气」，运行时会
        # 静默回主链，坐席却以为切成了——这里直接 400，比装成功诚实。
        if "model" in patch:
            want = str(patch.get("model") or "").strip()
            if want and not conv_route.model_spec(cfg, want):
                raise HTTPException(400, "unknown model profile",
                                    headers={"X-Deny-Reason": "unknown_model"})
            opens_unr = conv_route.model_opens_unrestricted(want, cfg)
            # ChatX 目录行走无限制闸（flagship），不走 multi_vendor_model。
            if want and not opens_unr and not conv_route.vendor_allowed(cfg):
                _lock_hit(conv_route.VENDOR_FEATURE_NAME)
                raise HTTPException(403, "per-chat vendor model requires a higher plan",
                                    headers={"X-Deny-Reason": "vendor_locked"})
            if opens_unr and "profile" not in patch:
                patch["profile"] = conv_route.PROFILE_UNRESTRICTED

        wants_unrestricted = (
            str(patch.get("profile") or "").strip().lower() == conv_route.PROFILE_UNRESTRICTED
            or conv_route._truthy(patch.get("bypass_safety"))
        )
        if wants_unrestricted:
            if not conv_route.enabled(cfg):
                raise HTTPException(403, "unrestricted model disabled",
                                    headers={"X-Deny-Reason": "disabled"})
            if not conv_route.feature_allowed(cfg):
                _lock_hit(conv_route.FEATURE_NAME)
                raise HTTPException(403, "unrestricted model requires a higher plan",
                                    headers={"X-Deny-Reason": "feature_locked"})

        route = conv_route.set(ibx, cid, patch, by=by, config=cfg)
        if route is None:
            return {"ok": False, "error": "store_write_failed"}
        out = conv_route.describe(ibx, cid, cfg)
        out.update({"ok": True, "conversation_id": cid})
        return out

    @app.get("/api/ai/model-route/health")
    async def api_model_route_health(request: Request, force: int = 0,
                                     profile: Optional[str] = None, all: int = 0,   # noqa: A002
                                     _=Depends(api_auth)):
        """端点在线态（缓存 60s；``force=1`` 立刻重探）。绝不抛。

        - 不带参数：无限制端点（历史语义，模式面板用）；
        - ``profile=<档名>``（``""``＝主链）：单档；
        - ``all=1``：目录全档并发，``health_all={name: health}``（模型面板打开一次画全）。
        """
        cfg = _cfg()
        out: Dict[str, Any] = {"ok": True}
        try:
            if all:
                out["health_all"] = await conv_route.probe_catalog(cfg, force=bool(force))
            out["health"] = await conv_route.probe_endpoint(cfg, force=bool(force),
                                                            profile=profile)
        except Exception:
            logger.debug("[conv_model_route] probe failed", exc_info=True)
            out["health"] = {"configured": False, "online": False, "error": "probe_failed"}
        return out

    @app.get("/api/ai/model-route/stats")
    async def api_model_route_stats(request: Request, _=Depends(api_auth)):
        """观测快照：无限制会话数 / 守卫跳过分层计数 / 离线挂起数 / 全局安全钥匙。"""
        ibx = _inbox_store(request)
        try:
            snap = conv_route.stats_snapshot(ibx)
        except Exception:
            logger.debug("[conv_model_route] stats_snapshot failed", exc_info=True)
            snap = {}
        return {"ok": True, "stats": snap, "enabled": conv_route.enabled(_cfg()),
                "allowed": conv_route.feature_allowed(_cfg())}


__all__ = ["register_conv_model_route_routes"]
