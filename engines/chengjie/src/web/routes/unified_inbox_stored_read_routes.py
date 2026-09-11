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
import time
from datetime import datetime

from fastapi import Depends, HTTPException, Request

from src.inbox.normalizer import conv_id as _conv_id
from src.web.routes.unified_inbox_aggregate import (
    _agent_yield_state_q18,
    _cancel_inflight_q3,
    _read_automation_mode,
    _resume_agent_yield_q18,
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
        # P0 2026-08-09 接管可见化：显式档位的来源与时间（「谁在什么时候把它
        # 写成这样」），以及接管态的自动接回倒计时——横幅说的与 watchdog
        # sweep 做的同参同判定（takeover_rearm.rearm_state）。None＝无显式行
        # / 旧 store，前端不渲染。
        mode_source = None
        rearm = None
        # P1-12 搁置静音：与 rearm 并列的第二种「AI 被按住了」态，但**没有**自动
        # 接回（坐席说的「别管它」不该被超时推翻）→ 必须常驻可见 + 一键恢复，
        # 否则搁置到点后静音就是隐形的。None＝非搁置静音态，前端不渲染。
        snooze_hold = None
        try:
            _ms_store = _inbox_store(request)
            if _ms_store is not None and hasattr(
                    _ms_store, "get_automation_mode_meta"):
                _meta = _ms_store.get_automation_mode_meta(cid)
                if _meta:
                    mode_source = {
                        "source": str(_meta.get("source") or ""),
                        "updated_at": float(_meta.get("updated_at") or 0.0),
                    }
                    from src.inbox.takeover_rearm import rearm_state
                    _cm3 = getattr(request.app.state, "config_manager", None)
                    _cfg3 = (getattr(_cm3, "config", None) or {}) if _cm3 else {}
                    rearm = rearm_state(_meta, _cfg3)
                    from src.inbox.snooze_hold import snooze_hold_state
                    snooze_hold = snooze_hold_state(_meta, _cfg3)
        except Exception:
            logger.debug("[automation] mode_source 读取失败（忽略）",
                         exc_info=True)
        # P0 2026-08-12 发送护栏预判搭同一趟便车（额度拦截可见化事故第三课——
        # 「点了才知道」之前，坐席应该在 composer 上方就看到「额度已用完」）。
        # 判定与编排器发送护栏同一函数（send_gate_snapshot 内走 send_blocked，
        # notify=False 不占告警防抖窗）；仅编排器实际拥有的账号才有意义（RPA
        # 回落路径不受这些护栏约束）。send_gate=None＝无信息，前端不显横幅。
        send_gate = None
        try:
            from src.integrations.account_orchestrator import (
                get_orchestrator_if_running,
            )
            _orch = get_orchestrator_if_running()
            if _orch is not None and _orch.owns(
                    str(platform or "").lower(), str(account_id or "default")):
                from src.inbox.send_gate_status import send_gate_snapshot
                _cm4 = getattr(request.app.state, "config_manager", None)
                send_gate = send_gate_snapshot(
                    str(platform or "").lower(), str(account_id or "default"),
                    str(chat_key or ""),
                    config=(getattr(_cm4, "config", None) or {}) if _cm4 else {})
                # P1：横幅「白名单此客户」按钮的能力位——服务端按 session 角色判
                # （拒 agent/viewer，与 exempt 端点同闸），前端零角色管道。
                if send_gate is not None:
                    try:
                        _sg_role = str(request.session.get("role", "") or "")
                    except Exception:
                        _sg_role = ""
                    send_gate["can_exempt"] = _sg_role not in ("agent", "viewer")
                    # P0 2026-08-23 急停可见化：横幅「解除停发」能力位——与
                    # DELETE /api/ops/kill-switch 的 manage_ops 闸同口径
                    # （master/admin；旧 token 会话无 role 视同 master），防
                    # 「按钮亮了点下去 403」。判定异常一律 False（少亮不误导）。
                    try:
                        _cl_role = _sg_role
                        if not _cl_role and bool(request.session.get("auth")):
                            _cl_role = "master"
                        _us = getattr(request.app.state, "user_store", None)
                        if _us is not None and hasattr(_us, "can_write"):
                            send_gate["can_lift"] = bool(
                                _us.can_write(_cl_role, "manage_ops"))
                        else:
                            send_gate["can_lift"] = _cl_role in ("master", "admin")
                    except Exception:
                        send_gate["can_lift"] = False
        except Exception:
            logger.debug("[automation] send_gate 快照失败（忽略）", exc_info=True)
            send_gate = None
        # #142（2026-09-02，钧 0902 07:55）：总闸拦截可见化——「会话档=全自动/
        # 多选但被总闸拦下」的会话在会话视图内亮显式提示+一键恢复，不再只靠顶栏
        # 小标签。判定单点仍是 deliver_paused_reason（telegram A 线直答豁免与
        # effective_automation ⑦ 层同口径）；pause_meta 补「谁关的/何时/几点自动
        # 恢复」；can_resume=主管角色（与 POST /api/companion/deliver-gate/resume
        # 的 _require_supervisor 同闸，防「按钮亮了点下去 403」）。None＝未拦截/
        # 旧后端，前端不渲染。
        deliver_paused = None
        try:
            if (str(platform or "").lower() != "telegram"
                    and mode in ("auto_ai", "multi_choice")):
                from src.inbox.automation_mode import deliver_paused_reason
                _dp_cm = getattr(request.app.state, "config_manager", None)
                _dp_cfg = (getattr(_dp_cm, "config", None) or {}) if _dp_cm else {}
                if deliver_paused_reason(_dp_cfg):
                    from src.inbox.autosend_gate_state import (
                        config_dir_from_manager, pause_meta,
                    )
                    from src.web.routes.unified_inbox_auth import _is_supervisor
                    deliver_paused = pause_meta(
                        _dp_cfg, config_dir_from_manager(_dp_cm)) or {}
                    deliver_paused["can_resume"] = bool(_is_supervisor(request))
        except Exception:
            logger.debug("[automation] deliver_paused 读取失败（忽略）",
                         exc_info=True)
            deliver_paused = None
        # P0 2026-08-14 搁置状态搭同一趟便车（cp-conv-ops 持久状态行——「点了搁置
        # 又跳回原样」事故的读回半边）：0＝未搁置/已到点；前端 feat 探测本字段，
        # 缺失（旧后端）自动回落 GET /api/workspace/snoozed 权威清单。
        snooze_until = 0.0
        try:
            _sn_store = _inbox_store(request)
            if _sn_store is not None:
                _sn_meta = _sn_store.get_conv_meta(cid) or {}
                snooze_until = float(_sn_meta.get("snooze_until") or 0.0)
                if snooze_until <= time.time():
                    snooze_until = 0.0
        except Exception:
            logger.debug("[automation] snooze_until 读取失败（忽略）", exc_info=True)
            snooze_until = 0.0
        return {"ok": True, "conversation_id": cid, "mode": mode,
                "budget": budget, "account": account, "effective": effective,
                "mode_source": mode_source, "rearm": rearm,
                "snooze_hold": snooze_hold,
                "send_gate": send_gate, "snooze_until": snooze_until,
                "deliver_paused": deliver_paused,
                # Q-18 C（#292）：「AI 让位中」状态（坐席 60s 内发过 / 打过字 → worker defer 中）
                # → 会话头 ay- chip 倒计时；None＝worker 缺席 / 旧后端，前端不渲染
                "agent_yield": _agent_yield_state_q18(request, cid)}

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
        # P0 2026-08-09 群聊上全自动要显式确认：把群/频道一把切到 auto_ai（AI 会
        # 在群里自动说话）多为批量误操作——.104 实弹修复时 3 个群被顺手切成
        # 全自动。仅拦「升到 auto_ai 且会话是 group/channel 且未带 confirm_group」；
        # 私聊、降档方向、会话行缺失（fail-open）都不受影响。409 detail 带
        # code=group_confirm_required，前端弹确认后带 confirm_group 重试。
        if mode == "auto_ai" and not bool(body.get("confirm_group")):
            _chat_type = ""
            try:
                _gg_store = _inbox_store(request)
                if _gg_store is not None:
                    _chat_type = str((_gg_store.get_conversation(cid) or {})
                                     .get("chat_type") or "")
            except Exception:
                _chat_type = ""
            if _chat_type in ("group", "channel"):
                raise HTTPException(409, {
                    "code": "group_confirm_required",
                    "chat_type": _chat_type,
                    "message": tr(request, "err.ws.group_confirm_required"),
                })
        # D-M9（M-2 E #223）：切手动三选——缺省「一直手动」（source=human，粘性，sweep 不碰）；
        # body.rearm="30m" ＝坐席显式要「30 分钟后接回」→ 来源改 takeover_opt_from:<接管前档>，
        # sweep 对它恒生效（不受全局 takeover_rearm 开关约束）；「离开会话时接回」由前端在切换
        # 会话时再调本端点恢复，不落服务端状态。
        _rearm = str(body.get("rearm") or "").strip().lower()
        _prev_explicit = None
        if mode == "manual" and _rearm in ("30m", "rearm30"):
            try:
                _st0 = _inbox_store(request)
                if _st0 is not None:
                    _prev_explicit = _st0.get_automation_mode_if_set(cid)
            except Exception:
                _prev_explicit = None
        cancelled = _write_automation_mode(request, cid, mode)
        # Q-18 C（#292）：切到 / 重选「全自动」= 明示接回——清让位窗 + 取消 defer 立即放行
        # （worker 落 `[autosend] resume by=mode_select`）。非 auto_ai 档不碰让位状态。
        agent_yield_resumed = None
        if mode == "auto_ai":
            agent_yield_resumed = _resume_agent_yield_q18(request, cid, by="mode_select")
        rearm_source = ""
        if mode == "manual" and _rearm in ("30m", "rearm30"):
            try:
                from src.inbox.takeover_rearm import opt_in_rearm_source
                _st1 = _inbox_store(request)
                if _st1 is not None:
                    rearm_source = opt_in_rearm_source(_prev_explicit)
                    _st1.set_automation_mode(cid, "manual", source=rearm_source)
            except Exception:
                logger.debug("[automation] 30 分钟接回来源写入失败（保持一直手动）", exc_info=True)
                rearm_source = ""
        return {
            "ok": True,
            "conversation_id": cid,
            "mode": mode,
            # Q-3（#264 D）：含在途（拟人等待中）稿——前端 toast「已取消 N 条待发 AI 消息」
            "cancelled_l2": int(cancelled or 0),
            "rearm_source": rearm_source,
            # Q-18 C：切全自动时的接回结果 {had_yield, released, by}（非 auto_ai → None）
            "agent_yield_resumed": agent_yield_resumed,
        }

    @app.post("/api/unified-inbox/agent-yield/resume")
    async def api_unified_inbox_agent_yield_resume(request: Request, _=Depends(api_auth)):
        """Q-18 C（#292）会话头「AI 让位中 · N 秒后接回」chip 点击 = **立即接回**：清该会话坐席
        发送 / 打字时刻 + 取消 defer 立即放行（worker 日志 `[autosend] resume by=chip`）。

        Body ``{platform, account_id, chat_key}`` 或 ``{conversation_id}``，可选 ``by``（缺省 chip）。
        返回 ``{ok, conversation_id, had_yield, released, by, agent_yield}``；worker 缺席 →
        ``had_yield=False``（不报错）。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = str(body.get("conversation_id") or "").strip()
        if not cid:
            platform = str(body.get("platform") or "").strip().lower()
            account_id = str(body.get("account_id") or "default").strip() or "default"
            chat_key = str(body.get("chat_key") or "").strip()
            if not platform or not chat_key:
                raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
            cid = _conv_id(platform, account_id, chat_key)
        by = str(body.get("by") or "chip").strip()[:24] or "chip"
        res = _resume_agent_yield_q18(request, cid, by=by)
        return {"ok": True, "conversation_id": cid, **res,
                "agent_yield": _agent_yield_state_q18(request, cid)}

    @app.post("/api/unified-inbox/agent-typing")
    async def api_unified_inbox_agent_typing(request: Request, _=Depends(api_auth)):
        """Q-3（#264 D）坐席打字信号：工作台输入框有输入（前端 3s 节流）→ worker 视为**插话**，
        放弃该会话在途 / 排队的 L2 AI 稿（60s 窗口内新捞到的也不发）。人工优先 ≥ 客户。

        Body ``{platform, account_id, chat_key}`` 或 ``{conversation_id}``。返回
        ``{ok, conversation_id, cancelled}``；worker 缺席 → cancelled=0（不报错，前端 fire-and-forget）。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        cid = str(body.get("conversation_id") or "").strip()
        if not cid:
            platform = str(body.get("platform") or "").lower()
            account_id = str(body.get("account_id") or "default")
            chat_key = str(body.get("chat_key") or "")
            if not platform or not chat_key:
                raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
            cid = _conv_id(platform, account_id, chat_key)
        cancelled = 0
        try:
            worker = getattr(request.app.state, "autosend_worker", None)
            if worker is not None and hasattr(worker, "note_agent_typing"):
                cancelled = int(worker.note_agent_typing(cid) or 0)
        except Exception:
            logger.debug("[inflight] agent_typing 信号处理失败（忽略）", exc_info=True)
        return {"ok": True, "conversation_id": cid, "cancelled": cancelled}

    @app.get("/api/unified-inbox/why-no-reply")
    async def api_unified_inbox_why_no_reply(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
    ):
        """「为什么没自动回」进程内诊断（why_no_reply CLI 的 API 化）。

        findings 只出码+参数（文案由前端按 UI 语言渲染），判定与 A/B 两线
        护栏同源（reply_diagnosis.diagnose_conversation）。只读零副作用。
        """
        api_auth(request)
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        from src.inbox.reply_diagnosis import diagnose_conversation
        _cm = getattr(request.app.state, "config_manager", None)
        cfg = (getattr(_cm, "config", None) or {}) if _cm else {}
        data = diagnose_conversation(
            _inbox_store(request), cfg,
            platform=str(platform or "").lower(),
            account_id=str(account_id or "default"),
            chat_key=str(chat_key or ""),
            # Q-18 C（#292）：worker 在手的让位状态 → finding agent_yield（until / by）
            worker=getattr(request.app.state, "autosend_worker", None))
        data["ok"] = True
        return data

    @app.post("/api/unified-inbox/warmup-review")
    async def api_unified_inbox_warmup_review_set(request: Request):
        """主管一键开/关「预热期入站人审」（cold_start.warmup_review）。

        Body: ``{enabled: bool}``。写 config.local.yaml overlay（ruamel 保注释
        链路）+ 配置热重载 ~30s 生效免重启。这是 .198/.104 事故里「看得见
        （封顶胶囊）但改不了（要 SSH 改 YAML）」的最后一块补齐。
        """
        from src.web.routes.unified_inbox_auth import _require_supervisor
        _require_supervisor(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool((body or {}).get("enabled"))
        cm = getattr(request.app.state, "config_manager", None)
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            raise HTTPException(503, tr(request, "err.ws.config_write_unavailable"))
        path = "companion.proactive_topic.cold_start.warmup_review"
        ok, msg = cm.set_overlay_flag(path, enabled)
        if not ok:
            raise HTTPException(500, tr(request, "err.ws.config_write_failed",
                                        err=str(msg or "")))
        return {"ok": True, "path": path, "value": enabled}

    @app.post("/api/unified-inbox/platform-cap")
    async def api_unified_inbox_platform_cap_set(request: Request):
        """主管设置/解除某平台的自动化档位封顶（inbox.auto_draft.platform_modes）。

        Body: ``{platform: str, ceiling: "review"|"manual"|""}``（空串=解除）。
        P2 2026-08-21（「封顶忘摘」事故闭环）：此前封顶只能 SSH 改 YAML——体检
        面板看得见、改不了，8/18 的 WA 临时封顶因此挂了 3 天没人摘。走
        ``set_overlay_flag`` 单键写（保注释 + 内存即时生效 + 不动其他平台的
        封顶）。**解除用显式空串而非删键**：键缺席会回落构造期快照，快照里
        可能还躺着旧封顶（P0 2026-08-21 实锤）。与 warmup-review 同主管权限。
        """
        from src.web.routes.unified_inbox_auth import _require_supervisor
        _require_supervisor(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        platform = str((body or {}).get("platform") or "").strip().lower()
        ceiling = str((body or {}).get("ceiling") or "").strip().lower()
        if not platform or not platform.replace("_", "").isalnum():
            # 平台名进 set_overlay_flag 的点分路径，必须先净化（防键注入）
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="platform"))
        if ceiling not in ("", "review", "manual"):
            raise HTTPException(400, tr(request, "err.ws.platform_cap_invalid"))
        cm = getattr(request.app.state, "config_manager", None)
        if cm is None or not hasattr(cm, "set_overlay_flag"):
            raise HTTPException(503, tr(request, "err.ws.config_write_unavailable"))
        path = f"inbox.auto_draft.platform_modes.{platform}"
        ok, msg = cm.set_overlay_flag(path, ceiling)
        if not ok:
            raise HTTPException(500, tr(request, "err.ws.config_write_failed",
                                        err=str(msg or "")))
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:
            operator = "web_admin"
        logger.info("[platform-cap] %s -> %r by=%s",
                    platform, ceiling or "(uncap)", operator)
        return {"ok": True, "platform": platform, "ceiling": ceiling}

    @app.post("/api/unified-inbox/reply-budget/relief")
    async def api_unified_inbox_reply_budget_relief(
        request: Request, _=Depends(api_auth),
    ):
        """peer_bot_guard P2 坐席救济：本会话**今日**不再受每日预算限制。

        Body: ``{platform, account_id, chat_key, revoke?: bool}``。
        默认（豁免）写台账 relief_day=今天（跨日自动失效，明天回到正常预算）；
        ``revoke: true``（P1 2026-08-12 后悔药）＝撤销今日豁免，预算判定立即
        恢复。两个方向共用**同一写入口**（勿造第二个端点的红线不变），均不清
        计数（观测口径保留）。等价于坐席人工接管的显式决定，与切档同权限。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        revoke = bool(body.get("revoke"))
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        store = _inbox_store(request)
        if store is None or not hasattr(store, "set_budget_relief"):
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        if revoke and not hasattr(store, "clear_budget_relief"):
            # 旧 store（未重启装载）不认撤销：如实拒绝，别把「没撤」说成「撤了」
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        cid = _conv_id(platform, account_id, chat_key)
        from src.inbox.peer_bot_guard import budget_state, today_key
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:
            operator = "web_admin"
        if revoke:
            store.clear_budget_relief(cid)
            logger.info("[reply-budget] 撤销豁免：恢复预算 cid=%s by=%s",
                        cid, operator)
        else:
            store.set_budget_relief(cid, today_key())
            logger.info("[reply-budget] 救济：今日跳过预算 cid=%s by=%s",
                        cid, operator)
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

    @app.post("/api/unified-inbox/automation/account-gate")
    async def api_unified_inbox_automation_account_gate(
        request: Request, _=Depends(api_auth),
    ):
        """M-2 B（D-M1 ⑦）账号级门禁人工出路。Body ``{platform, account_id, action}``：

        - ``clear_degraded``：坐席确认通道已修好 → 解除「通道异常，已暂停自动发送」红标
          （连续失败计数归零；自动投递重新放开）；
        - ``end_cooldown``：坐席已过目积压 → 提前结束登录冷静期；
        - ``snapshot``：只读快照（账号菜单渲染用）。
        """
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        platform = str((body or {}).get("platform") or "").lower()
        account_id = str((body or {}).get("account_id") or "default")
        action = str((body or {}).get("action") or "snapshot").lower()
        if not platform:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="platform"))
        from src.inbox.account_channel_gate import (
            account_snapshot, clear_degraded, end_cooldown,
        )
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:
            operator = "web_admin"
        changed = False
        if action == "clear_degraded":
            changed = clear_degraded(platform, account_id, actor=operator)
        elif action == "end_cooldown":
            changed = end_cooldown(platform, account_id)
            if changed:
                logger.info("[channel_gate] %s:%s 冷静期由 %s 提前结束",
                            platform, account_id, operator)
        elif action != "snapshot":
            raise HTTPException(400, tr(request, "err.ws.field_required", field="action"))
        _cm = getattr(request.app.state, "config_manager", None)
        _cfg = (getattr(_cm, "config", None) or {}) if _cm else {}
        return {"ok": True, "platform": platform, "account_id": account_id,
                "action": action, "changed": bool(changed),
                "gate": account_snapshot(platform, account_id, config=_cfg)}

    @app.post("/api/unified-inbox/automation/account-bulk")
    async def api_unified_inbox_automation_account_bulk(
        request: Request, _=Depends(api_auth),
    ):
        """M-2 B（D-M1 ② / UE7VM3 ④）账号级批量切档：「此账号全部会话 → 手动/半自动/全自动」。

        Body ``{platform, account_id, mode, confirm?: bool}``：
        - ``confirm`` 缺省/False → **干跑**：返回 ``total / will_change / override_individual /
          skipped_groups / already``，前端据此弹「将对 N 个会话开启，覆盖 M 个个别设置」；
        - ``confirm=True`` → 落地：写会话行（source=account_bulk）+ 账号级决策（新会话跟随）
          + 登录门禁确认（登录后默认半自动到此为止）；升 auto_ai 时群/频道跳过。
        全自动只能经本端点按账号显式开启；逐会话下拉仍可单独调。

        P-2 C（#259 ZH3ZQ5 / H3BAJD · D-P1）：升 auto_ai 的干跑多回 ``split``（active / dormant /
        frozen / self_chat 四栏 + 样例）；``scope="all"`` → 分栏覆盖该账号**全部**会话（登录完成后
        账号已是全自动时的确认框）。confirm 可带 ``targets``（勾选的 cid，只改这些；未勾旧会话不碰）、
        ``demote``（登录确认框里被取消勾选的「最近有来信」会话 → 拨回 review）、``draft_for``
        （勾选且有待回复入站的 → 各生成一条 **review** 稿进人审，永不 L2）、``login_review_id``（弹过即 ack）。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        mode = str(body.get("mode") or "").lower()
        confirm = bool(body.get("confirm"))
        _scope = str(body.get("scope") or "targets").lower()
        _targets = body.get("targets")
        _targets = [str(x) for x in _targets] if isinstance(_targets, list) else None
        _demote = [str(x) for x in (body.get("demote") or []) if str(x)] \
            if isinstance(body.get("demote"), list) else []
        _draft_for = [str(x) for x in (body.get("draft_for") or []) if str(x)] \
            if isinstance(body.get("draft_for"), list) else []
        _login_review_id = int(body.get("login_review_id") or 0)
        if not platform:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="platform"))
        if mode not in AUTOMATION_MODES:
            raise HTTPException(400, tr(request, "err.ws.unsupported_automation_mode", mode=mode))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        from src.inbox.account_bulk_mode import (
            account_mode_summary, apply_account_bulk, plan_account_bulk,
        )
        from src.inbox.account_channel_gate import account_snapshot
        _cm = getattr(request.app.state, "config_manager", None)
        _cfg = (getattr(_cm, "config", None) or {}) if _cm else {}
        if not confirm:
            plan = plan_account_bulk(store, platform, account_id, mode)
            plan.pop("targets", None)
            if mode == "auto_ai" and _scope == "all":
                # 登录确认框：账号已是全自动，将改动数为 0，但两栏要看**全部**会话
                from src.inbox.dormant_review import split_targets
                sp = split_targets(store, platform, account_id, None, config=_cfg)
                plan["split"] = sp
                c = sp.get("counts") or {}
                plan["dormant"] = int(c.get("dormant") or 0)
                plan["frozen"] = int(c.get("frozen") or 0)
                plan["self_chat"] = int(c.get("self_chat") or 0)
                plan["active_targets"] = [r["conversation_id"] for r in sp.get("active") or []]
                plan["dormant_targets"] = [r["conversation_id"] for r in sp.get("dormant") or []]
            return {"ok": True, "dry_run": True, **plan,
                    "gate": account_snapshot(platform, account_id, config=_cfg),
                    "summary": account_mode_summary(store, platform, account_id)}
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:
            operator = "web_admin"
        res = apply_account_bulk(store, platform, account_id, mode,
                                 actor=operator, config=_cfg, only=_targets)
        # Q-3（#264 D）账号级切到 manual / review / multi_choice → 该账号在途（拟人等待中）
        # L2 一并取消，计入 cancelled_l2（前端 toast「已取消 N 条」）
        res["cancelled_inflight"] = 0
        if mode in ("manual", "review", "multi_choice"):
            res["cancelled_inflight"] = _cancel_inflight_q3(
                request, platform=platform, account_id=account_id, by="mode_switch")
            res["cancelled_l2"] = int(res.get("cancelled_l2") or 0) + res["cancelled_inflight"]
        # 登录确认框：取消勾选的「最近有来信」会话 → 拨回 review（不碰）；取消其待投递 L2
        res["demoted"] = 0
        if mode == "auto_ai" and _demote:
            from src.inbox.account_bulk_mode import BULK_SOURCE
            for cid in _demote[:2000]:
                try:
                    if str(store.get_automation_mode_if_set(cid) or "") == "auto_ai":
                        try:
                            store.set_automation_mode(cid, "review", source=BULK_SOURCE)
                        except TypeError:
                            store.set_automation_mode(cid, "review")
                        res["demoted"] += 1
                        if hasattr(store, "cancel_pending_l2_drafts"):
                            store.cancel_pending_l2_drafts(cid, decided_by="account_bulk_demote")
                except Exception:
                    logger.debug("[account_bulk] demote 失败 cid=%s", cid, exc_info=True)
        # 勾选且待回复的 → 逐条 review 稿（永不 L2；LLM 逐条，上限 20 条防长请求）
        res["drafted"] = []
        if mode == "auto_ai" and _draft_for:
            from src.inbox.dormant_review import draft_preview
            for cid in _draft_for[:20]:
                try:
                    r = await draft_preview(request.app, store, cid, actor=operator,
                                            source="bulk_confirm")
                    if r.get("ok"):
                        res["drafted"].append({"conversation_id": cid, "draft_id": r["draft_id"]})
                except Exception:
                    logger.debug("[account_bulk] 预览稿失败 cid=%s", cid, exc_info=True)
        if _login_review_id:
            try:
                from src.inbox.dormant_review import get_dormant_store
                get_dormant_store(store).ack_login_review(_login_review_id, by=operator)
            except Exception:
                pass
        return {"ok": True, "dry_run": False, **res,
                "summary": account_mode_summary(store, platform, account_id)}

    @app.get("/api/unified-inbox/dormant-review")
    async def api_unified_inbox_dormant_review(
        request: Request, _=Depends(api_auth),
        platform: str = "", account_id: str = "", limit: int = 200,
    ):
        """P-2 D（#259 · D-P1）「沉寂会话待你决定」清单快照：items / count / total /
        login_reviews（登录完成后待弹一次的两栏确认框）/ limit_h。前端横幅 + 轮询同一入口。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        from src.inbox.dormant_review import snapshot
        return {"ok": True, **snapshot(store, platform=str(platform or "").lower(),
                                       account_id=str(account_id or ""), limit=int(limit or 200))}

    @app.post("/api/unified-inbox/dormant-review/action")
    async def api_unified_inbox_dormant_review_action(
        request: Request, _=Depends(api_auth),
    ):
        """三按钮：``{conversation_id, action: ignore|manual|draft}``。
        ignore → 标 ``dormant:ignored``；manual → 档位切手动（前端随后跳会话）；
        draft → 生成一条 **review** 稿（永不自动发），返回 draft_id / draft_text。
        ``{login_review_id, action: ack}`` → 登录确认框已弹过（不再弹）。"""
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.inbox_persistence_disabled"))
        try:
            operator = str(request.session.get("username", "web_admin"))
        except Exception:
            operator = "web_admin"
        action = str((body or {}).get("action") or "").lower()
        from src.inbox.dormant_review import (
            ACTIONS, apply_action, draft_preview, get_dormant_store,
        )
        if action == "ack":
            rid = int((body or {}).get("login_review_id") or 0)
            ok = get_dormant_store(store).ack_login_review(rid, by=operator) if rid else False
            return {"ok": bool(ok), "action": "ack", "login_review_id": rid}
        cid = str((body or {}).get("conversation_id") or "")
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        if action not in ACTIONS:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="action"))
        if action == "draft":
            r = await draft_preview(request.app, store, cid, actor=operator)
            if not r.get("ok"):
                return {"ok": False, "action": action, "conversation_id": cid,
                        "error": str(r.get("error") or "generate_failed")}
            return {"ok": True, "action": action, **r}
        return apply_action(store, cid, action, actor=operator)

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
        # Q-3（#264 D）：应急止血也要拦住正在拟人等待的在途稿（全部范围）
        cancelled += _cancel_inflight_q3(request, by="mode_switch")
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
