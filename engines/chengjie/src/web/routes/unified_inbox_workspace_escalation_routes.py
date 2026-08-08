"""统一收件箱——坐席工作台 SLA告警/坐席身份/升级队列路由域（巨石拆分 slice 13）。

把"SLA 告警源 + 当前坐席身份 + 升级队列（escalations/mine/assign/log）"这一子域，从
``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_escalation_routes(app, *, api_auth)``，由主 register 在**原位置**顺序调用。
端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下：sla 快照族（unified_inbox_sla）、auth 身份/主管权限、services._inbox_store；
只收 api_auth 一个参数（本域无 page/templates/config 需求）。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.inbox.store import SNOOZE_FOREVER_TS, is_permanent_snooze
from src.web.routes.unified_inbox_auth import (
    _is_supervisor,
    _require_supervisor,
    _session_agent,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.routes.unified_inbox_sla import _escalation_snapshot, _sla_alert_snapshot
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _record_snooze_ops_event(
    conversation_id: str, *, by: str, action: str, until_ts: float = 0.0,
) -> None:
    """搁置操作落 ops_events 审计（90 天可追溯），best-effort 绝不阻断主流程。

    - ``kind``＝``conv_snooze``；``reason``＝``set``（定时）/``forever``（永久）/``clear``（取消）；
    - ``detail``＝``conv=<id>;by=<agent>[;until=<epoch>]``——「谁把哪个客户永久搁置了」从
      不可考变成可查（P0 时 ``set_snooze(by=)`` 只收参不落痕，这里补上最后一米）。
    platform/account 从 conversation_id（``platform:account:chat_key``）反解，解不出不猜。
    """
    try:
        from src.ops.ops_events import get_ops_event_store

        store = get_ops_event_store()
        if store is None:
            return
        parts = str(conversation_id or "").split(":", 2)
        platform = parts[0] if len(parts) >= 3 else ""
        account = parts[1] if len(parts) >= 3 else ""
        detail = f"conv={conversation_id};by={by}"
        if action != "clear" and until_ts:
            detail += f";until={int(until_ts)}"
        store.record(
            "conv_snooze", account_id=account,
            platform=platform or "telegram", reason=str(action or ""),
            detail=detail,
        )
    except Exception:
        logger.debug("[snooze] ops 事件审计失败（已忽略）", exc_info=True)


def _snooze_history(conversation_id: str, *, limit: int = 10) -> List[Dict[str, Any]]:
    """从 ops_events 反查该会话的 conv_snooze 事件（新→旧），供面板显示搁置来源。

    detail 契约＝``conv=<id>;by=<agent>[;until=<epoch>]``（``_record_snooze_ops_event``
    唯一写入口）。先按 account_id（conversation_id 第二段）缩小扫描窗，再精确匹配
    ``conv=`` 段；缺库/解析失败 → 空列表（消费方隐藏该行，绝不报错）。
    """
    try:
        from src.ops.ops_events import get_ops_event_store

        store = get_ops_event_store()
        if store is None:
            return []
        cid = str(conversation_id or "")
        parts = cid.split(":", 2)
        account = parts[1] if len(parts) >= 3 else ""
        out: List[Dict[str, Any]] = []
        for r in store.recent(account_id=account, limit=200):
            if str(r.get("kind") or "") != "conv_snooze":
                continue
            fields: Dict[str, str] = {}
            for seg in str(r.get("detail") or "").split(";"):
                k, _, v = seg.partition("=")
                fields[k] = v
            if fields.get("conv") != cid:
                continue
            try:
                until = float(fields.get("until") or 0)
            except (TypeError, ValueError):
                until = 0.0
            out.append({
                "ts": float(r.get("ts") or 0),
                "action": str(r.get("reason") or ""),
                "by": str(fields.get("by") or ""),
                "until_ts": until,
            })
            if len(out) >= limit:
                break
        return out
    except Exception:
        logger.debug("[snooze] 审计反查失败（已忽略）", exc_info=True)
        return []


def register_workspace_escalation_routes(app, *, api_auth) -> None:
    """挂载 SLA 告警 / 坐席身份 / 升级队列端点（/api/workspace/sla-alerts|me|escalations*|escalation*）。"""

    @app.get("/api/workspace/sla-alerts")
    async def api_workspace_sla_alerts(request: Request):
        """SLA 告警源（顶栏徽标轮询 + 严重超时清单下钻）。"""
        api_auth(request)
        return _sla_alert_snapshot(request)

    @app.get("/api/workspace/me")
    async def api_workspace_me(request: Request):
        """当前坐席身份 + 角色能力（前端按 is_supervisor 显隐管理向 UI）。

        附带 C0-3 授权简况（read_only / state / 提示），供全局只读横幅消费。
        """
        api_auth(request)
        a = _session_agent(request)
        lic_brief = None
        try:
            from src.licensing import get_license_manager

            _st = get_license_manager().status()
            lic_brief = {
                "state": _st.state,
                "read_only": _st.read_only,
                "plan": _st.plan,
                "message": "；".join(_st.messages) if _st.read_only else "",
            }
        except Exception:
            logger.debug("授权简况读取失败（已忽略）", exc_info=True)
        demo_on = False
        try:
            from src.utils.demo_seeder import demo_status
            demo_on = bool(demo_status(_inbox_store(request)).get("present"))
        except Exception:
            logger.debug("demo 状态读取失败（已忽略）", exc_info=True)
        return {"ok": True, "agent_id": a["agent_id"],
                "display_name": a["display_name"], "role": a.get("role", ""),
                "is_supervisor": _is_supervisor(request),
                "license": lic_brief, "demo_mode": demo_on}

    @app.get("/api/workspace/escalations")
    async def api_workspace_escalations(request: Request):
        """升级告警源（无人有效处理的严重超时；全局口径，不受个人静默影响）。"""
        api_auth(request)
        return _escalation_snapshot(request)

    @app.get("/api/workspace/escalations/mine")
    async def api_workspace_escalations_mine(
        request: Request, days: int = 7,
    ):
        """我的指派升级列表（当前坐席被指派为责任主管的升级，含接管时延）。
        主管专属；非主管返回空列表（不报 403，前端可安全轮询）。
        """
        api_auth(request)
        if not _is_supervisor(request):
            return {"ok": True, "items": [], "total": 0}
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "items": [], "total": 0}
        agent_id = _session_agent(request)["agent_id"]
        since_ts = time.time() - int(max(1, min(90, days))) * 86400
        items = inbox.list_my_escalations(
            agent_id, since_ts=since_ts, limit=100)
        return {"ok": True, "items": items, "total": len(items)}

    @app.post("/api/workspace/escalation/{esc_id}/assign")
    async def api_workspace_escalation_assign(
        request: Request, esc_id: int,
    ):
        """主管手动将某条升级指派给另一位主管（reassign）。主管专属。
        Body JSON: {"agent_id": "<target_supervisor_agent_id>"}
        """
        api_auth(request)
        _require_supervisor(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        body = await request.json()
        target = str(body.get("agent_id") or "").strip()
        if not target:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="agent_id"))
        ok = inbox.set_escalation_assigned(esc_id, target)
        if not ok:
            raise HTTPException(404, tr(request, "err.ws.escalation_not_found", esc_id=esc_id))
        return {"ok": True, "esc_id": esc_id, "assigned_to": target}

    @app.get("/api/workspace/handoff-brief")
    async def api_workspace_handoff_brief(
        request: Request, conversation_id: str = "", reason: str = "",
    ):
        """M8 结构化转人工简报：客户画像（意图/情绪/风险/CSAT/摘要）+ 最近往来 + 亮点提醒。

        坐席接手前一键拉取，3 秒进入状态。任何已认证坐席可读（读取会话上下文）。
        store 缺失或会话无元数据时优雅降级为空画像（不报错）。
        """
        api_auth(request)
        from src.utils.handoff_brief import build_handoff_brief
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        inbox = _inbox_store(request)
        meta = None
        recent: List[Dict[str, Any]] = []
        if inbox is not None:
            try:
                meta = inbox.get_conv_meta(cid)
            except Exception:
                logger.debug("get_conv_meta 失败（已忽略）", exc_info=True)
            try:
                recent = inbox.list_recent_messages(cid, limit=12)
            except Exception:
                logger.debug("list_recent_messages 失败（已忽略）", exc_info=True)
        return build_handoff_brief(cid, meta, recent, reason=reason)

    @app.get("/api/workspace/escalation-log")
    async def api_workspace_escalation_log(request: Request, days: int = 7):
        """升级历史 + 接管时延（复盘安全网成效）：升级→首个人工接管。主管专属。"""
        api_auth(request)
        _require_supervisor(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "days": 7, "items": [], "stats": {}}
        span = 30 if int(days or 7) >= 30 else 7
        now = int(time.time())
        lt = time.localtime(now)
        midnight = int(time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight - (span - 1) * 86400
        convs = {str(c.get("conversation_id") or ""): c
                 for c in inbox.list_conversations(limit=500)}
        rows = inbox.escalation_takeovers(since, limit=500)
        taken_n = 0
        dly_sum = 0.0
        reasons: Dict[str, int] = {}
        items: List[Dict[str, Any]] = []
        for r in rows:
            c = convs.get(r["conversation_id"]) or {}
            delay = (int(r["taken_ts"] - r["ts"])
                     if r["taken_ts"] is not None else None)
            if delay is not None:
                taken_n += 1
                dly_sum += delay
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
            items.append({
                **r,
                "platform": str(c.get("platform") or ""),
                "name": str(c.get("display_name") or c.get("chat_key")
                            or r["conversation_id"]),
                "takeover_sec": delay,
            })
        total = len(items)
        return {"ok": True, "days": span, "items": items, "stats": {
            "total": total, "taken": taken_n,
            "taken_rate": round(taken_n / total * 100, 1) if total else 0.0,
            "avg_takeover_sec": int(dly_sum / taken_n) if taken_n else 0,
            "reasons": reasons,
        }}

    # ── P0-companion：会话搁置（snooze）——「稍后再看」从待接管/超时队列临时移出 ─────
    @app.post("/api/workspace/conversation/{conversation_id}/snooze")
    async def api_workspace_conversation_snooze(
        request: Request, conversation_id: str,
    ):
        """把会话搁置 N 分钟 / 到指定时刻 / 永久——从「待接管/超时告警」队列移出。

        Body JSON 三选一：``{"minutes": 120}`` / ``{"until_ts": <epoch 秒>}`` /
        ``{"forever": true}``（＝搁到 ``SNOOZE_FOREVER_TS`` 哨兵，不再按时间重浮）。
        到点自动重浮；客户期间再来消息则**任何档位都**立即重浮（永久≠静音）。
        ``until_ts`` 必须是未来的有限时刻（过去→400 而非旧的静默取消，超远期钉到哨兵）。
        任何已认证坐席可操作自己在看的会话。
        """
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        body = await request.json()
        if body.get("forever"):
            until_ts = SNOOZE_FOREVER_TS
        elif body.get("until_ts") is not None:
            try:
                until_ts = float(body.get("until_ts"))
            except (TypeError, ValueError):
                raise HTTPException(400, tr(request, "err.ws.until_ts_invalid"))
            if math.isnan(until_ts):
                raise HTTPException(400, tr(request, "err.ws.until_ts_invalid"))
            if until_ts > SNOOZE_FOREVER_TS:
                until_ts = SNOOZE_FOREVER_TS  # +inf / 超远期一律按「永久」哨兵
            elif until_ts <= time.time():
                # 旧行为是静默取消（set_snooze 视过去为 cancel）——前端弹「搁置失败」
                # 却不知为何。自定义时间上线后这条路径会被真实踩到，改为明确 400。
                raise HTTPException(400, tr(request, "err.ws.until_ts_past"))
        else:
            try:
                minutes = float(body.get("minutes") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, tr(request, "err.ws.minutes_invalid"))
            if minutes <= 0:
                raise HTTPException(400, tr(request, "err.ws.minutes_must_be_positive"))
            until_ts = time.time() + minutes * 60.0
        by = _session_agent(request)["agent_id"]
        snoozed = inbox.set_snooze(cid, until_ts, by=by)
        if snoozed:
            _record_snooze_ops_event(
                cid, by=by,
                action="forever" if is_permanent_snooze(until_ts) else "set",
                until_ts=until_ts)
        return {"ok": True, "conversation_id": cid, "snoozed": snoozed,
                "snooze_until": until_ts if snoozed else 0,
                "permanent": bool(snoozed and is_permanent_snooze(until_ts))}

    @app.post("/api/workspace/conversation/{conversation_id}/unsnooze")
    async def api_workspace_conversation_unsnooze(
        request: Request, conversation_id: str,
    ):
        """立即取消搁置，会话回到「待接管/超时告警」队列。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        inbox.clear_snooze(cid)
        _record_snooze_ops_event(cid, by=_session_agent(request)["agent_id"], action="clear")
        return {"ok": True, "conversation_id": cid, "snoozed": False}

    @app.post("/api/workspace/conversation/{conversation_id}/seen-mention")
    async def api_workspace_conversation_seen_mention(
        request: Request, conversation_id: str,
    ):
        """P4-11B：清除会话的「@我」未读旗标（坐席打开该群会话即视为已看到点名）。

        幂等；共享收件箱语义——任一坐席看过即对全员清除（与未读同口径）。
        """
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        cleared = False
        try:
            cleared = inbox.set_conversation_mentioned(cid, False)
        except Exception:
            logger.debug("[ws] seen-mention 清除失败", exc_info=True)
        return {"ok": True, "conversation_id": cid, "cleared": cleared}

    @app.get("/api/workspace/snoozed")
    async def api_workspace_snoozed(request: Request):
        """当前搁置中的会话清单（含剩余秒），供「搁置中」视图。任何已认证坐席可读。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "items": [], "total": 0}
        items = inbox.list_snoozed(limit=200)
        return {"ok": True, "items": items, "total": len(items)}

    @app.get("/api/workspace/conversation/{conversation_id}/snooze-history")
    async def api_workspace_conversation_snooze_history(
        request: Request, conversation_id: str,
    ):
        """该会话的搁置操作史（审计反查：谁在何时 搁置/永久/取消）。

        供搁置面板显示来源（「由谁设置」——别的坐席搁的一眼可知）。
        审计缺库/无记录 → 空列表，面板隐藏该行。任何已认证坐席可读。
        """
        api_auth(request)
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        items = _snooze_history(cid, limit=10)
        return {"ok": True, "conversation_id": cid, "items": items,
                "total": len(items)}
