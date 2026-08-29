"""统一收件箱——协作实时推送路由域（巨石拆分 slice 36）。

把 ``register_unified_inbox_routes`` 巨型闭包中连续的 SSE + typing 子域整体外移为
``register_realtime_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- ``workspace/stream``：SSE 实时推送（inbox 事件 + SLA/升级边沿告警 + 通知队列）
- ``workspace/typing``：多坐席打字状态协同

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 36 端点契约断言）。

依赖全部朝下：auth.(_SUPERVISOR_ROLES/_is_supervisor/_session_agent)、
services._inbox_store、sla.(_sla_alert_snapshot/_escalation_snapshot/_presence_stale_sec)、
event_bus（handler 内局部 import）。只收 api_auth 一个参数（stream 内联 api_auth 调用）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from src.integrations.shared.event_bus import get_event_bus
from src.web.routes.unified_inbox_auth import (
    _SUPERVISOR_ROLES,
    _is_supervisor,
    _session_agent,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.routes.unified_inbox_sla import (
    _escalation_snapshot,
    _presence_stale_sec,
    _sla_alert_snapshot,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# SSE replay / live 订阅事件类型（与 monolith 原集合一致）
_SSE_EVENT_TYPES = frozenset({
    "inbox_message",
    # P2-2：出站镜像事件（autosend/主动触达/坐席手发经编排器回写收件箱时发布）——
    # 前端据此「刷新线程/列表预览但不加未读」，选中会话轮询得以 10s→30s。
    "outbound_message",
    # 2026-07-31 计划维护预告：restart_instance.ps1 停机前经内部路由广播，前端把
    # 接下来的连接失败渲染成蓝色「维护窗口」而非红色「连接中断」（横幅根因治理）。
    "maintenance_notice",
    "agent_presence",
    "conversation_claim", "conversation_assigned", "follow_up",
    "draft_created",
    "draft_sla_breach",
    "draft_reassigned",
    "typing",
    "peer_typing",
    "message_op",
    # 2026-08-17 官方级消息管理：置顶/会话删除/消息软删（含恢复与清空）的多窗口同步
    "conversation_pinned",
    "conversation_deleted",
    "messages_deleted",
    "anomaly_alert",
    "sla_alert",
    "conv_note",
    "queue_alert",
    "stage_advance",
    "stage_advance_pending",
    "ops_report",
    "stage_downgrade",
    "stage_reunion",
    "stage_sync",
    "orchestrator_worker_alert",
    "workflow_step",
    "workflow_execution_completed",
    "workflow_execution_failed",
    "workflow_execution_cancelled",
    "health_alert",
    "billing_alert",
    "ops_report",
    # P0 2026-08-09：营销目标达成（goals.notify 扫描器发布）——工作台 toast + 铃铛
    "goal_completed_alert",
    # P3 2026-08-17：peer_bot_guard 判定告警（服务端每会话每日至多一次）。
    # 前端只消费 reason=daily_budget → 触顶中央弹窗（workspace_base __wsBudgetPop），
    # 修「预算熔断发生时坐席不开着那个会话就零感知」的盲区；其余 reason 前端暂忽略。
    "bot_peer_alert",
})

# 写入 app.state.notif_queue 的重要事件类型
_NOTIF_EVENT_TYPES = frozenset({
    "inbox_message", "draft_sla_breach", "draft_reassigned",
    "conversation_assigned",
    # P3.1 2026-08-17：预算触顶进铃铛历史（离线/跨班次坐席补看弹窗错过的触顶）。
    # 内容级准入在 _notif_content_ok——bot_peer_alert 只收 reason=daily_budget
    # （Tier0/复读/秒回判定已有收件箱 🤖 徽章 + webhook，进铃铛=噪音）。
    "bot_peer_alert",
    # P0-协作闭环（2026-08-01）：@提及注解进通知历史——此前被 @ 的坐席只有
    # 「正开着同一会话」才收到 toast，跨班次/离线的提及等于丢失。读取侧按
    # 会话坐席过滤（batch_notif_routes），非被 @ 者的铃铛历史不出现别人的提及。
    "conv_note",
    "anomaly_alert", "sla_alert", "escalation", "queue_alert",
    "stage_advance", "stage_advance_pending", "stage_downgrade",
    "stage_reunion", "stage_sync", "workflow_step",
    "workflow_execution_completed", "workflow_execution_failed",
    "workflow_execution_cancelled",
    "health_alert",
    "billing_alert",
    "orchestrator_worker_alert",
    "ops_report",
    "goal_completed_alert",
})

# 复发型告警：按「类型+会话」在 notif_queue 内合并，仅保留最新一条（避免历史堆叠）
_COALESCE_NOTIF_TYPES = frozenset({
    "escalation", "sla_alert", "draft_sla_breach", "queue_alert", "anomaly_alert",
    # 预算触顶按会话合并：SSE 每个新连接会把 recent_events 重放一遍经过
    # _maybe_push_notif（本队列无 conv_note 式幂等），coalesce 保最新一条
    # 即天然去重；跨日再触顶也只留最新（昨天的触顶已无行动价值）。
    "bot_peer_alert",
})


def customer_msgs_in_center(config: dict | None) -> bool:
    """客户聊天消息要不要进通知中心/铃铛历史（impl85 阶段5，工单#30 钧拍板）。

    「铃铛的消息中心，不用显示客户聊天记录，只需要系统的消息或需要人工处理的
    消息」——客户消息默认**不进**（内容留在通知中心也有隐私问题；会话列表未读
    才是它的家）。``workspace.notify_center.customer_messages: true`` 重新打开
    （值守承诺的「想盯消息的人可以自己打开」）。系统/运维/需人工类事件不受影响。
    """
    try:
        ws = (config or {}).get("workspace") or {}
        nc = ws.get("notify_center") if isinstance(ws, dict) else None
        if isinstance(nc, dict) and "customer_messages" in nc:
            return bool(nc.get("customer_messages"))
    except Exception:
        pass
    return False


def _notif_content_ok(evt: dict, config: dict | None = None) -> bool:
    """铃铛队列的内容级准入（类型白名单之上的第二道闸，纯函数可门禁）。

    bot_peer_alert 是混合语义事件（Tier0/复读/秒回/预算共用一个类型）——
    只有 ``reason=daily_budget``（预算触顶）值得进坐席铃铛历史：它有明确
    的当场行动（今日继续/改人审跟进），其余判定属身份标注，收件箱徽章与
    webhook 已覆盖。
    impl85 阶段5：``inbox_message``（客户聊天消息）按 ``customer_msgs_in_center``
    准入（默认不进——见该函数 docstring）。其他类型一律放行（维持旧行为）。
    """
    etype = (evt or {}).get("type")
    if etype == "inbox_message":
        return customer_msgs_in_center(config)
    if etype != "bot_peer_alert":
        return True
    data = evt.get("data") or {}
    return str(data.get("reason") or "") == "daily_budget"


def _edge_pick(items: list, seen: set) -> list:
    """SLA/升级边沿判定的单一口径：返回本轮「新转入」的 items，并原地维护 seen 集
    （补新边沿 + 剔除已恢复者——恢复后再次越线可再报）。

    存在的理由（2026-08-05 实锤）：连接首轮 seen 为空 → 旧逻辑把**全部在途项**当
    新边沿逐条发帧（生产积压 ~45 条 SLA + ~45 条升级），前端每帧又各触发一次快照
    刷新 → 冷启动瞬间 ~90 个并发 GET 把浏览器同源 6 连接吃满，页面上其余请求
    （含 ?conv= 深链救援）整段饿死。连接期这些帧本就零信息量——工作台开页时
    已拉过快照接口、收帧后也只是再拉一次快照。故首轮 emit=False 静默 prime
    （升级审计副作用照跑），只有连接存续期间的**真边沿**才发帧。"""
    fresh = [it for it in items if it["conversation_id"] not in seen]
    seen.update(it["conversation_id"] for it in fresh)
    seen.intersection_update({it["conversation_id"] for it in items})
    return fresh


def _is_loopback_client(request: Any) -> bool:
    """maintenance-notice 的本机直通判定。

    重启编排脚本没有 session/Bearer，且「宣告本进程即将停机」天然只该来自
    同一台机器 —— loopback 即放行；其余来源回退 ``api_auth``。
    """
    try:
        host = request.client.host if request.client else ""
    except Exception:
        host = ""
    return host in ("127.0.0.1", "::1", "localhost")


def register_realtime_routes(app, *, api_auth) -> None:
    """挂载 SSE 实时推送 + typing 协同端点。"""

    @app.post("/api/internal/ops/maintenance-notice")
    async def api_maintenance_notice(request: Request):
        """内部桥（2026-07-31「连接中断」横幅根因治理）：停机前宣告计划维护窗口。

        ``restart_instance.ps1`` 在 stop 之前 POST（body ``{window_sec, reason}``）：
        登记 ``maintenance_notice`` 进程内状态（→ ``seat_restart_banner.quiet_poll``
        折叠，见 instance_restart_status）并经 EventBus → SSE 即时广播 —— 已打开的
        工作台把接下来的连接失败渲染为蓝色「服务维护窗口」而非红色「连接中断」，
        轮询/SSE 重连借 ``__wsRestartCool.quiet`` 的既有消费口自动降速。
        鉴权：loopback 直通（脚本无 session；同机才可宣告本进程维护），
        非本机回退 ``api_auth``（Bearer/主管 session，供将来 UI 触发）。
        """
        if not _is_loopback_client(request):
            api_auth(request)
        from src.utils.maintenance_notice import set_notice

        try:
            body = await request.json()
        except Exception:
            body = {}
        snap = set_notice(
            (body or {}).get("window_sec"),
            reason=str((body or {}).get("reason") or ""),
        )
        try:
            get_event_bus().publish("maintenance_notice", {
                "left_sec": snap["left_sec"],
                "until_ts": snap["until_ts"],
                "reason": snap["reason"],
            })
        except Exception:
            logger.debug("maintenance_notice 事件发布失败（已忽略）", exc_info=True)
        return {"ok": True, "maintenance": snap}

    @app.get("/api/workspace/stream")
    async def api_workspace_stream(request: Request):
        """SSE：实时推送收件箱新消息事件（替代前端轮询）。"""
        api_auth(request)
        import json as _json

        bus = get_event_bus()
        queue = bus.subscribe()

        _sla_seen: set = set()

        def _sla_pushes(emit: bool = True):
            """边沿触发：返回本轮"新转入严重超时"的会话 SSE 帧（去重 + 恢复后可再报）。

            emit=False（连接首轮）：只 prime seen 集不发帧——在途存量对新连接零信息量，
            逐条重放曾把浏览器连接池打满（见 _edge_pick docstring）。"""
            frames: List[str] = []
            try:
                snap = _sla_alert_snapshot(request)
                fresh = _edge_pick(snap.get("items", []), _sla_seen)
                if emit:
                    for it in fresh:
                        frames.append(
                            "data: " + _json.dumps(
                                {"type": "sla_alert", "data": it},
                                ensure_ascii=False) + "\n\n")
            except Exception:
                logger.debug("SLA SSE 推送计算失败（已忽略）", exc_info=True)
            return frames

        _esc_seen: set = set()

        def _pick_assigned_supervisor(inbox) -> str:
            """负载均衡：从在线主管中选当前指派数最少的那个。
            都不在线或无法确定时返回空串（保留广播语义）。"""
            if inbox is None:
                return ""
            try:
                now = time.time()
                since = now - 86400  # 24 h 窗口内的已指派数
                presence = inbox.list_agent_presence(
                    active_within_sec=_presence_stale_sec(request))
                online = [p for p in presence
                          if p.get("status") in ("online", "busy")]
                if not online:
                    return ""
                sups = [p for p in online
                        if str(p.get("role") or "") in _SUPERVISOR_ROLES]
                pool = sups if sups else online
                best = min(
                    pool,
                    key=lambda p: inbox.count_assigned_escalations(
                        str(p["agent_id"]), since_ts=since),
                )
                return str(best.get("agent_id") or "")
            except Exception:
                logger.debug("auto-assign supervisor 失败（已忽略）", exc_info=True)
                return ""

        def _esc_pushes(emit: bool = True):
            """边沿触发：新升级 → 审计落库 + 自动指派主管 + 推定向 SSE 帧。

            emit=False（连接首轮）：审计/指派副作用**照跑**（服务重启窗口越线的升级
            仍要有人记账），但不发帧——存量重放曾把浏览器连接池打满（见 _edge_pick）。"""
            frames: List[str] = []
            try:
                snap = _escalation_snapshot(request)
                inbox = _inbox_store(request)
                for it in _edge_pick(snap.get("items", []), _esc_seen):
                    cid = it["conversation_id"]
                    assigned_to = ""
                    if inbox is not None:
                        try:
                            is_new = inbox.record_escalation(
                                cid, reason=it.get("reason", ""),
                                agent_id=it.get("agent_id", ""),
                                agent_name=it.get("agent_name", ""),
                                wait_sec=it.get("wait_sec", 0))
                            if is_new:
                                assigned_to = _pick_assigned_supervisor(inbox)
                                if assigned_to:
                                    try:
                                        rows = inbox.list_escalations(
                                            since_ts=time.time() - 10, limit=5)
                                        esc_id = next(
                                            (r["id"] for r in rows
                                             if r.get("conversation_id") == cid),
                                            None)
                                        if esc_id is not None:
                                            inbox.set_escalation_assigned(
                                                esc_id, assigned_to)
                                    except Exception:
                                        logger.debug("set_escalation_assigned 失败",
                                                     exc_info=True)
                            else:
                                try:
                                    rows = inbox.list_escalations(
                                        since_ts=time.time() - 3600, limit=20)
                                    existing = next(
                                        (r for r in rows
                                         if r.get("conversation_id") == cid), None)
                                    assigned_to = str(
                                        (existing or {}).get("assigned_to") or "")
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug("升级审计落库失败（已忽略）", exc_info=True)
                    if emit:
                        payload = dict(it)
                        payload["assigned_to"] = assigned_to
                        frames.append(
                            "data: " + _json.dumps(
                                {"type": "escalation", "data": payload},
                                ensure_ascii=False) + "\n\n")
            except Exception:
                logger.debug("升级 SSE 推送计算失败（已忽略）", exc_info=True)
            return frames

        def _maybe_push_notif(evt: dict):
            """将重要事件写入 app.state.notif_queue（P24 通知中心）。

            复发型告警（升级/会话SLA/草稿SLA/队列/异常）按「类型+会话」合并：
            写入前先剔除队列中同 key 的旧条，仅保留最新一条 —— 从源头避免历史里
            同一会话堆几十条同义告警（前端亦有合并，这里是 defense-in-depth）。
            """
            etype = evt.get("type")
            if etype not in _NOTIF_EVENT_TYPES:
                return
            _cm = getattr(request.app.state, "config_manager", None)
            if not _notif_content_ok(
                    evt, (getattr(_cm, "config", None) or {}) if _cm else None):
                return
            nq: list = getattr(request.app.state, "notif_queue", None)
            if nq is None:
                nq = []
                request.app.state.notif_queue = nq
            if etype in _COALESCE_NOTIF_TYPES:
                d = evt.get("data") or {}
                key = str(d.get("conversation_id") or d.get("draft_id") or d.get("id") or "")
                if key:
                    nq[:] = [
                        n for n in nq
                        if not (
                            n.get("type") == etype
                            and str((n.get("data") or {}).get("conversation_id")
                                    or (n.get("data") or {}).get("draft_id")
                                    or (n.get("data") or {}).get("id") or "") == key
                        )
                    ]
            elif etype == "conv_note":
                # 注解按 note_id 幂等：SSE 重连会重放 recent_events 并再次经过本函数，
                # 同一条注解若刷新 _notif_ts 会把已读的提及顶回未读——首写胜出，重放丢弃。
                nid = str((evt.get("data") or {}).get("note_id")
                          or evt.get("note_id") or "")
                if nid and any(
                    n.get("type") == "conv_note"
                    and str((n.get("data") or {}).get("note_id")
                            or n.get("note_id") or "") == nid
                    for n in nq
                ):
                    return
            nq.append({**evt, "_notif_ts": int(time.time() * 1000)})
            if len(nq) > 200:
                del nq[:-200]

        async def _gen():
            try:
                for evt in bus.recent_events(30):
                    if evt.get("type") in _SSE_EVENT_TYPES:
                        yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                        _maybe_push_notif(evt)
                # 连接首轮：静默 prime（升级审计副作用照跑）——在途存量对新连接零信息量，
                # 逐条重放曾触发前端刷新风暴打满浏览器连接池（见 _edge_pick docstring）。
                _sla_pushes(emit=False)
                _esc_pushes(emit=False)
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        if evt.get("type") in _SSE_EVENT_TYPES:
                            yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                            _maybe_push_notif(evt)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        for fr in _sla_pushes():
                            yield fr
                        for fr in _esc_pushes():
                            yield fr
                        try:
                            _watcher = getattr(request.app.state, "sla_watcher", None)
                            if _watcher is not None and _is_supervisor(request):
                                snap = _watcher.status_snapshot()
                                yield "data: " + _json.dumps({
                                    "type": "sla_watcher_status",
                                    "data": snap,
                                }, ensure_ascii=False) + "\n\n"
                        except Exception:
                            pass
                    if await request.is_disconnected():
                        break
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/workspace/typing")
    async def api_workspace_typing(request: Request, _=Depends(api_auth)):
        """Phase 11：多坐席打字状态协同 — 向同一对话的其他坐席发送实时 typing 事件。"""
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        agent = _session_agent(request)
        try:
            get_event_bus().publish("typing", {
                "conversation_id": conversation_id,
                "agent_id": agent["agent_id"],
                "agent_name": agent["display_name"],
                "ts": time.time(),
            })
        except Exception:
            logger.debug("typing 事件发布失败", exc_info=True)
        return {"ok": True}
