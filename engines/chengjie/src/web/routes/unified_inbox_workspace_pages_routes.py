"""统一收件箱——坐席工作台 HTML 页面壳路由域（巨石拆分 slice 17 + slice 38a）。

把统一收件箱/工作台相关 HTML 页面从 ``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_pages_routes(app, *, page_auth, templates, config_manager)``：

- slice 38a：主工作台 ``/workspace``（unified_inbox.html）+ 旧入口 ``/unified-inbox`` redirect
- slice 17：``/workspace/contacts|tasks|dash|escalations`` 四个子页面壳

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from starlette.responses import RedirectResponse

from src.integrations.platform_session_health import UNHEALTHY_STATUSES
from src.web.routes.unified_inbox_auth import _is_supervisor

logger = logging.getLogger(__name__)


# ── 渠道中心壳层「网页会话健康条」（P0）──────────────────────────────────────
# messenger-web / whatsapp-baileys 两类 Node worker 会把会话状态 push 进
# platform_session_health 登记表（telegram/line 无外部 worker，不在其中）。
# 本函数把 dump() 快照过滤/整形成单一平台的会话行，供
# GET /api/workspace/channel-sessions 出数——模块级纯函数，便于独立测试。
def summarize_channel_sessions(
    dump: Dict[str, Any], platform: str, *, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """把 ``PlatformSessionHealth.dump()`` 整形成指定平台的会话列表。

    输出行 ``{key, account_id, login_id, status, unhealthy, age_sec}``：
    - ``age_sec``：不健康会话＝掉线时长（``unhealthy_since`` 起算，缺则退回
      最近上报时刻 ``ts``）；健康会话＝距最近一次状态上报的秒数；
    - 登记表键形如 ``platform:account_id``（account_id 可含冒号，按首个冒号切）；
    - 脏条目（值非 dict / 键无冒号 / 时间戳非数值）一律跳过，绝不抛。
    """
    ts_now = time.time() if now is None else float(now)
    plat = str(platform or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    sessions = (dump or {}).get("sessions")
    if not plat or not isinstance(sessions, dict):
        return rows
    for key in sorted(sessions):
        sess = sessions.get(key)
        if not isinstance(sess, dict):
            continue
        k = str(key)
        k_plat, _, k_acct = k.partition(":")
        if k_plat != plat or not k_acct:
            continue
        status = str(sess.get("status") or "unknown")
        unhealthy = status in UNHEALTHY_STATUSES
        try:
            if unhealthy:
                since = (float(sess.get("unhealthy_since") or 0.0)
                         or float(sess.get("ts") or 0.0))
            else:
                since = float(sess.get("ts") or 0.0)
        except (TypeError, ValueError):
            since = 0.0
        age_sec = int(max(0.0, ts_now - since)) if since > 0 else 0
        rows.append({
            "key": k,
            "account_id": k_acct,
            "login_id": str(sess.get("login_id") or ""),
            "status": status,
            "unhealthy": unhealthy,
            "age_sec": age_sec,
        })
    return rows


def register_workspace_pages_routes(
    app, *, page_auth, templates, config_manager=None,
) -> None:
    """挂载工作台 HTML 页面（主入口 + 子页面壳）。"""

    # P5-2：「成交/完成」阶段单一来源 → 注入工作台各页 JS（收件箱 done 筛选 / KPI /
    # 看板已成交卡片同口径，防前后端漂移）。排序保证 tojson 输出稳定、测试可断言。
    try:
        from src.contacts.models import (
            FUNNEL_DONE_STAGES as _FUNNEL_DONE_STAGES,
            WON_STAGES as _WON_STAGES,
        )
        _funnel_done_sorted = sorted(_FUNNEL_DONE_STAGES)
        _won_sorted = sorted(_WON_STAGES)
    except Exception:  # 极端 import 失败也不阻断页面渲染
        # stage-source-allow: 仅为 models import 失败时的兜底默认，非成交判定逻辑（P5-2c 扫描门禁豁免）
        _funnel_done_sorted = ["BONDED", "CONVERTED", "LINE_ACCEPTED", "LINE_ENGAGED"]  # stage-source-allow
        _won_sorted = ["BONDED", "CONVERTED"]  # stage-source-allow

    def _page_ctx(request: Request) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
            "funnel_done_stages": _funnel_done_sorted,
            "won_stages": _won_sorted,
        }
        # P1-2：主管标记（收件箱「AI 值守」姿态开关等主管专属控件的模板级门槛）。
        ctx["is_supervisor"] = _is_supervisor(request)
        # P3：账号手机号显示脱敏开关（治理化，默认脱敏=True；演示/隐私可控）
        ctx["mask_account_phone"] = True
        # P0-5 可见化：平台自动化降级/暂停状态注入页面，收件箱顶部渲染提示条
        # （坐席能看见「Messenger 已降为人审」，不再疑惑为何不自动回）。
        ctx["platform_mode_caps"] = {}
        ctx["platform_draft_skips"] = []
        try:
            if config_manager is not None:
                _cfg = config_manager.config or {}
                _wa = _cfg.get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
                _sp = (_cfg.get("accounts", {}) or {}).get("self_profile", {}) or {}
                if "mask_phone" in _sp:
                    ctx["mask_account_phone"] = bool(_sp.get("mask_phone"))
                _ad = (_cfg.get("inbox", {}) or {}).get("auto_draft", {}) or {}
                ctx["platform_mode_caps"] = {
                    str(k).lower(): str(v).lower()
                    for k, v in (_ad.get("platform_modes") or {}).items()
                    if str(v or "").strip()
                }
                ctx["platform_draft_skips"] = sorted(
                    str(x).lower() for x in (_ad.get("skip_platforms") or []))
        except Exception:
            pass
        # P0-1 A3：token 直登（桌面默认 admin）绕过 /setup 时 AI Key 仍为空/占位 →
        # 工作台顶部出可关闭引导条（深链 /workspace/setup#ai）。仅主管可见（能修的人才看到）。
        # 托管试用（AITR_HOSTED_AI_KEY）或备用池有真 Key → 不算缺失；试用条单独提示额度。
        ctx["ai_key_missing"] = False
        ctx["ai_trial_mode"] = False
        try:
            if config_manager is not None and _is_supervisor(request):
                from src.utils.golive import _is_placeholder
                _ai = (config_manager.config or {}).get("ai") or {}
                _pool = _ai.get("key_pool") if isinstance(_ai.get("key_pool"), dict) else {}
                _pool_keys = [
                    k for k in (_pool.get("keys") or [])
                    if isinstance(k, dict) and not _is_placeholder(k.get("api_key"))
                ]
                _hosted = bool(_ai.get("_hosted_trial")) or bool(
                    (os.environ.get("AITR_HOSTED_AI_KEY") or "").strip())
                _missing = _is_placeholder(_ai.get("api_key")) and not _pool_keys
                ctx["ai_key_missing"] = _missing
                ctx["ai_trial_mode"] = (not _missing) and _hosted
        except Exception:
            pass
        return ctx

    @app.get("/workspace", response_class=HTMLResponse)
    async def workspace_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "unified_inbox.html", _page_ctx(request))

    @app.get("/unified-inbox")
    async def unified_inbox_redirect(request: Request, _=Depends(page_auth)):
        """旧入口：保留并 307→ 新独立工作台 /workspace。"""
        return RedirectResponse("/workspace", status_code=307)

    @app.get("/workspace/contacts", response_class=HTMLResponse)
    async def workspace_contacts_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "contacts_list.html", _page_ctx(request))

    @app.get("/workspace/tasks", response_class=HTMLResponse)
    async def workspace_tasks_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "tasks.html", _page_ctx(request))

    @app.get("/workspace/dash", response_class=HTMLResponse)
    async def workspace_dash_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "workspace_dashboard.html", _page_ctx(request))

    @app.get("/workspace/escalations", response_class=HTMLResponse)
    async def workspace_escalations_page(request: Request, _=Depends(page_auth)):
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "escalation_log.html", _page_ctx(request))

    @app.get("/workspace/roi", response_class=HTMLResponse)
    async def workspace_roi_page(request: Request, _=Depends(page_auth)):
        # P0-3：老板视角 ROI 门面（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "workspace_roi.html", _page_ctx(request))

    @app.get("/workspace/setup", response_class=HTMLResponse)
    async def workspace_setup_page(request: Request, _=Depends(page_auth)):
        # P1-1：渠道接入向导（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "setup_wizard.html", _page_ctx(request))

    # ── 渠道中心（多渠道设置融合，主管专属）─────────────────────────
    # 旧管理后台四页（/telegram /line-rpa /messenger-rpa /whatsapp-rpa）整体迁入
    # 工作台壳：正文 partial =_channel_body_<ch>.html，观感由 workspace_channels.html
    # 的「变量桥」统一；旧路径 302 到这里（书签不断）。
    #
    # discord 是第五个渠道，且与前四个**不同族**：官方 Bot Token 接入（无扫码/无真机
    # 设备/无 UI 自动化），能力面见 _channel_body_discord.html。它是默认关的子系统，
    # 但 tab 仍然常驻——正文首屏就是「接入就绪自检」，未启用时如实说「开关没开/缺
    # discord.py/缺 Token」并给处置。把 tab 藏起来反而会让运营找不到开它的地方。
    _CHANNEL_KEYS = ("telegram", "line", "messenger", "whatsapp", "discord")

    @app.get("/workspace/channels", response_class=HTMLResponse)
    async def workspace_channels_root(request: Request, _=Depends(page_auth)):
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        q = request.url.query
        return RedirectResponse(
            "/workspace/channels/telegram" + (f"?{q}" if q else ""), status_code=307
        )

    @app.get("/workspace/channels/{channel}", response_class=HTMLResponse)
    async def workspace_channels_page(
        channel: str, request: Request, _=Depends(page_auth),
    ):
        if channel not in _CHANNEL_KEYS:
            return RedirectResponse("/workspace/channels/telegram", status_code=307)
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        ctx = _page_ctx(request)
        ctx["channel"] = channel
        return templates.TemplateResponse(request, "workspace_channels.html", ctx)

    @app.get("/api/workspace/channel-sessions")
    async def api_workspace_channel_sessions(
        request: Request, platform: str = "", _=Depends(page_auth),
    ):
        """渠道中心壳层「网页会话健康条」数据源（P0）。

        读 platform_session_health 登记表（messenger-web / whatsapp-baileys 两类
        Node worker push 的会话状态），按 ``platform`` 过滤整形；telegram/line 无
        外部 worker → 恒空列表（前端据此隐藏健康条）。``relogin_supported`` 标记
        该平台是否支持既有 ``POST /api/admin/platform-sessions/relogin`` 一键重登
        （当前仅 messenger；whatsapp 走坐席账号面板重新扫码）。

        观测旁路：任何异常回 ``{ok:false}``，绝不 500；响应为纯数据字段
        （无人话文案，措辞由前端 i18n 渲染）。
        """
        plat = str(platform or "").strip().lower()
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            snap = get_platform_session_health().dump()
            return {
                "ok": True,
                "platform": plat,
                "sessions": summarize_channel_sessions(snap, plat),
                "relogin_supported": plat == "messenger",
            }
        except Exception:
            logger.debug("channel-sessions 快照读取失败（已忽略）", exc_info=True)
            return {"ok": False, "platform": plat, "sessions": [],
                    "relogin_supported": False}

    @app.get("/workspace/kb-start", response_class=HTMLResponse)
    async def workspace_kb_start_page(request: Request, _=Depends(page_auth)):
        # P1-2：知识库冷启动向导（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "kb_cold_start.html", _page_ctx(request))

    @app.get("/workspace/golive", response_class=HTMLResponse)
    async def workspace_golive_page(request: Request, _=Depends(page_auth)):
        # P2-1：上线自检清单（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "golive_checklist.html", _page_ctx(request))

    @app.get("/workspace/ai-quality", response_class=HTMLResponse)
    async def workspace_ai_quality_page(request: Request, _=Depends(page_auth)):
        # P3-1：AI 回复质量闭环看板（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "ai_quality.html", _page_ctx(request))

    @app.get("/workspace/usage", response_class=HTMLResponse)
    async def workspace_usage_page(request: Request, _=Depends(page_auth)):
        # C0-2：用量计量看板（主管/老板专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "workspace_usage.html", _page_ctx(request))
