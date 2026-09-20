"""新账号「AI 接管方式」确认路由（P0 2026-08-30）。

挂载点：
    GET  /api/reply-settings/account-modes         — 待确认/已确认账号清单
    POST /api/reply-settings/account-modes/decide  — 写入账号档位决策（主管）

职责边界：
- 判定/存储全在纯函数核心 ``src.inbox.account_mode_onboarding``，本层只做
  取数注入 + 权限 + 审计 + i18n（响应 0 硬编码 CJK）。
- 决策后按账号对齐存量**系统落档**会话行（human/takeover/guard 行绝不覆盖，
  语义与「一键全自动」的 standby 对齐同族）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import Depends, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _append_audit(config_manager, *, actor, platform, account_id, mode,
                  aligned) -> None:
    """账号档位决策审计（best-effort，绝不影响写入结果）。"""
    try:
        base = getattr(config_manager, "config_path", None)
        if not base:
            return
        rec = {"ts": round(time.time(), 3), "actor": actor,
               "platform": platform, "account_id": account_id,
               "mode": mode, "aligned": aligned}
        p = Path(base).parent / "account_mode_audit.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("写账号档位审计失败（忽略）", exc_info=True)


def register_account_mode_routes(app, *, api_auth, config_manager) -> None:
    from src.inbox.account_mode_onboarding import (
        DECISION_MODES, PENDING_DEFAULT_MODE, align_account_conversations,
        decide_account_mode, decisions_snapshot, onboarding_enabled,
        pending_accounts,
    )

    @app.get("/api/reply-settings/account-modes")
    async def api_account_modes_get(request: Request, _=Depends(api_auth)):
        """待确认清单 + 已确认清单（只读）。功能关 → enabled:false 空表。"""
        import asyncio as _aio

        cfg = getattr(config_manager, "config", None) or {}
        if not onboarding_enabled(cfg):
            return {"ok": True, "enabled": False, "pending": [], "decided": [],
                    "default_mode": PENDING_DEFAULT_MODE}
        try:
            pending = await _aio.to_thread(pending_accounts, cfg)
        except Exception:
            logger.debug("pending_accounts 失败（返回空）", exc_info=True)
            pending = []
        try:
            decided = decisions_snapshot()
        except Exception:
            decided = []
        return {"ok": True, "enabled": True, "pending": pending,
                "decided": decided, "default_mode": PENDING_DEFAULT_MODE,
                "modes": list(DECISION_MODES)}

    @app.post("/api/reply-settings/account-modes/decide")
    async def api_account_modes_decide(request: Request, _=Depends(api_auth)):
        """写入账号档位决策（主管专属）+ 对齐该账号存量系统落档会话行。"""
        import asyncio as _aio

        from src.web.routes.unified_inbox_auth import _require_supervisor
        _require_supervisor(request)

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        platform = str(body.get("platform") or "").strip().lower()
        account_id = str(body.get("account_id") or "").strip()
        mode = str(body.get("mode") or "").strip().lower()
        actor = str(body.get("actor") or "").strip()
        if not actor:
            try:
                actor = str(request.session.get("username") or "web")
            except Exception:
                actor = "web"

        errors = []
        if not platform or not account_id:
            errors.append({"field": "platform/account_id", "code": "empty",
                           "message": tr(request, "rps_err_bad_value",
                                         field="platform/account_id")})
        if mode not in DECISION_MODES:
            errors.append({"field": "mode", "code": "bad_enum",
                           "message": tr(request, "rps_err_bad_value",
                                         field="mode")})
        if errors:
            return {"ok": False, "errors": errors}

        cfg = getattr(config_manager, "config", None) or {}
        if not onboarding_enabled(cfg):
            return {"ok": False, "errors": [{
                "field": "", "code": "disabled",
                "message": tr(request, "rps_am_err_disabled")}]}

        ok = decide_account_mode(platform, account_id, mode, actor=actor)
        if not ok:
            return {"ok": False, "errors": [{
                "field": "", "code": "save_failed",
                "message": tr(request, "rps_err_save_failed")}]}

        aligned = 0
        try:
            from src.web.routes.unified_inbox_services import _inbox_store
            store = _inbox_store(request)
            if store is not None:
                aligned = await _aio.to_thread(
                    align_account_conversations, store, platform, account_id,
                    mode)
        except Exception:
            logger.debug("账号会话对齐失败（决策已生效）", exc_info=True)
        _append_audit(config_manager, actor=actor, platform=platform,
                      account_id=account_id, mode=mode, aligned=aligned)
        try:
            pending = await _aio.to_thread(pending_accounts, cfg)
        except Exception:
            pending = []
        return {"ok": True, "platform": platform, "account_id": account_id,
                "mode": mode, "aligned_conversations": aligned,
                "pending": pending, "decided": decisions_snapshot()}
