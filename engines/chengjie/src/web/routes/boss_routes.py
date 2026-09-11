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


def _build_now(store: Any, config: Dict[str, Any], notifier: Any = None,
               now: Optional[float] = None) -> Dict[str, Any]:
    """「现在」区（P1-8 2026-08-29）：钱 / 单 / 等 / 险——日报页此前全是
    回顾性数字，老板看不到「此刻有没有事等我」。四块全部复用既有单口径
    函数 + 进程单例 peek（绝不新建库），逐段软失败——缺段＝前端隐藏对应
    格子，绝不摆假零。"""
    t = float(now if now is not None else time.time())
    out: Dict[str, Any] = {}
    # 单 + 钱：营销目标（peek 既有单例——新建会凭空造 :memory: 空库把
    # 「零目标」误报成事实，与 value_report 同纪律；未启用=段缺失）
    try:
        from src.companion.goals.store import peek_goal_store
        gs = peek_goal_store()
        if gs is not None:
            oc = gs.outcome_counts(t - 86400.0, t)
            summ = gs.summary() or {}
            out["goals"] = {
                "active": int((summ.get("by_status") or {}).get("active") or 0),
                "won_24h": int(oc.get("won") or 0),
                "won_amount_24h": float(oc.get("won_amount") or 0.0),
                "done_24h": int(oc.get("done") or 0),
            }
    except Exception:
        logger.debug("boss now: goals section skipped", exc_info=True)
    waiting: Dict[str, Any] = {}
    try:
        # 等 ①：还没人拍板的回复（页面词条只说人话，不出工程词）
        with store._lock:  # noqa: SLF001 —— 与 value_report 读法同惯例
            pend = store._conn.execute(
                "SELECT COUNT(*) FROM reply_drafts"
                " WHERE status IN ('pending','enriching')").fetchone()[0]
        waiting["replies_pending"] = int(pend)
    except Exception:
        logger.debug("boss now: pending section skipped", exc_info=True)
    try:
        # 等 ②：24h 没回上话的客户（reply_latency 同一口径；自带 300s TTL）
        from src.ops.reply_latency import reply_latency_snapshot
        d1 = (reply_latency_snapshot(store, now=t) or {}).get("d1") or {}
        waiting["unanswered_24h"] = int(d1.get("unanswered") or 0)
    except Exception:
        logger.debug("boss now: latency section skipped", exc_info=True)
    if waiting:
        out["waiting"] = waiting
    risk: Dict[str, Any] = {}
    try:
        # 险 ①：渠道账号掉线数（进程登记表，零 IO）
        from src.integrations.platform_session_health import (
            get_platform_session_health,
        )
        risk["sessions_down"] = len(
            get_platform_session_health().unhealthy_sessions())
    except Exception:
        logger.debug("boss now: sessions section skipped", exc_info=True)
    try:
        # 险 ①b：被安全冻结的账号面（kill_switch 生效作用域数；fail-open=0 项）
        from src.ops.kill_switch import status_snapshot
        risk["frozen"] = len(status_snapshot())
    except Exception:
        logger.debug("boss now: frozen section skipped", exc_info=True)
    try:
        # 险 ②：告警通道 verdict（healthy 之外都该让老板知道「出事没人收通知」）
        from src.integrations.alert_link_status import (
            collect_alert_link_status,
        )
        risk["alert_link"] = str(
            (collect_alert_link_status(config, notifier) or {})
            .get("verdict") or "")
    except Exception:
        logger.debug("boss now: alert link section skipped", exc_info=True)
    if risk:
        out["risk"] = risk
    return out


def _build_payload(store: Any, config: Dict[str, Any],
                   notifier: Any = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """聚合日账/周账/趋势/省时/现在（纯装配；store 缺失由调用方兜）。"""
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
    out = {
        "ok": True,
        "generated_at": t,
        "daily": daily,
        "weekly": weekly,
        "series": series,
        "now": _build_now(store, config, notifier, now=t),
        "saved": {
            "minutes_per_reply": mpr,
            "today_minutes": saved_today,
            "week_minutes": saved_week,
        },
    }
    # 2026-09-08 成本对账 P2：老板日报加「AI 花费」段（今天/本月/昨日对账灯/余额跑道）。
    # 账本缺席 → 段缺失，前端隐藏卡片，绝不摆假零。
    try:
        from src.ai.cost_ledger import get_cost_ledger
        from src.web.routes.cost_routes import build_summary
        cs = build_summary(get_cost_ledger(), config, days=7, now=t)
        if cs.get("available"):
            lr = cs.get("last_recon") or {}
            out["cost"] = {
                "provider": cs["provider"], "currency": cs["currency"],
                "today": cs["today"]["cost"], "month": cs["month_total"],
                "avg7": cs["avg7"], "balance": cs["balance"], "runway_days": cs["runway_days"],
                "recon_light": lr.get("light") or (
                    "✅" if lr.get("verdict") == "ok" else "❌" if lr.get("verdict") == "mismatch"
                    else "⚪"),
                "recon_day": lr.get("day") or "",
                "recon_verdict": lr.get("verdict") or "",
                "issues": [r.get("message") for r in (lr.get("reasons") or [])
                           if r.get("level") in ("warn", "crit")][:3],
                "top": [{"label": p["label"], "cost": p["cost"]}
                        for p in cs["today"]["purposes"][:3]],
                "pricing_configured": bool(cs.get("pricing_configured")),
            }
    except Exception:
        logger.debug("boss: cost section skipped", exc_info=True)
    return out


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
        notifier = getattr(request.app.state, "webhook_notifier", None)
        data = _build_payload(store, _cfg(), notifier, now=now)
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
