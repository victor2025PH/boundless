# -*- coding: utf-8 -*-
"""老板日报 /workspace/boss（WP-3 2026-08-17）。

ops-overview 是工程师视角（几十张卡）；本页给运营主管/老板一屏「AI 今天/本周
替你干了什么」——大数字 + 7 天趋势 + 一句话解读，词汇只用钱和时间，
**零工程黑话**（草稿/L2/autosend 不进这页——页面文案全走 i18n pack ``bp_*``，
服务端 text_lines 含「拟稿」等运维词，只进导出/推送，不进页面渲染）。

端点：
  GET /workspace/boss              页面（任意登录角色可读——「老板也是坐席角色」，
                                   路径在 /workspace 下，agent 角色天然可达）
  GET /api/workspace/boss-value    日账+周账+7日趋势+省时折算（300s TTL 缓存，
                                   防多人开页反复全扫消息表——与 reply_latency
                                   同款缓存刻度）
  GET /api/workspace/boss-export.md 本周报告 Markdown 导出（WS-3 案例采写直接用；
                                   运维面允许 text_lines 原词）

数据口径全在 ``src/ops/value_report.py``（持久库、重启不清零；日窗=滚动 24h，
7 日窗平铺==周窗 的守恒关系被 tests 钉住）。省时系数
``ops.value_report.manual_minutes_per_reply``（默认 3 分钟/条，客户可按团队实情调）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 300.0


def _build_payload(store: Any, config: Dict[str, Any],
                   now: Optional[float] = None) -> Dict[str, Any]:
    """聚合日账/周账/趋势/省时（纯装配；store 缺失由调用方兜）。"""
    from src.ops.value_report import (
        build_daily_value,
        build_weekly_value,
        daily_series,
        estimate_saved_minutes,
        resolve_minutes_per_reply,
    )

    t = float(now if now is not None else time.time())
    daily = build_daily_value(store, now=t)
    weekly = build_weekly_value(store, now=t)
    series = daily_series(store, days=7, now=t)
    mpr = resolve_minutes_per_reply(config)
    saved_today = estimate_saved_minutes(
        (daily or {}).get("today") or {}, mpr)
    saved_week = estimate_saved_minutes(
        (weekly or {}).get("this_week") or {}, mpr)
    return {
        "ok": True,
        "generated_at": t,
        "daily": daily,
        "weekly": weekly,
        "series": series,
        "saved": {
            "minutes_per_reply": mpr,
            "today_minutes": saved_today,
            "week_minutes": saved_week,
        },
    }


def _export_markdown(payload: Dict[str, Any]) -> str:
    """本周报告 Markdown（案例采写素材；允许 text_lines 运维原词）。"""
    t = float(payload.get("generated_at") or time.time())
    d0 = time.strftime("%Y-%m-%d", time.localtime(t - 7 * 86400))
    d1 = time.strftime("%Y-%m-%d", time.localtime(t))
    weekly = payload.get("weekly") or {}
    tw = weekly.get("this_week") or {}
    lw = weekly.get("last_week") or {}
    saved = payload.get("saved") or {}
    lines = [
        f"# AI 价值周报（{d0} ～ {d1}）",
        "",
        "## 本周摘要",
    ]
    for ln in (weekly.get("text_lines") or []):
        lines.append(f"- {ln}")
    if not (weekly.get("text_lines") or []):
        lines.append("- 本周暂无数据")
    lines += [
        "",
        "## 关键数字（本周 / 上周）",
        "",
        "| 指标 | 本周 | 上周 |",
        "|---|---|---|",
    ]

    def _n(seg: Dict[str, Any], *path) -> Any:
        cur: Any = seg
        for p in path:
            cur = (cur or {}).get(p) if isinstance(cur, dict) else None
        return cur if cur is not None else 0

    lines.append(f"| AI 写好的回复 | {_n(tw, 'drafts', 'created')} "
                 f"| {_n(lw, 'drafts', 'created')} |")
    lines.append(f"| AI 发出的回复 | {_n(tw, 'drafts', 'sent')} "
                 f"| {_n(lw, 'drafts', 'sent')} |")
    lines.append(f"| 主动问候 | {_n(tw, 'outreach', 'sent')} "
                 f"| {_n(lw, 'outreach', 'sent')} |")
    lines.append(f"| 问候获回应 | {_n(tw, 'outreach', 'responded')} "
                 f"| {_n(lw, 'outreach', 'responded')} |")
    lines.append(f"| 发出消息总量 | {_n(tw, 'traffic', 'messages_out')} "
                 f"| {_n(lw, 'traffic', 'messages_out')} |")
    lines.append(f"| 收到客户消息 | {_n(tw, 'traffic', 'messages_in')} "
                 f"| {_n(lw, 'traffic', 'messages_in')} |")
    mpr = saved.get("minutes_per_reply") or 3
    wk_min = float(saved.get("week_minutes") or 0)
    lines += [
        "",
        f"折算省下人工：约 **{round(wk_min / 60.0, 1)} 小时**"
        f"（按每条回复 {mpr} 分钟估算；系数可在配置"
        f" `ops.value_report.manual_minutes_per_reply` 按团队实情调整）",
        "",
        "> 数据来源：实例持久库（重启不清零）；口径详见 docs/实施35。",
    ]
    return "\n".join(lines) + "\n"


def register_boss_routes(app, *, page_auth, api_auth, templates,
                         config_manager=None) -> None:
    """挂载老板日报页与数据端点。"""
    _cache: Dict[str, Any] = {"ts": 0.0, "data": None}

    def _cfg() -> Dict[str, Any]:
        return getattr(config_manager, "config", None) or {}

    def _ctx(request: Request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": (
                sess.get("display_name") or sess.get("username") or ""),
        }
        try:
            _wa = (_cfg().get("web_admin") or {})
            if _wa.get("site_name"):
                ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    def _payload(request: Request, *, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if (not force and _cache["data"] is not None
                and now - float(_cache["ts"] or 0) < _CACHE_TTL_S):
            return _cache["data"]
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            return {"ok": False, "available": False}
        data = _build_payload(store, _cfg(), now=now)
        _cache["ts"] = now
        _cache["data"] = data
        return data

    @app.get("/workspace/boss", response_class=HTMLResponse)
    async def workspace_boss_page(request: Request, _=Depends(page_auth)):
        """老板日报页（任意登录角色可读；数据走 /api/workspace/boss-value）。"""
        return templates.TemplateResponse(request, "boss.html", _ctx(request))

    @app.get("/api/workspace/boss-value")
    async def api_boss_value(request: Request, force: int = 0,
                             _=Depends(api_auth)):
        return _payload(request, force=bool(force))

    @app.get("/api/workspace/boss-export.md")
    async def api_boss_export_md(request: Request, _=Depends(api_auth)):
        payload = _payload(request)
        if not payload.get("ok"):
            return PlainTextResponse("# AI 价值周报\n\n暂无数据（实例存储未就绪）。\n",
                                     media_type="text/markdown; charset=utf-8")
        fname = "chatx_weekly_value_" + time.strftime("%Y%m%d") + ".md"
        return PlainTextResponse(
            _export_markdown(payload),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    logger.info("老板日报页已注册（/workspace/boss）")
