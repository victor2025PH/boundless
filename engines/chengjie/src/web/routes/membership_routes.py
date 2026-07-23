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
            "included_base": q.get("included_base", q.get("included", 0)),
            "topup_chars": q.get("topup_chars", 0),
            "used_chars": q.get("used", 0),
            "remaining_chars": q.get("remaining"),
            "exceeded": q.get("exceeded", False),
        }
    except Exception:
        return {"included_chars": 0, "included_base": 0, "topup_chars": 0,
                "used_chars": 0, "remaining_chars": None, "exceeded": False}


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
    # 购买/续费入口（P4b）：运营配置 licensing.shop_url（商城/联系页/TG 客服链接
    # 均可），空 = 不渲染 CTA（自营/内网部署零变化）。纯透传，不做校验。
    try:
        shop_url = str(((config.get("licensing") or {}).get("shop_url")) or "")
    except Exception:
        shop_url = ""
    out["shop"] = {"url": shop_url}
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

    @app.post("/api/admin/license/topup")
    async def api_license_topup(request: Request, payload: dict):
        """字符加量包入账（charpack 履约通道，P4b）。

        body: {chars:int, ref:str, note?:str}。ref=订单号（幂等主键，重复拒绝）。
        鉴权与 license activate 同口径（api_auth；坐席被全局白名单拦）。
        当前为手工/半自动通道——fulfillment watcher 的 charpack 自动接线属下阶段。
        """
        from src.web.web_i18n import tr

        api_auth(request)
        from src.licensing.quota_store import add_license_topup

        res = add_license_topup(
            int(payload.get("chars") or 0),
            str(payload.get("ref") or ""),
            str(payload.get("note") or ""),
        )
        if not res.get("ok"):
            err = str(res.get("error") or "internal")
            res["detail"] = tr(request, f"err.lic.topup_{err}",
                               f"topup failed: {err}")
        return res

    @app.post("/api/admin/license/topup-voucher")
    async def api_license_topup_voucher(request: Request, payload: dict):
        """兑换字符加量凭证（P4c 自动履约闭环的客户侧终点）。

        body: {voucher:str}——厂商签发的 topup token（官网私信送达/人工交付均可）。
        验签+绑定校验+入账全在 ``topup_voucher.redeem_topup_voucher``；本路由只做
        鉴权与 detail 文案。错误码全集见该函数 docstring（i18n 键 err.lic.voucher_*
        与 topup_* 两族，duplicate_ref/unlimited 等入账层错误复用 topup_* 族）。
        """
        from src.web.web_i18n import tr

        api_auth(request)
        from src.licensing.topup_voucher import redeem_topup_voucher

        res = redeem_topup_voucher(str(payload.get("voucher") or ""))
        if not res.get("ok"):
            err = str(res.get("error") or "internal")
            fam = ("voucher" if err in (
                "bad_signature", "not_voucher", "malformed",
                "lic_mismatch", "customer_mismatch", "unavailable") else "topup")
            res["detail"] = tr(request, f"err.lic.{fam}_{err}",
                               f"redeem failed: {err}")
        return res

    @app.get("/membership", response_class=HTMLResponse)
    async def membership_page(request: Request, _=Depends(page_auth)):
        snap = build_membership_snapshot(_cfg(), user_store)
        return templates.TemplateResponse(request, "membership.html", {
            "mb": snap,
        })
