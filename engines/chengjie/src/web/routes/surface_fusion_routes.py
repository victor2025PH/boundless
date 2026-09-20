# -*- coding: utf-8 -*-
"""双面板融合 API（``surface_fusion``，2026-08-13 P0）。

挂 ``/api/surface/*``，三个端点服务「看清能力 → 看清驾驶权 → 显式切换」闭环：

  GET  /api/surface/capabilities —— 平台能力注册表（workspace × native 双面板
       状态 + 桥接目标）+ 当前驾驶权 + 总开关回显。前端（工作台会话头徽章 /
       桌面壳诚实条）按它渲染，能力口径单一事实源在
       ``src/integrations/surface_fusion.py``。
  GET  /api/surface/pilot        —— 某账号当前自动化持有者（workspace|native）。
  POST /api/surface/pilot        —— 显式切换驾驶权（viewer 只读拒绝；带审计
       历史）。**这是防双发的机制层**：AutosendWorker 投递前按 owner 让位；
       将来原生页受控出站（P2）上线时反向同理。

设计约束：
- 端点不闸 ``surface_fusion.enabled``——注册表只读、切换本身无副作用（强制
  执行点在 worker guard 内自查开关），响应带 ``enabled`` 让前端自行决定 UI；
- 响应 detail 全走 ``tr()`` 请求级 i18n（0 硬编码中文，棘轮门禁口径）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.surface_fusion_routes")

_ROLE_VIEWER = "viewer"


def register_surface_fusion_routes(app, api_auth, config_manager=None):
    """挂载双面板融合 API。``api_auth``＝登录校验依赖。"""

    def _full_cfg(request: Request) -> Dict[str, Any]:
        cm = config_manager
        if cm is None:
            cm = getattr(request.app.state, "config_manager", None)
        cfg = getattr(cm, "config", None) if cm is not None else None
        return cfg if isinstance(cfg, dict) else {}

    def _require_write(request: Request) -> None:
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.sf.readonly"))

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web-admin")
        except Exception:
            return "web-admin"

    @app.get("/api/surface/capabilities")
    async def api_surface_capabilities(
            request: Request, platform: str = "messenger",
            account_id: str = "", _=Depends(api_auth)):
        """平台能力注册表 + 驾驶权快照（只读）。未策划平台返回空表。"""
        from src.integrations import surface_fusion as sf
        plat = str(platform or "").lower()
        return {
            "ok": True,
            "enabled": sf.fusion_enabled(_full_cfg(request)),
            "platform": plat,
            "curated_platforms": sf.curated_platforms(),
            "embeddable_platforms": list(sf.EMBEDDABLE_PLATFORMS),
            "capabilities": sf.capability_matrix(plat),
            # 能力四分组（both/workspace_only/native_only/bridge）——前端能力总览
            # 浮层与「原生页独有能力」提示的单一口径，免前端各算一套与注册表漂移。
            "summary": sf.capability_summary(plat),
            "pilot": sf.get_pilot(plat, account_id),
            # 让位观测（P4）：A 线 + B 线驾驶权让位计数（进程级；「锁在工作」的读数）
            "pilot_yields": sf.pilot_yield_stats(),
        }

    @app.get("/api/surface/pilot")
    async def api_surface_pilot_get(
            request: Request, platform: str = "",
            account_id: str = "", _=Depends(api_auth)):
        """某账号当前自动化持有者（无记录＝默认 workspace）。"""
        from src.integrations import surface_fusion as sf
        plat = str(platform or "").lower()
        if not plat:
            raise HTTPException(400, tr(request, "err.sf.platform_required"))
        out = dict(sf.get_pilot(plat, account_id))
        out.update({
            "ok": True,
            "enabled": sf.fusion_enabled(_full_cfg(request)),
            "platform": plat,
            "account_id": str(account_id or "default"),
        })
        return out

    @app.post("/api/surface/pilot")
    async def api_surface_pilot_set(request: Request, _=Depends(api_auth)):
        """显式切换驾驶权。body: ``{platform, account_id?, owner}``。

        owner ∈ {workspace, native}。切换即时生效（worker guard 闭包活读 +
        锁文件 mtime 缓存失效），无需重启。
        """
        from src.integrations import surface_fusion as sf
        _require_write(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        plat = str(body.get("platform") or "").lower()
        if not plat:
            raise HTTPException(400, tr(request, "err.sf.platform_required"))
        acct = str(body.get("account_id") or "default")
        owner = str(body.get("owner") or "").strip().lower()
        try:
            pilot = sf.set_pilot(plat, acct, owner, by=_actor(request))
        except ValueError:
            raise HTTPException(400, tr(request, "err.sf.owner_invalid",
                                        owner=owner))
        except Exception:
            logger.error("驾驶权切换写盘失败 %s:%s -> %s", plat, acct, owner,
                         exc_info=True)
            raise HTTPException(503, tr(request, "err.sf.write_failed"))
        logger.info("[surface_fusion] pilot switched %s:%s -> %s by=%s",
                    plat, acct, owner, _actor(request))
        return {
            "ok": True,
            "enabled": sf.fusion_enabled(_full_cfg(request)),
            "platform": plat,
            "account_id": acct,
            **pilot,
        }
