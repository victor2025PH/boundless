"""会员中心（融合实例 P3 精简版）。

端点：
  GET /membership            —— 会员中心页（档位/到期/席位/渠道/字符额度/功能矩阵/升级引导）。
                                任意登录用户可看（信息页，nav 锁标的落点；菜单入口仅 master）。
  GET /api/admin/membership  —— 同数据 JSON（前端刷新 / 集成 / 测试）。

数据口径 = feature_gate.gate_snapshot（档位×功能矩阵单源）+ license status
（到期/席位/渠道）+ quota_store（P0-4 字符额度）+ web_users 计数（席位用量）。
本模块只读拼装，不引入任何新判定逻辑（防与 feature_gate 口径漂移）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


def _quota_snapshot() -> Dict[str, Any]:
    """P0-4 字符额度快照（与 license_routes 同源实现；无额度 → included=0）。"""
    try:
        from src.licensing.quota_store import check_license_quota

        q = check_license_quota()
        return {
            "included_chars": q.get("included", 0),
            "used_chars": q.get("used", 0),
            "remaining_chars": q.get("remaining"),
            "exceeded": q.get("exceeded", False),
        }
    except Exception:
        return {"included_chars": 0, "used_chars": 0,
                "remaining_chars": None, "exceeded": False}


def build_membership_snapshot(config: dict, user_store=None) -> Dict[str, Any]:
    """会员中心单一数据装配（页面与 API 共用；任何一段失败都不拖垮整体）。"""
    from src.licensing import get_license_manager
    from src.licensing.feature_gate import gate_snapshot

    out: Dict[str, Any] = {"ok": True, "ts": int(time.time())}
    try:
        st = get_license_manager().status()
        lic = st.to_dict()
    except Exception:
        st, lic = None, {"state": "unavailable", "licensed": False,
                         "plan": "community"}
    out["license"] = lic
    try:
        out["gate"] = gate_snapshot(config, st)
    except Exception:
        out["gate"] = {"enabled": False, "plan": "community", "features": {},
                       "locked": [], "plan_order": []}
    out["quota"] = _quota_snapshot()
    seats_used = None
    try:
        if user_store is not None:
            seats_used = int(user_store.user_count())
    except Exception:
        seats_used = None
    out["seats"] = {
        "used": seats_used,
        "limit": int(lic.get("seats") or 0),
    }
    # 渲染友好矩阵（模板零逻辑）：每功能一行，每档一格 bool
    try:
        from src.licensing.feature_gate import plan_rank

        gate = out["gate"]
        plans = list(gate.get("plan_order") or [])
        rows = []
        for name, meta in (gate.get("features") or {}).items():
            mp = str(meta.get("min_plan") or "")
            rows.append({
                "name": name,
                "min_plan": mp,
                "allowed": bool(meta.get("allowed", True)),
                "cells": [plan_rank(p) >= plan_rank(mp) for p in plans],
            })
        rows.sort(key=lambda r: (plan_rank(r["min_plan"]), r["name"]))
        out["matrix"] = {"plans": plans, "rows": rows}
    except Exception:
        out["matrix"] = {"plans": [], "rows": []}
    return out


def register_membership_routes(app, *, templates, page_auth, api_auth,
                               config_manager, user_store=None) -> None:
    def _cfg() -> dict:
        return getattr(config_manager, "config", None) or {}

    @app.get("/api/admin/membership")
    async def api_admin_membership(request: Request):
        api_auth(request)
        return build_membership_snapshot(_cfg(), user_store)

    @app.get("/membership", response_class=HTMLResponse)
    async def membership_page(request: Request, _=Depends(page_auth)):
        snap = build_membership_snapshot(_cfg(), user_store)
        return templates.TemplateResponse(request, "membership.html", {
            "mb": snap,
        })
