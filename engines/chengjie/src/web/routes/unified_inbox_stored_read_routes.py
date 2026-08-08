"""统一收件箱——A1 store-backed 持久化读端点 / 自动化模式路由域（巨石拆分 slice 29）。

把 ``register_unified_inbox_routes`` 巨型闭包中 A1 读路径子域整体外移为
``register_stored_read_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- A1 store-backed 读：``unified-inbox/stored-chats`` + ``unified-inbox/history``
  （直接从 InboxStore 统一事实源读会话/消息，独立于 live 聚合的 /chats、/thread）
- 自动化模式：``unified-inbox/automation`` (GET/POST)

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 29 端点契约断言）。

依赖全部朝下：services._inbox_store、aggregate.(_read/_write_automation_mode)、
helpers.AUTOMATION_MODES、normalizer.conv_id。只收 api_auth 一个参数（零闭包私有 helper）。

注：A1 的 ``unified-inbox/profile`` 端点深度耦合 live 路径 helper（_collect_all_chats /
_get_telegram_client / _message_obj），不在本刀范围，随核心 live 集群后续处理。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import Depends, HTTPException, Request

from src.inbox.normalizer import conv_id as _conv_id
from src.web.routes.unified_inbox_aggregate import (
    _read_automation_mode,
    _write_automation_mode,
)
from src.web.routes.unified_inbox_helpers import AUTOMATION_MODES
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_stored_read_routes(app, *, api_auth) -> None:
    """挂载 A1 持久化读端点（stored-chats/history）+ 自动化模式（GET/POST）。"""

    # ── A1 读路径增量①：store-backed 持久化读端点 ──────────────────────
    # 直接从 InboxStore（统一事实源）读会话/消息，独立于 live 聚合（/chats、/thread）。
    # 价值：跨平台、跨重启的持久历史可查（蓝图 A1 验收）；不改 live 路径，零风险。

    @app.get("/api/unified-inbox/stored-chats")
    async def api_unified_inbox_stored_chats(
        request: Request, limit: int = 50, platform: str = "",
    ):
        """从持久层读会话列表（事实源），区别于实时聚合的 /chats。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        limit = max(1, min(200, int(limit or 50)))
        convs = store.list_conversations(limit=limit, platform=str(platform or ""))
        for c in convs:
            cid = str(c.get("conversation_id") or "")
            mode = _read_automation_mode(request, cid)
            c["automation_mode"] = mode if mode in AUTOMATION_MODES else "review"
            c["message_count"] = store.count_messages(cid)
        return {"ok": True, "source": "store", "count": len(convs), "chats": convs}

    @app.get("/api/unified-inbox/history")
    async def api_unified_inbox_history(
        request: Request, conversation_id: str = "", limit: int = 50,
    ):
        """从持久层读某会话的历史消息（跨重启可查），并附最近一次分析。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        limit = max(1, min(200, int(limit or 50)))
        conv = store.get_conversation(cid)
        if conv is None:
            return {"ok": True, "found": False, "source": "store",
                    "conversation_id": cid, "messages": [], "count": 0}
        # 取**最近** limit 条（ts 升序），而非最旧 limit 条：
        # AI 草稿/时间线都要看「当前正在聊的内容」，用最旧会导致长会话上下文错位。
        if hasattr(store, "list_recent_messages"):
            messages = store.list_recent_messages(cid, limit=limit)
        else:
            messages = store.list_messages(cid, limit=limit)
        analysis = None
        if hasattr(store, "latest_analysis"):
            try:
                analysis = store.latest_analysis(cid)
            except Exception:
                analysis = None
        return {
            "ok": True, "found": True, "source": "store",
            "conversation_id": cid, "conversation": conv,
            "messages": messages, "count": store.count_messages(cid),
            "analysis": analysis,
        }

    @app.get("/api/unified-inbox/automation")
    async def api_unified_inbox_automation_get(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
    ):
        api_auth(request)
        cid = _conv_id(str(platform or "").lower(), str(account_id or "default"), str(chat_key or ""))
        mode = _read_automation_mode(request, cid)
        # peer_bot_guard P2：预算状态搭便车（前端开会话本就调本端点渲染档位
        # 胶囊，零新增请求）。budget=None＝守卫关/取数失败——前端不显横幅。
        budget = None
        try:
            from src.inbox.peer_bot_guard import budget_state
            _cm = getattr(request.app.state, "config_manager", None)
            _cfg_root = (getattr(_cm, "config", None) or {}) if _cm else {}
            _store = _inbox_store(request)
            if _store is not None:
                budget = budget_state(_store, cid, _cfg_root)
        except Exception:
            logger.debug("[automation] 预算状态读取失败（忽略）", exc_info=True)
            budget = None
        # P1-198（2026-08-05）：账号在线状态搭同一趟便车。198 事故第二课——
        # 测试者手动停用账号后，会话界面毫无痕迹，此后所有「不回消息」都被
        # 归因成「产品坏了」。account=None＝注册表无此号/未初始化（主协议号、
        # 纯 RPA 会话），前端不显横幅。
        account = None
        try:
            from src.integrations.account_registry import peek_account
            _arow = peek_account(str(platform or "").lower(),
                                 str(account_id or "default"))
            if _arow:
                account = {"status": str(_arow.get("status") or ""),
                           "mode": str(_arow.get("mode") or "")}
        except Exception:
            account = None
        # 2026-08-07 有效档位（effective_automation）：把「平台/业务线/冷启动
        # 预热」封顶端给前端胶囊——下拉框的 mode 只是基础值，实际执行档位可能
        # 被系统封顶（.198「界面亮全自动、实际全进人审」的可见化闭环）。
        # effective=None＝求值失败 → 前端不渲染（fail-open，与档位闸同方向）；
        # 求值与 A/B 两线同一实现，保证「胶囊说的」=「护栏做的」。
        effective = None
        try:
            from src.inbox.effective_automation import effective_automation
            _ea_store = _inbox_store(request)
            _ea_cm = getattr(request.app.state, "config_manager", None)
            _ea_cfg = (getattr(_ea_cm, "config", None) or {}) if _ea_cm else {}
            _ea = effective_automation(
                _ea_store, _ea_cfg, conversation_id=cid,
                platform=str(platform or "").lower(),
                account_id=str(account_id or "default"),
                base_mode=mode)
            effective = {"mode": str(_ea.get("effective_mode") or mode),
                         "caps": _ea.get("caps") or []}
        except Exception:
            logger.debug("[automation] effective 档位求值失败（忽略）",
                         exc_info=True)
            effective = None
        return {"ok": True, "conversation_id": cid, "mode": mode,
                "budget": budget, "account": account, "effective": effective}

    @app.post("/api/unified-inbox/automation")
    async def api_unified_inbox_automation_set(request: Request, _=Depends(api_auth)):
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        mode = str(body.get("mode") or "review")
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        if mode not in AUTOMATION_MODES:
            raise HTTPException(400, tr(request, "err.ws.unsupported_automation_mode", mode=mode))
        cid = _conv_id(platform, account_id, chat_key)
        cancelled = _write_automation_mode(request, cid, mode)
        return {
            "ok": True,
            "conversation_id": cid,
            "mode": mode,
            "cancelled_l2": int(cancelled or 0),
        }

    @app.post("/api/unified-inbox/reply-budget/relief")
    async def api_unified_inbox_reply_budget_relief(
        request: Request, _=Depends(api_auth),
    ):
        """peer_bot_guard P2 坐席救济：本会话**今日**不再受每日预算限制。

        Body: ``{platform, account_id, chat_key}``。写台账 relief_day=今天
        （跨日自动失效，明天回到正常预算）；不清计数（观测口径保留）。
        等价于坐席人工接管的显式决定，与切档同权限（api_auth）。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        store = _inbox_store(request)
        if store is None or not hasattr(store, "set_budget_relief"):
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cid = _conv_id(platform, account_id, chat_key)
        from src.inbox.peer_bot_guard import budget_state, today_key
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:
            operator = "web_admin"
        store.set_budget_relief(cid, today_key())
        logger.info("[reply-budget] 救济：今日跳过预算 cid=%s by=%s", cid, operator)
        _cm = getattr(request.app.state, "config_manager", None)
        _cfg_root = (getattr(_cm, "config", None) or {}) if _cm else {}
        return {
            "ok": True,
            "conversation_id": cid,
            "budget": budget_state(store, cid, _cfg_root),
        }

    @app.get("/api/unified-inbox/bot-flag")
    async def api_unified_inbox_bot_flag_get(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
    ):
        """对方机器人守卫 P1：读会话的 bot 判定（徽章/证据 chips 数据源）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cid = _conv_id(str(platform or "").lower(),
                       str(account_id or "default"), str(chat_key or ""))
        row = store.get_conversation(cid) or {}
        return {
            "ok": True,
            "conversation_id": cid,
            "peer_is_bot": int(row.get("peer_is_bot") or 0),
            "bot_score": float(row.get("bot_score") or 0.0),
            "bot_evidence": str(row.get("bot_evidence") or ""),
        }

    @app.post("/api/unified-inbox/bot-flag")
    async def api_unified_inbox_bot_flag_set(request: Request, _=Depends(api_auth)):
        """对方机器人守卫 P1：一键覆写。

        Body: ``{platform, account_id, chat_key, value: "bot"|"human"|"clear"}``
        - ``bot``   → peer_is_bot=1 + 会话降 manual（停自动链，语义与 sweep 一致）；
        - ``human`` → peer_is_bot=-1（启发式疑似对其不再拦截/降档；Tier0 平台
          真值与复读/预算等行为刹车不受影响）；档位不动，由坐席自行调回；
        - ``clear`` → 回到未标注（0），评分/证据一并清空，守卫按信号重新判。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        value = str(body.get("value") or "").strip().lower()
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        if value not in ("bot", "human", "clear"):
            raise HTTPException(400, tr(
                request, "err.ws.unsupported_bot_flag", value=value))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cid = _conv_id(platform, account_id, chat_key)
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:   # 无 SessionMiddleware 的宿主（测试/嵌入部署）
            operator = "web_admin"
        # P1.5 覆写反馈环：先取原判定原因再覆写——「human:suspected_bot 有几次」
        # 就是启发式误判率的分子，下周词表/阈值校准只认这个数。best-effort。
        try:
            from src.inbox.peer_bot_guard import evidence_reason, record_override
            _prev_row = store.get_conversation(cid) or {}
            record_override(value, evidence_reason(_prev_row.get("bot_evidence")))
        except Exception:
            logger.debug("[bot-flag] 覆写计数失败（忽略）", exc_info=True)
        cancelled = 0
        if value == "bot":
            store.set_peer_bot_verdict(
                cid, is_bot=1, evidence=f"operator:{operator} 手动标记为机器人")
            cancelled = _write_automation_mode(request, cid, "manual")
        elif value == "human":
            store.set_peer_bot_verdict(
                cid, is_bot=-1, score=0.0,
                evidence=f"operator:{operator} 确认为真人")
        else:
            store.set_peer_bot_verdict(cid, is_bot=0, score=0.0, evidence="")
        row = store.get_conversation(cid) or {}
        return {
            "ok": True,
            "conversation_id": cid,
            "peer_is_bot": int(row.get("peer_is_bot") or 0),
            "bot_score": float(row.get("bot_score") or 0.0),
            "bot_evidence": str(row.get("bot_evidence") or ""),
            "cancelled_l2": int(cancelled or 0),
        }

    @app.post("/api/unified-inbox/automation/bulk-downgrade")
    async def api_unified_inbox_automation_bulk_downgrade(
        request: Request, _=Depends(api_auth),
    ):
        """主管一键：把所有全自动会话降为「AI草稿我审」，并取消其待投递 L2。

        应急止血（互聊刹不住时），不改全局值守开关。body 可选
        ``{to_mode:"review"|"manual"}``，默认 review。
        """
        from src.web.routes.unified_inbox_auth import _require_supervisor
        _require_supervisor(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        to_mode = str((body or {}).get("to_mode") or "review").lower()
        if to_mode not in ("review", "manual"):
            raise HTTPException(400, tr(request, "err.ws.unsupported_automation_mode", mode=to_mode))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cids = store.bulk_set_automation_mode("auto_ai", to_mode) or []
        cancelled = 0
        for cid in cids:
            try:
                cancelled += int(store.cancel_pending_l2_drafts(
                    cid, decided_by="bulk_mode_downgrade") or 0)
            except Exception:
                pass
        return {
            "ok": True,
            "from_mode": "auto_ai",
            "to_mode": to_mode,
            "changed": len(cids),
            "cancelled_l2": cancelled,
        }

    @app.get("/api/unified-inbox/automation-stats")
    async def api_unified_inbox_automation_stats(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 30,
    ):
        """全自动安全条：本会话今日自动发/拦截统计 + 近期审计记录（draft_audit_log）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cid = _conv_id(str(platform or "").lower(), str(account_id or "default"), str(chat_key or ""))
        now = datetime.now()
        since_ts = datetime(now.year, now.month, now.day).timestamp()
        stats = store.get_conversation_automation_stats(cid, since_ts=since_ts)
        recent = store.list_draft_audit(
            conversation_id=cid, since_ts=since_ts, limit=max(1, min(100, int(limit or 30))),
        )
        return {
            "ok": True,
            "conversation_id": cid,
            "since_ts": since_ts,
            "stats": stats,
            "recent": recent,
        }
