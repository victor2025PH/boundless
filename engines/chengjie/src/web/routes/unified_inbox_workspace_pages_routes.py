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
from pathlib import Path
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
        # P1-5（2026-08-12）：统一 App 右栏灰度默认档——overlay 置
        # inbox.copilot.app_default: true 即全员默认加载统一 App 右栏（与桌面壳同源）。
        # 个人 🧪 手动选择（localStorage）压过运营默认；?app=0 深链是灰度期排障回退口。
        # 缺省 False＝现状不变；机制先行，开闸是运营决策（读数见 ui-event cpapp_* 埋点）。
        ctx["copilot_app_default"] = False
        # P0 账号顶部状态栏 Account Dock（2026-08-16 老板指名）：视图层缺省=开——模板对
        # ctx 缺席也按开渲染（模板热更先于重启的中间态可用）；overlay
        # inbox.account_dock.enabled: false 可整体关闭（随重启生效）。
        ctx["account_dock_enabled"] = True
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
                _cp = (_cfg.get("inbox", {}) or {}).get("copilot", {}) or {}
                ctx["copilot_app_default"] = bool(_cp.get("app_default"))
                _adk = (_cfg.get("inbox", {}) or {}).get("account_dock", {}) or {}
                if "enabled" in _adk:
                    ctx["account_dock_enabled"] = bool(_adk.get("enabled"))
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
                # 付费托管租户（licensing.hosted_ai.enabled）与免费试用走同一条
                # 网关注入链（_hosted_trial 同为 True），但横幅措辞必须分开——
                # 付费客户看到「免费试用中」是错误且失礼的（P5，2026-08-08）
                _lic = (config_manager.config or {}).get("licensing") or {}
                _hai = _lic.get("hosted_ai") if isinstance(_lic.get("hosted_ai"), dict) else {}
                ctx["ai_hosted_paid"] = bool(_hai.get("enabled")) and ctx["ai_trial_mode"]
        except Exception:
            pass
        return ctx

    @app.get("/workspace", response_class=HTMLResponse)
    async def workspace_page(request: Request, _=Depends(page_auth)):
        # 首启向导 303 闸门已删除（2026-08-31 新手引导退役）：/workspace 无条件可达。
        # 历史教训（勿复活）：welcome_pending 闸门曾让「向导未完成」的新手从侧栏
        # 永远进不了工作台——核心页面不得被任何 feature flag 挡路。
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

    @app.get("/workspace/cockpit", response_class=HTMLResponse)
    async def workspace_cockpit_page(request: Request, _=Depends(page_auth)):
        # 驾驶舱（cockpit P1 2026-08-13）：介入优先级队列 + 接管在场 + 账号健康。
        # 与 /workspace/dash 分工：dash=看数（经营/绩效），cockpit=行动（现在该管谁）。
        return templates.TemplateResponse(request, "cockpit.html", _page_ctx(request))

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

    # ── 渠道中心（四渠道设置融合，主管专属）─────────────────────────
    # 旧管理后台四页（/telegram /line-rpa /messenger-rpa /whatsapp-rpa）整体迁入
    # 工作台壳：正文 partial =_channel_body_<ch>.html，观感由 workspace_channels.html
    # 的「变量桥」统一；旧路径 302 到这里（书签不断）。
    _CHANNEL_KEYS = ("telegram", "line", "messenger", "whatsapp")

    # ── P3 平台能力速览（SSOT=docs/平台能力矩阵.md，由 scripts/platform_matrix 从代码
    #    生成、门禁钉住与代码一致；此处只读解析生成物，绝不在请求时构造 worker）。
    #    路径必须 __file__ 锚定：服务进程 CWD=实例数据根，docs/ 在引擎代码根
    #    （CWD 相对路径=迁移后静默失真，见 test_static_asset_paths 家族教训）。
    _MATRIX_DOC = Path(__file__).resolve().parents[3] / "docs" / "平台能力矩阵.md"
    _MATRIX_ROW_BY_CHANNEL = {
        "telegram": "Telegram（protocol）",
        "line": "LINE（protocol）",
        "messenger": "Messenger（web）",
        "whatsapp": "WhatsApp（protocol）",
    }
    _matrix_cache: Dict[str, Any] = {"mtime": None, "rows": {}}

    def _cap_state(cell: str) -> str:
        """矩阵格值 → ASCII 状态词（模板只比对状态词，避免 CJK 判定散进模板）。"""
        v = (cell or "").strip()
        if v == "Y":
            return "yes"
        if v == "-":
            return "no"
        if v == "开关":
            return "toggle"
        return "unknown"

    def _platform_capability(channel: str) -> Optional[Dict[str, str]]:
        """渠道页能力速览：{send_text, send_media, read_receipt, typing, inbound_media}，
        值 ∈ {"yes","no","toggle","unknown"}；文档缺失/解析失败 → None（速览条整体隐藏）。"""
        try:
            mt = _MATRIX_DOC.stat().st_mtime
            if _matrix_cache["mtime"] != mt:
                rows: Dict[str, Dict[str, str]] = {}
                for line in _MATRIX_DOC.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s.startswith("|") or "---" in s:
                        continue
                    cells = [c.strip() for c in s.strip("|").split("|")]
                    if len(cells) >= 6:
                        rows[cells[0]] = {
                            "send_text": _cap_state(cells[1]),
                            "send_media": _cap_state(cells[2]),
                            "read_receipt": _cap_state(cells[3]),
                            "typing": _cap_state(cells[4]),
                            "inbound_media": _cap_state(cells[5]),
                        }
                _matrix_cache["mtime"] = mt
                _matrix_cache["rows"] = rows
            row = _matrix_cache["rows"].get(
                _MATRIX_ROW_BY_CHANNEL.get(channel, ""))
            return dict(row) if row else None
        except Exception:
            return None

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
        # P3 能力速览（缺席=None → 模板整条隐藏；把「平台限制」显式化防误报故障）
        ctx["platform_capability"] = _platform_capability(channel)
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
        # C0-2 → 一页两态（2026-08-16）：主管看团队全量视图；坐席/观察员不再 307
        # 弹走，改看「我的用量」自视图——个人字符额度是坐席的切身信息，藏着只会
        # 变成「额度快用完了却没人告诉我」。视图分叉由模板按 usage_self_only 渲染。
        ctx = _page_ctx(request)
        ctx["usage_self_only"] = not _is_supervisor(request)
        return templates.TemplateResponse(request, "workspace_usage.html", ctx)
