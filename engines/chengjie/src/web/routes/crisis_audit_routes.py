"""R9b 危机事件审计 API 路由（从 R9 的 CrisisEventStore 暴露给值守人员）。

把 R9 落库的危机事件变成真人**可操作**的工作台：列未处理危机、标记已处置。
依赖经 AdminRouteContext 注入；读用 api_auth，写（标记处置）用 manage_ops 权限
（与"确认/指派运维事件"同级）。行为与既有审计类路由一致。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.web.crisis_audit")

# #185（2026-09-05）：空态「留痕已开，近 N 天无事件」的观察窗——与页面文案 ca_js025 的 {n} 同源。
AUDIT_EMPTY_WINDOW_DAYS = 30

# 一键开启只允许写这两把（白名单——不做通用 overlay 写口）。
_ENABLE_FLAGS = (
    "companion.wellbeing.crisis_audit",
    "companion.wellbeing.crisis_escalation",
)


def wellbeing_switches(config: Any) -> Dict[str, bool]:
    """从合并后的 config 读安全链三开关（纯函数，路由与测试同源）。

    口径与 ``skill_manager._maybe_escalate_crisis`` 完全一致：``wellbeing.enabled`` 默认开，
    ``crisis_audit`` / ``crisis_escalation`` 默认关；config 不是 dict 一律按「未知=关」——
    宁可让页面亮红条提示去开，也不能把「读不到配置」渲染成「没事」。
    """
    cfg = config if isinstance(config, dict) else {}
    wb = ((cfg.get("companion") or {}).get("wellbeing") or {}) if isinstance(cfg, dict) else {}
    if not isinstance(wb, dict):
        wb = {}
    enabled = bool(wb.get("enabled", True))
    return {
        "wellbeing_enabled": enabled,
        "audit_on": bool(enabled and wb.get("crisis_audit", False)),
        "escalation_on": bool(enabled and wb.get("crisis_escalation", False)),
    }


def audit_page_state(*, audit_on: bool, count_in_window: int, total_count: int) -> str:
    """三档空态（纯函数）：audit_off / audit_on_empty / has_events。

    ``has_events`` 以**全表**有行为准（哪怕留痕现在关着，历史事件也要能看见）；
    ``audit_on_empty`` 要求留痕开着且窗口内 0 条。
    """
    if total_count > 0:
        return "has_events"
    if not audit_on:
        return "audit_off"
    return "audit_on_empty" if count_in_window <= 0 else "has_events"


def register_crisis_audit_routes(app, ctx) -> None:
    """挂载 /api/crisis-events* 到 app。"""
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    def _sm(request):
        from src.web.web_context import resolve_skill_manager
        sm = resolve_skill_manager(telegram_client, app)
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        return sm

    def _config_manager():
        cm = getattr(app.state, "config_manager", None)
        if cm is not None and isinstance(getattr(cm, "config", None), dict):
            return cm
        return None

    def _live_config(sm) -> Optional[Dict[str, Any]]:
        cm = _config_manager()
        if cm is not None:
            return cm.config
        sm_cfg = getattr(sm, "config", None)
        inner = getattr(sm_cfg, "config", None)
        return inner if isinstance(inner, dict) else None

    def _count_since(sm, since_ts: float) -> int:
        store = getattr(sm, "_crisis_store", None)
        fn = getattr(store, "count_since", None)
        if not callable(fn):
            return 0
        try:
            return int(fn(since_ts) or 0)
        except Exception:
            return 0

    @app.get("/api/crisis-events")
    async def api_crisis_events_list(
        request: Request,
        only_unhandled: bool = False,
        prefix: str = "",
        limit: int = 50,
    ):
        """危机事件列表（默认按时间倒序；only_unhandled=true 仅看未处理）。

        #185：响应多带 ``state``（audit_off / audit_on_empty / has_events）与三开关，
        页面据此渲染三档空态——「功能没开」与「没有事件」绝不再长一个样。
        """
        _api_auth(request)
        sm = _sm(request)
        lim = max(1, min(int(limit or 50), 500))
        items = sm.crisis_list_for_admin(
            limit=lim, only_unhandled=bool(only_unhandled), user_prefix=prefix[:120],
        )
        switches = wellbeing_switches(_live_config(sm))
        total = int(sm.crisis_count_for_admin(only_unhandled=False) or 0)
        in_window = _count_since(sm, time.time() - AUDIT_EMPTY_WINDOW_DAYS * 86400)
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "unhandled_total": sm.crisis_count_for_admin(only_unhandled=True),
            "total": total,
            "state": audit_page_state(
                audit_on=switches["audit_on"], count_in_window=in_window, total_count=total,
            ),
            "window_days": AUDIT_EMPTY_WINDOW_DAYS,
            **switches,
        }

    @app.post("/api/crisis-events/enable")
    async def api_crisis_events_enable(request: Request):
        """一键开启留痕（+ 人工升级）：写 overlay 保注释，即时生效。需 manage_ops 权限。

        body ``{"escalation": true}``（默认 true）——两把一起开；``false`` 只开留痕。
        走 ``ConfigManager.set_overlay_flag``（ruamel round-trip 保注释，同陪伴能力看板的开关口径），
        路径钉死在 ``_ENABLE_FLAGS`` 白名单，不是通用写口。
        """
        _api_write("manage_ops")(request)
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        want_escalation = bool(body.get("escalation", True))
        cm = _config_manager()
        if cm is None or not callable(getattr(cm, "set_overlay_flag", None)):
            raise HTTPException(status_code=503, detail=tr(request, "err.ca.config_unavailable"))
        applied = []
        for path in _ENABLE_FLAGS:
            if path.endswith("crisis_escalation") and not want_escalation:
                continue
            ok, msg = cm.set_overlay_flag(path, True)
            if not ok:
                raise HTTPException(
                    status_code=500, detail=tr(request, "err.ca.enable_failed", msg=str(msg)),
                )
            applied.append(path)
        try:
            audit = getattr(ctx, "audit_store", None)
            if audit is not None and hasattr(audit, "log"):
                audit.log(
                    str(request.session.get("username") or "admin"),
                    "crisis_audit_enable",
                    target="companion.wellbeing",
                    new_val=",".join(applied),
                )
        except Exception:
            logger.debug("crisis_audit_enable 审计落账失败（忽略）", exc_info=True)
        return {"ok": True, "applied": applied, **wellbeing_switches(cm.config)}

    @app.post("/api/crisis-events/{event_id}/handle")
    async def api_crisis_event_handle(event_id: int, request: Request):
        """标记某危机事件为已人工处理（记录处理人 + 备注）。需 manage_ops 权限。"""
        _api_write("manage_ops")(request)
        sm = _sm(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        note = str(body.get("note", "") or "")[:500]
        handled_by = str(
            request.session.get("username")
            or request.session.get("role")
            or "admin"
        )
        ok = sm.crisis_mark_handled_for_admin(
            int(event_id), handled_by=handled_by, note=note,
        )
        if not ok:
            raise HTTPException(status_code=404, detail=tr(request, "err.ca.event_not_found"))
        return {"ok": True, "handled": int(event_id)}
