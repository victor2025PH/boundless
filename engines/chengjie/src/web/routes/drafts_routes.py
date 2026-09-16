"""统一草稿/审批路由（Phase B / B2）。

API 端点（register_drafts_routes — main.py 调用）：
  GET  /api/drafts                          ?status=pending&platform=&limit=50
  GET  /api/drafts/stats                    — 按平台×状态计数
  GET  /api/drafts/risk-summary             — 待处理草稿按 autopilot_level 分布 + actionable / stale_count（B2 / Q-31 D）
  GET  /api/drafts/audit                    — 草稿处置审计日志（B2；主管专属）
  GET  /api/drafts/autosend-status          — AutosendWorker 运行指标（Phase A）
  GET  /api/drafts/{draft_id}               — 单条草稿
  POST /api/drafts/{draft_id}/resolve       — 带 L4 拦截 + 审计的统一处置（B2）
  POST /api/drafts/{draft_id}/force-override — 主管强制放行 L4 草稿（B2）
  POST /api/drafts/bulk-autosend            — 批量触发所有 L2 草稿自动发送（B2）
  POST /api/drafts/persona-test             — 人设试聊回复存入人工审批队列（绝不自动发送）

页面路由（register_drafts_page_routes — admin.py 调用）：
  GET  /workspace/drafts         — 草稿审批工作台（坐席/主管均可，L4 需主管放行）
  GET  /workspace/draft-audit    — 审计日志页（主管专属）

依赖 app.state.draft_service（main.py 注入）。未注入时端点返回 503。
"""

from __future__ import annotations

import logging
import time

from fastapi import Depends, HTTPException, Request
from src.utils.agent_char_usage import record_request_chars
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _perm_ok(request: Request, perm: str) -> bool:
    """按登录坐席判能力权限（P2 管理面改造：perms_json 按人覆写，master 恒 True）。

    懒 import：``resolve_user_perm`` 由并行批次在 web_user_store 落地，模块未就绪
    （ImportError）/ user_store 未暴露 / 未登录（token 链）/ 任何异常 → **一律放行**
    （fail-open；本文件多线共用，本批只接 /translate 一处，勿扩散）。
    """
    try:
        from src.utils.web_user_store import resolve_user_perm
        us = getattr(request.app.state, "user_store", None)
        sess = request.session
        uname = str(sess.get("username") or "")
        role = str(sess.get("role") or "")
        if us is None or not uname:
            return True
        return resolve_user_perm(us, uname, role, perm)
    except Exception:
        return True

# 主管角色集（与 unified_inbox_routes 保持一致）
_SUPERVISOR_ROLES = {"master", "admin", "supervisor"}

# J2：意图 → 模板场景映射（与 template_seeds.py 的 scene 枚举对应）
_INTENT_TO_SCENE: dict = {
    "退款": "refund", "退款申请": "refund", "要求退款": "refund",
    "物流查询": "shipping", "催单": "shipping", "物流": "shipping", "到货查询": "shipping",
    "订单查询": "order_inquiry", "查询订单": "order_inquiry", "下单": "order_inquiry",
    "产品咨询": "product_info", "产品问题": "product_info", "询问产品": "product_info",
    "投诉": "complaint", "投诉处理": "complaint", "不满": "complaint",
    "感谢": "closing", "再见": "closing",
    "询问": "order_inquiry",  # 通用问询默认归订单
}


def _intent_to_scene(intent: str) -> str:
    """J2：将 quick_analyze 返回的 intent 映射到模板 scene。"""
    if not intent:
        return ""
    for key, scene in _INTENT_TO_SCENE.items():
        if key in intent:
            return scene
    return ""


def _get_draft_service(request: Request):
    svc = getattr(request.app.state, "draft_service", None)
    if svc is None:
        raise HTTPException(503, tr(request, "err.svc.draft_service_disabled"))
    return svc


def _session_role(request: Request) -> str:
    """从 session 读 role（与 unified_inbox_routes._session_agent 对齐）。"""
    try:
        sess = request.session  # may raise if no SessionMiddleware
    except (AttributeError, AssertionError):
        sess = {}
    if not sess:
        sess = request.scope.get("session", {})
    return str(sess.get("role") or "")


def _session_agent_id(request: Request) -> str:
    try:
        sess = request.session
    except (AttributeError, AssertionError):
        sess = {}
    if not sess:
        sess = request.scope.get("session", {})
    uid = sess.get("user_id") or sess.get("username") or ""
    return str(uid)


def _is_supervisor(request: Request) -> bool:
    return _session_role(request) in _SUPERVISOR_ROLES


def register_drafts_routes(app, *, api_auth):
    """挂载统一草稿路由（B2 增强版）。"""

    @app.get("/api/drafts")
    async def api_drafts_list(
        request: Request,
        status: str = "pending",
        platform: str = "",
        limit: int = 50,
        conversation_id: str = "",
        _=Depends(api_auth),
    ):
        svc = _get_draft_service(request)
        limit = max(1, min(200, int(limit or 50)))
        # B86：conversation_id＝会话级精确过滤（体检计数与面板列表同源；旧服务
        # 无该形参时回落全量口径——宁可多显示不静默空屏）
        try:
            drafts = svc.list_drafts(
                status=status or "", platform=platform or "", limit=limit,
                conversation_id=str(conversation_id or ""))
        except TypeError:
            drafts = svc.list_drafts(
                status=status or "", platform=platform or "", limit=limit)
        # 「点通过会不会被拦」的**预判**（与护栏同一入口 approve_block_reason，
        # 否则徽标与实际行为不一致＝比没徽标更糟）。让坐席在点之前就看见「这稿太老 /
        # 已经回过」，而不是撞 409 才知道。判定内部按稿龄短路，只有可能被拦的才查会话。
        if hasattr(svc, "approve_block_reason"):
            for d in drafts:
                try:
                    d["approve_blocked"] = svc.approve_block_reason(d)
                except Exception:
                    d["approve_blocked"] = ""
        # M-2 E（#235）：L1「为什么要人确认」原因码——进程注册表优先（autodraft 拟稿时登记），
        # 重启后回落草稿审计行 action=l1_reason（record_draft_audit）。非 L1 不查。
        try:
            from src.inbox.l1_reason import peek as _l1_peek
            _store = getattr(svc, "_store", None)
            for d in drafts:
                if str(d.get("autopilot_level") or "") != "L1":
                    continue
                _did = str(d.get("draft_id") or "")
                _r = _l1_peek(_did) or _l1_peek(str(d.get("conversation_id") or ""))
                if not _r and _store is not None and hasattr(_store, "list_draft_audit"):
                    try:
                        for _a in (_store.list_draft_audit(draft_id=_did, limit=10) or []):
                            if str(_a.get("action") or "") == "l1_reason" and _a.get("reason"):
                                _r = str(_a.get("reason"))
                                break
                    except Exception:
                        _r = ""
                d["l1_reason"] = _r
        except Exception:
            logger.debug("[drafts] L1 原因富集失败（忽略）", exc_info=True)
        # Q-21 B（#302 / Y82GWM）：稿头「对方语言未知 · 按人设语言（English）回」——起草链登记的
        # 会话语言计划（outbound_translate.build_conv_lang_plan，进程注册表 → KV conv_lang_plan:<cid>）。
        try:
            from src.inbox.outbound_translate import peek_conv_lang_plan
            _lp_store = getattr(svc, "_store", None)
            for d in drafts:
                _lp = peek_conv_lang_plan(str(d.get("conversation_id") or ""), store=_lp_store)
                if _lp:
                    d["lang_plan"] = _lp
        except Exception:
            logger.debug("[drafts] lang-plan 富集失败（忽略）", exc_info=True)
        # R87 P1-3（X9B22T 15:30 preview='[PHOTO selfie cozy bedroom…'）：草稿正文里的 [PHOTO …] 发图指令
        # 是给投递链的协议标记，坐席预览不该看原码——另给 draft_text_display（剥净）+ photo_directive
        # （kind / scene），draft_text 原样保留（编辑 / 通过仍送原文，投递链照常解析执行）。
        try:
            from src.ai.photo_directive import extract_photo_directive as _epd
            for d in drafts:
                _raw = str(d.get("draft_text") or d.get("text") or "")
                if "[PHOTO" not in _raw and "[photo" not in _raw:
                    continue
                _clean, _pd = _epd(_raw)
                if _pd:
                    d["photo_directive"] = {"kind": str(_pd.get("kind") or ""),
                                            "scene": str(_pd.get("scene") or "")[:120]}
                if _clean != _raw:
                    d["draft_text_display"] = _clean
        except Exception:
            logger.debug("[drafts] photo_directive 富集失败（忽略）", exc_info=True)
        return {"ok": True, "count": len(drafts), "drafts": drafts}

    @app.get("/api/drafts/stats")
    async def api_drafts_stats(request: Request, _=Depends(api_auth)):
        svc = _get_draft_service(request)
        return {"ok": True, "stats": svc.stats()}

    @app.get("/api/drafts/risk-summary")
    async def api_drafts_risk_summary(
        request: Request, sla_hours: int = 4, _=Depends(api_auth),
    ):
        """L0–L4 分布统计（供仪表盘风险看板轮询）。含 sla_overdue（D1）与 actionable / stale_count（Q-31 D）。"""
        svc = _get_draft_service(request)
        summary = svc.risk_summary()
        # D1：追加 SLA 过期数量（主管可见；非主管返回 -1 表示无权限）
        if _is_supervisor(request):
            threshold_ts = time.time() - max(1, min(72, int(sla_hours or 4))) * 3600
            drafts = svc.list_drafts(status="pending", limit=200)
            sla_overdue = sum(
                1 for d in drafts
                if d.get("autopilot_level") in {"L3", "L4"}
                and float(d.get("created_ts") or 0) > 0
                and float(d.get("created_ts") or 0) < threshold_ts
            )
            summary["sla_overdue"] = sla_overdue
        else:
            summary["sla_overdue"] = -1
        return {"ok": True, **summary}

    @app.get("/api/drafts/autosend-status")
    async def api_drafts_autosend_status(request: Request, _=Depends(api_auth)):
        """AutosendWorker 运行时指标（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        # 人工通过→真投递 接线状态：零流量也能看出链路是否断（配置漂移/注入被吞/
        # 重构漏接线）。刻意在 worker=None 分支也给——那正是最可能断的形态
        # （l2_autosend.enabled=false ⇒ worker 不创建 ⇒ 坐席点通过只标记不发）。
        _hd_wired = None
        _stale_h = None
        try:
            _dsvc = getattr(request.app.state, "draft_service", None)
            if _dsvc is not None:
                _hd_wired = bool(getattr(_dsvc, "inbox_deliver_wired", False))
                # 陈旧护栏阈值（小时，0=关）。不暴露的话这道护栏对运维完全不可见——
                # 「它在不在、几小时」只能翻代码，而它直接决定坐席能不能发老稿子。
                _stale_h = float(getattr(_dsvc, "_stale_approve_hours", 0) or 0)
        except Exception:
            _hd_wired = None
        worker = getattr(request.app.state, "autosend_worker", None)
        if worker is None:
            return {"ok": True, "worker": None,
                    "human_deliver_wired": _hd_wired,
                    "stale_approve_hours": _stale_h,
                    "note": tr(request, "err.draft.autosend_worker_off")}
        snap = worker.status_snapshot()
        try:
            from src.inbox.voice_autosend import metrics_snapshot as _vms
            snap["voice"] = _vms()  # 全自动语音：sent/fallback/last_reason/last_duration_ms
        except Exception:
            pass
        try:
            from src.inbox.video_autosend import metrics_snapshot as _vdm
            snap["video"] = _vdm()  # 全自动数字人视频：sent/fallback/decision_reasons
        except Exception:
            pass
        try:
            from src.inbox.holding_reply import metrics_snapshot as _hms
            snap["holding"] = _hms()  # L3 缓冲话术：sent/skipped_*/failed/last_lang
        except Exception:
            pass
        try:
            from src.integrations.humanize_metrics import (
                snapshot as _hum, pacing_snapshot as _pace,
            )
            snap["humanize"] = _hum()  # 已读/打字按平台成功率（read_ok/read_fail/typing_*）
            snap["pacing"] = _pace()   # 拟人延迟分布（按路径 avg_delay/avg_target/avg_elapsed）
        except Exception:
            pass
        try:
            from src.inbox.image_autosend import metrics_snapshot as _ims
            snap["image"] = _ims()  # 全自动发图：sent/fallback/last_reason/last_kind
        except Exception:
            pass
        try:
            from src.ai.image_gate import metrics_snapshot as _igs
            snap["image_gate"] = _igs()  # 出图自检：checked/passed/rejected/retry_ok/soft_pass
        except Exception:
            pass
        try:
            from src.inbox.reply_split import bubbles_metrics_snapshot as _bms
            snap["bubbles"] = _bms()  # P1.5 文本分条：sends_by_source/parts_sent/partial
        except Exception:
            pass
        try:
            from src.ai.face_swap import metrics_snapshot as _fss
            snap["face_swap"] = _fss()  # 换脸：swapped/passthrough/failed/last_reason
        except Exception:
            pass
        try:
            from src.inbox.automation_mode_stats import metrics_snapshot as _ams
            snap["automation_bootstrap"] = _ams()
        except Exception:
            pass
        try:
            # 语言硬闸（P1-198）：held（冲突拦下）/ rescued（gate-only 救回）/
            # no_target_sent（CJK 盲发）。enabled 随配置回显——闸门在不在岗
            # 零流量也能看出来。
            from src.inbox.outbound_lang_stats import get_outbound_lang_stats
            from src.inbox.outbound_translate import parse_outbound_lang_gate_cfg
            _lg = get_outbound_lang_stats().dump()
            _cm = getattr(request.app.state, "config_manager", None)
            _lg["enabled"] = bool(parse_outbound_lang_gate_cfg(
                getattr(_cm, "config", None) or {}).get("enabled"))
            snap["lang_gate"] = _lg
        except Exception:
            pass
        try:
            from src.inbox.effective_mood import mood_steering_snapshot as _mss
            # P1-198 续：人工情绪标注转向——marks（按标签计打点）/ consumed
            # （draft_directive/goal_hold/proactive_gate/voice 各链真用上的次数）。
            # 「标了却恒 0 消费」＝接线断了，零流量即可判。
            snap["mood_steering"] = _mss()
        except Exception:
            pass
        try:
            from src.companion.proactive_stats import metrics_snapshot as _ps
            snap["proactive_topic"] = _ps()
        except Exception:
            pass
        try:
            # #37 自动链引用回复（I-4 D2）：decided/quoted/skipped{single_inbound,
            # low_relevance,…}/applied/fallback_plain + 配置回显（enabled/地板）——
            # 「开了却 decided 恒 0」＝接线断；「low_relevance 占比高」＝地板该调。
            from src.inbox import reply_quote_policy as _rqp
            _qr = _rqp.stats_snapshot()
            _cm_q = getattr(request.app.state, "config_manager", None)
            _qcfg = _rqp.parse_quote_cfg(getattr(_cm_q, "config", None) or {})
            _qr["enabled"] = bool(_qcfg.get("enabled"))
            _qr["min_unanswered"] = int(_qcfg.get("min_unanswered") or 0)
            _qr["min_relevance"] = float(_qcfg.get("min_relevance") or 0)
            snap["quote_reply"] = _qr
        except Exception:
            pass
        try:
            # Phase18：主动触达分形态回复率（photo/voice/text A/B）——数据源 outreach_log，
            # 看板「数据健康」卡直读本端点即可渲染，无需再打 companion 路由。
            _ibx = getattr(request.app.state, "inbox_store", None)
            if _ibx is not None and hasattr(_ibx, "outreach_response_stats"):
                _ab = {}
                for _kind in ("photo", "voice", "text"):
                    _r = _ibx.outreach_response_stats(
                        f"proactive_topic:{_kind}", response_window_days=3.0)
                    if int(_r.get("sent") or 0) > 0:
                        _ab[_kind] = {
                            "sent": int(_r.get("sent") or 0),
                            "responded": int(_r.get("responded") or 0),
                            "response_rate": _r.get("response_rate"),
                        }
                if _ab:
                    snap["proactive_kind_ab"] = _ab
        except Exception:
            pass
        try:
            # P2：分条 A/B——全自动文本投递（bubbles vs single）3 天窗回复率对比。
            # 观察性对比（非随机分组，受消息长短/类型混杂影响），趋势参考用。
            _ibx_b = getattr(request.app.state, "inbox_store", None)
            if _ibx_b is not None and hasattr(_ibx_b, "outreach_response_stats"):
                _bab = {}
                for _kind in ("bubbles", "single", "holdout"):
                    _r = _ibx_b.outreach_response_stats(
                        f"autosend_text:{_kind}", response_window_days=3.0)
                    if int(_r.get("sent") or 0) > 0:
                        _bab[_kind] = {
                            "sent": int(_r.get("sent") or 0),
                            "responded": int(_r.get("responded") or 0),
                            "response_rate": _r.get("response_rate"),
                        }
                if _bab:
                    snap["bubbles_ab"] = _bab
        except Exception:
            pass
        try:
            # 统一草稿引擎规则栈生效观测（记忆/情感/陪伴/慢思考/守卫/重试命中）
            from src.monitoring.metrics_store import get_metrics_store
            snap["draft_pipeline"] = get_metrics_store().get_inbox_draft_metrics()
        except Exception:
            pass
        try:
            # 工作时间班表（P0-ws，2026-08-04）：配置总闸 + 全局默认班表此刻
            # 在班态 + 各账号覆写的在班态（含下一次边界）——零流量也能判
            # 「为什么这个号现在不自动回」。worker 侧计数（skipped_off_hours/
            # catchup）已在 status_snapshot 本体。
            from src.inbox.work_hours_gate import (
                schedule_state as _ws_state,
                work_schedule_cfg as _ws_cfg_fn,
            )
            _cm_ws = getattr(request.app.state, "config_manager", None)
            _ws = _ws_cfg_fn(getattr(_cm_ws, "config", None) or {})
            if _ws.get("enabled"):
                _ws_out: dict = {
                    "enabled": True,
                    "default": _ws_state(_ws, "", "default"),
                    "accounts": {},
                }
                _accts = _ws.get("accounts")
                if isinstance(_accts, dict):
                    for _k in list(_accts)[:32]:
                        _plat, _, _aid = str(_k).partition(":")
                        _ws_out["accounts"][str(_k)] = _ws_state(
                            _ws, _plat, _aid or "default")
                snap["work_schedule"] = _ws_out
            else:
                snap["work_schedule"] = {"enabled": False}
        except Exception:
            pass
        return {"ok": True, "worker": snap, "human_deliver_wired": _hd_wired,
                "stale_approve_hours": _stale_h}

    @app.get("/api/drafts/pipeline-metrics")
    async def api_drafts_pipeline_metrics(
        request: Request, window_sec: int = 3600, _=Depends(api_auth),
    ):
        """统一草稿引擎规则栈的**只读聚合指标**（命中率/分位延迟/规则触发计数）。

        与 ``autosend-status`` 的区别：本端点**只需 API token、不强制主管会话**。
        返回的全部是非 PII 聚合数（无消息内容、无用户标识，见
        ``MetricsStore.get_inbox_draft_metrics``），故对持令牌的运营工具开放是安全的，
        用于 ``scripts/suggest_draft_thresholds`` / CI 闭合阈值校准回路
        （此前该脚本只能打主管会话端点，纯 token 拿不到指标）。

        ``window_sec``（60–86400，默认 3600）控制返回的 ``window`` 滑窗大小——与
        ``health_watchdog._check_draft_quality`` 评估告警所用窗口对齐，便于校准脚本按
        「watchdog 视角」与「稳态累计」两个口径同时观测。
        """
        out: dict = {"ok": True}
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ws = max(60, min(86400, int(window_sec or 3600)))
            out["draft_pipeline"] = get_metrics_store().get_inbox_draft_metrics(window_sec=ws)
        except Exception:
            out["draft_pipeline"] = {}
        return out

    @app.get("/api/drafts/audit")
    async def api_drafts_audit(
        request: Request,
        draft_id: str = "",
        agent_id: str = "",
        days: int = 7,
        limit: int = 200,
        _=Depends(api_auth),
    ):
        """草稿处置审计日志（主管专属）。可按 draft_id / agent_id / 天数过滤。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        svc = _get_draft_service(request)
        since_ts = time.time() - max(1, min(90, int(days or 7))) * 86400
        items = svc.list_audit(
            draft_id=draft_id or "",
            agent_id=agent_id or "",
            since_ts=since_ts,
            limit=max(1, min(500, int(limit or 200))),
        )
        return {"ok": True, "items": items, "total": len(items)}

    # ── C2 / D1 端点必须注册在 /{draft_id} 之前，防止被通配路由截获 ──

    @app.get("/api/workspace/copilot")
    async def api_workspace_copilot(
        request: Request,
        text: str = "",
        draft_id: str = "",
        conversation_id: str = "",
        _=Depends(api_auth),
    ):
        """AI Copilot：对客户来文做规则层全量分析 + KB 匹配（<10ms，无 LLM）。

        返回：intent, emotion, risk_level, risk_reasons, next_step, kb_matches, language
        用于 draft_review.html 内嵌 AI 洞察面板。
        """
        from src.ai.chat_assistant_service import (
            quick_analyze, _suggestions, detect_language,
            _detect_emotion, _detect_intent, _detect_risk,
        )
        t = str(text or "")
        analysis = quick_analyze(t)
        # F1：附带规则建议文本（最多3条），供前端快捷回复按钮展示
        suggestions: list = []
        if t.strip():
            try:
                lang = analysis.get("language", "zh")
                intent = analysis.get("intent", "")
                emotion = analysis.get("emotion", "平稳")
                risk = analysis.get("risk_level", "low")
                for s in _suggestions(t, lang=lang, intent=intent, emotion=emotion, risk=risk)[:3]:
                    suggestions.append({
                        "style": str(s.style or ""),
                        "title": str(s.title or ""),
                        "text": str(s.text or ""),
                    })
            except Exception:
                pass
        # KB 匹配（可选，kb_store 未挂载时返回空列表）
        kb_matches: list = []
        try:
            kb_store = getattr(request.app.state, "kb_store", None)
            if kb_store is not None and t.strip():
                result = kb_store.search(t, top_k=3)
                raw_entries = (result or {}).get("entries", [])
                for e in raw_entries[:3]:
                    kb_matches.append({
                        "entry_id": str(e.get("entry_id") or e.get("id") or ""),
                        "title": str(e.get("title") or ""),
                        "summary": str(e.get("summary") or e.get("answer") or "")[:120],
                        "score": float(e.get("score") or 0),
                    })
        except Exception:
            pass

        # J2：模板智能推荐——按 intent→scene + 客户语言从模板库精准检索
        template_suggestions: list = []
        try:
            inbox_store = getattr(request.app.state, "inbox_store", None)
            if inbox_store is not None and t.strip():
                _intent = str(analysis.get("intent") or "")
                _lang = str(analysis.get("language") or "zh")
                _scene = _intent_to_scene(_intent)
                tpls = inbox_store.list_templates(
                    language=_lang, scene=_scene, limit=3
                )
                if not tpls and _scene:
                    # 同场景、无语言限制 fallback（用于语言不完整的模板库）
                    tpls = inbox_store.list_templates(scene=_scene, limit=3)
                for tpl in tpls[:3]:
                    template_suggestions.append({
                        "id": str(tpl.get("id") or ""),
                        "title": str(tpl.get("title") or ""),
                        "content": str(tpl.get("content") or ""),
                        "scene": str(tpl.get("scene") or ""),
                        "language": str(tpl.get("language") or ""),
                    })
        except Exception:
            pass

        # Q2: 若有 draft_id，附带草稿质量评分（已存储在 reply_drafts）
        quality_info: dict = {}
        if draft_id:
            try:
                inbox_s = getattr(request.app.state, "inbox_store", None)
                if inbox_s is not None:
                    qr = inbox_s.get_draft_quality(draft_id)
                    if qr and qr["quality_score"] >= 0:
                        from src.inbox.quality import quality_to_badge
                        quality_info = {
                            "quality_score": qr["quality_score"],
                            "quality_breakdown": qr["breakdown"],
                            "quality_badge": quality_to_badge(qr["quality_score"]),
                        }
            except Exception:
                pass

        # Q3: 记录 KB 推荐事件（用于命中率监控）
        if kb_matches:
            try:
                inbox_s2 = getattr(request.app.state, "inbox_store", None)
                if inbox_s2 is not None:
                    import uuid as _uuid2
                    _agent = _session_agent_id(request)
                    for km in kb_matches:
                        _rec_id = _uuid2.uuid4().hex[:12]
                        km["_rec_id"] = _rec_id  # 返回给前端，用于点击时回调
                        inbox_s2.record_kb_recommendation(
                            rec_id=_rec_id,
                            entry_id=str(km.get("entry_id") or ""),
                            entry_title=str(km.get("title") or ""),
                            conversation_id=str(conversation_id or ""),
                            agent_id=str(_agent or ""),
                        )
            except Exception:
                pass

        return {
            "ok": True,
            "draft_id": draft_id,
            "conversation_id": conversation_id,
            **analysis,
            "suggestions": suggestions,
            "kb_matches": kb_matches,
            "template_suggestions": template_suggestions,  # J2
            **quality_info,  # Q2: quality_score, quality_breakdown, quality_badge
        }

    @app.get("/api/drafts/sla-overdue")
    async def api_drafts_sla_overdue(
        request: Request,
        hours: int = 4,
        _=Depends(api_auth),
    ):
        """列出 L3/L4 草稿中超过 SLA 时限（默认 4h）的待审草稿（主管专属）。

        用于顶栏 SLA 角标 + 草稿审批页 SLA 徽章。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        svc = _get_draft_service(request)
        threshold_ts = time.time() - max(1, min(72, int(hours or 4))) * 3600
        drafts = svc.list_drafts(status="pending", limit=200)
        overdue = [
            d for d in drafts
            if d.get("autopilot_level") in {"L3", "L4"}
            and float(d.get("created_ts") or 0) > 0
            and float(d.get("created_ts") or 0) < threshold_ts
        ]
        return {
            "ok": True,
            "count": len(overdue),
            "sla_hours": int(hours),
            "overdue": overdue,
        }

    # ── H1 草稿翻译 + H2 批量处置（必须在 /{draft_id} 之前注册） ──────────────

    @app.post("/api/drafts/{draft_id}/translate")
    async def api_drafts_translate(request: Request, draft_id: str, _=Depends(api_auth)):
        """H1：将草稿 AI 回复文本翻译为客户语言。

        优先使用 web_app.state.translation_service（若已配置），
        降级时返回原文（带 fallback 标记）。
        返回：{ok, translated, source_lang, target_lang, fallback, draft_id}
        """
        # 能力权限（2026-08-16）：草稿翻译=坐席主动消费翻译能力，同受 ai.translate 闸
        if not _perm_ok(request, "ai.translate"):
            raise HTTPException(403, tr(request, "err.perm.capability_denied"))
        svc = _get_draft_service(request)
        draft = svc.get_draft(draft_id)
        if draft is None:
            raise HTTPException(404, tr(request, "err.draft.not_found"))

        draft_text = str(draft.get("draft_text") or "")
        peer_text = str(draft.get("peer_text") or "")
        if not draft_text.strip():
            return {"ok": False, "error": tr(request, "err.draft.text_empty_no_translate"), "draft_id": draft_id}

        # 推断语言：source=中文（草稿），target=客户语言（从 peer_text 检测）
        from src.ai.chat_assistant_service import detect_language
        source_lang = str(draft.get("draft_lang") or "zh") or "zh"
        target_lang = detect_language(peer_text) if peer_text.strip() else "en"
        if target_lang in ("zh", "zh-TW", ""):
            target_lang = "en"  # 中文草稿对中文客户无需翻译，回退英文

        ts_svc = getattr(request.app.state, "translation_service", None)
        if ts_svc is None:
            return {
                "ok": True,
                "draft_id": draft_id,
                "translated": draft_text,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "fallback": True,
                "note": "翻译服务未启用，返回原文",
            }
        try:
            result = await ts_svc.translate(
                draft_text, target_lang=target_lang, source_lang=source_lang, style="chat"
            )
            translated = str(result.translated_text if hasattr(result, "translated_text")
                             else result.get("translated_text", draft_text))
            # 坐席字符计量归因（2026-08-16）：真翻译成功才记（源文本=草稿正文，
            # 与 /translate 的 len(text) 同口径）；无 ok 属性的旧结果形状按成功记。
            if getattr(result, "ok", True):
                record_request_chars(request, "translation", len(draft_text))
            return {
                "ok": True,
                "draft_id": draft_id,
                "translated": translated,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "fallback": False,
            }
        except Exception as e:
            logger.debug("草稿翻译失败: %s", e)
            return {
                "ok": True,
                "draft_id": draft_id,
                "translated": draft_text,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "fallback": True,
                "note": "翻译暂时不可用，返回原文",
            }

    @app.post("/api/drafts/bulk-resolve")
    async def api_drafts_bulk_resolve(request: Request, _=Depends(api_auth)):
        """H2：批量处置草稿。

        Body: {action: "approve"|"reject", draft_ids: [...], by?, reason?}
        ``reason=bulk_stale``（Q-31 D）：只拒绝 ``approve_block_reason==age`` 的 pending，
        任意已登录坐席可点（超龄稿本就不能原样发）；未给 draft_ids 时服务端自选。
        其它批量动作仍主管专属。
        返回：{ok, total, succeeded, failed, errors: [...]}
        """
        svc = _get_draft_service(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        action = str(body.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(400, tr(request, "err.draft.bad_action"))
        reason = str(body.get("reason") or "").strip()
        draft_ids = list(body.get("draft_ids") or [])
        if reason == "bulk_stale":
            if action != "reject":
                raise HTTPException(400, tr(request, "err.draft.bad_action"))
            pending = svc.list_drafts(status="pending", limit=500)
            stale_ids = [
                str(d.get("draft_id") or "")
                for d in pending
                if d.get("draft_id")
                and hasattr(svc, "approve_block_reason")
                and (svc.approve_block_reason(d) or "") == "age"
            ]
            if draft_ids:
                want = {str(x) for x in draft_ids}
                draft_ids = [i for i in stale_ids if i in want]
            else:
                draft_ids = stale_ids
            cap = 200
        else:
            if not _is_supervisor(request):
                raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
            if not draft_ids:
                return {"ok": True, "total": 0, "succeeded": 0, "failed": 0, "errors": []}
            cap = 50
        by = str(body.get("by") or _session_agent_id(request))
        agent_id = _session_agent_id(request)
        succeeded, failed, errors = 0, 0, []
        for did in draft_ids[:cap]:
            try:
                _kw = {"by": by or agent_id or "bulk"}
                if reason:
                    _kw["reason"] = reason
                try:
                    result = svc.resolve_with_audit(str(did), action, **_kw)
                except TypeError:
                    result = svc.resolve_with_audit(
                        str(did), action, by=by or agent_id or "bulk"
                    )
                if result.get("ok"):
                    succeeded += 1
                else:
                    failed += 1
                    errors.append({"draft_id": did, "error": result.get("error", "failed")})
            except Exception as e:
                failed += 1
                errors.append({"draft_id": did, "error": str(e)})
        return {
            "ok": True,
            "total": len(draft_ids),
            "succeeded": succeeded,
            "failed": failed,
            "errors": errors[:10],
        }

    @app.post("/api/drafts/persona-test")
    async def api_drafts_persona_test(request: Request, _=Depends(api_auth)):
        """人设试聊 → 存草稿：把试聊抽屉（/api/chat/test 预览）里满意的回复
        写进**既有草稿审批队列**，由坐席在 /workspace/drafts 人工审核后经既有链路发送。

        Body: {conversation_id: str, text: str, persona_id?: str, peer_text?: str}
        本端点只落库，绝不新建发送路径、绝不自动发送。
        风险评估与 DraftService.auto_generate_draft 同一条路径（quick_analyze +
        keyword_risk_level 取 max，再 risk_to_autopilot），保证同一风控口径。
        """
        svc = _get_draft_service(request)  # draft_service 未挂载 → 503
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            store = getattr(svc, "_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        conversation_id = str(body.get("conversation_id") or "").strip()
        text = str(body.get("text") or "").strip()
        persona_id = str(body.get("persona_id") or "").strip()
        peer_text = str(body.get("peer_text") or "")
        if not conversation_id:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        if not text:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="text"))
        if len(text) > 2000:
            raise HTTPException(400, tr(request, "err.rpa.message_too_long"))

        # 会话存在性以 conversations 主表为准（get_conversation：单条 SELECT *，
        # 与批量版 get_conversations_for_ids 同表同列，单 id 场景更直接）。
        # 绝不能拿 conversation_meta（get_conv_meta）当 404 依据：那是 I1 智能分析
        # 元数据表，只有分析管线跑过的会话才有行，普通刚 ingest/upsert 的有效会话
        # 查它返回 None，会被误判成 404；且它也没有 account_id/chat_key/display_name。
        try:
            conv = store.get_conversation(conversation_id)
        except Exception:
            conv = None
        if conv is None:
            raise HTTPException(
                404,
                tr(request, "err.draft.ptest_conv_not_found",
                   "会话不存在，请先在统一收件箱确认"),
            )

        try:
            import uuid as _uuid
            from src.ai.chat_assistant_service import quick_analyze
            from src.inbox.drafts import _max_risk, keyword_risk_level, risk_to_autopilot

            # 风险评估：与 auto_generate_draft 完全同一条路径（规则层，<1ms 无 LLM）
            analysis = quick_analyze(text)
            risk_level = _max_risk(
                analysis.get("risk_level", "low"),
                keyword_risk_level(text),
            )
            # automation_mode 硬编码 "review"：人设试聊草稿永远走人工审批，
            # 绝不能落进 L2 自动投递批次（review 模式只产 L1/L3/L4）。
            autopilot = risk_to_autopilot(risk_level, "review")

            draft: dict = {
                "source_kind": "inbox",
                # 独立幂等键：绝不用裸 conv_id 作 source_id——那是 auto_generate_draft
                # 的每会话幂等键（uq_drafts_source 冲突合并），复用会与自动草稿互相覆盖。
                "source_id": "ptest_" + _uuid.uuid4().hex,
                "conversation_id": conversation_id,
                "platform": str(conv.get("platform") or ""),
                "account_id": str(conv.get("account_id") or "default"),
                "chat_key": str(conv.get("chat_key") or ""),
                "chat_name": str(conv.get("display_name") or ""),
                "peer_text": peer_text,
                "draft_text": text,
                "draft_lang": analysis.get("language", "zh"),
                "risk_level": risk_level,
                "risk_reasons": analysis.get("risk_reasons") or [],
                "autopilot_level": autopilot,
                "status": "pending",
            }
            if persona_id:
                # trace_id 为 reply_drafts 既有字段，此处用作来源标记
                # （表内无专门 origin 列；S3 全链路查询也能按它检索到试聊草稿）
                draft["trace_id"] = f"ptest:{persona_id}"
            else:
                # 可选增强：无 persona_id 时照 auto_generate_draft 的 S3 写法继承
                # 会话 trace_id。conv_meta 只有分析管线跑过的会话才有行，
                # 故仅 best-effort 取值，绝不作为会话存在性/404 依据。
                try:
                    _cm = store.get_conv_meta(conversation_id) or {}
                    _tid = str(_cm.get("trace_id") or "")
                    if _tid:
                        draft["trace_id"] = _tid
                except Exception:
                    pass
            draft_id = store.upsert_draft(draft)
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("persona-test 存草稿失败: %s", e, exc_info=True)
            raise HTTPException(500, tr(request, "err.rpa.op_failed",
                                        op="persona_test_draft", err=e))

        # 审计（best-effort，照本文件 kb-archive 端点的写法直写 record_draft_audit）
        try:
            store.record_draft_audit(
                draft_id,
                autopilot_level=autopilot,
                action="persona_test_draft",
                agent_id=_session_agent_id(request) or "persona_test",
                reason=(f"persona_id={persona_id}" if persona_id else "persona_test"),
                risk_level=risk_level,
                conversation_id=conversation_id,
            )
        except Exception:
            pass

        return {
            "ok": True,
            "draft_id": draft_id,
            "risk_level": risk_level,
            "autopilot_level": autopilot,
            "review_url": "/workspace/drafts",
        }

    @app.get("/api/drafts/{draft_id}")
    async def api_drafts_get(request: Request, draft_id: str, _=Depends(api_auth)):
        svc = _get_draft_service(request)
        draft = svc.get_draft(draft_id)
        if draft is None:
            raise HTTPException(404, tr(request, "err.draft.not_found"))
        return {"ok": True, "draft": draft}

    @app.post("/api/drafts/{draft_id}/regenerate")
    async def api_drafts_regenerate(request: Request, draft_id: str,
                                    _=Depends(api_auth)):
        """陈旧稿一键重生成（P1 2026-08-09，stale 护栏 409 的出路闭环）。

        stale_approve_hours 护栏把老稿拦下后，坐席此前只有「编辑改写」或
        「去输入框重新生成再手发」两条手工路。本端点＝按**当前**会话上下文
        重走人设产线（``generate_persona_reply``，与全自动草稿/composer AI
        同一条产线）→ 生成成功后**原子作废**旧稿（竞态窗内被同事处置 →
        409 already_resolved，不铸新稿）→ 铸新 pending 稿。

        安全语义：新稿 autopilot 按 review 口径定级（只产 L1/L3/L4）——
        重生成是人工审阅流，**绝不**产 L2 落进自动投递批次。
        """
        svc = _get_draft_service(request)
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            store = getattr(svc, "_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        old = store.get_draft(draft_id)
        if old is None:
            raise HTTPException(404, tr(request, "err.draft.not_found"))
        if str(old.get("status") or "") not in ("pending", "enriching"):
            raise HTTPException(409, tr(request, "err.draft.already_resolved"))
        cid = str(old.get("conversation_id") or "")
        platform = str(old.get("platform") or "")
        chat_key = str(old.get("chat_key") or "")
        account_id = str(old.get("account_id") or "default")

        # 按**当前**会话上下文生成（老稿之所以老，就是因为情境已经变了）
        from src.inbox.persona_reply import generate_persona_reply, normalize_history
        rows = []
        try:
            if cid and hasattr(store, "list_recent_messages"):
                from src.ai.context_depth import history_fetch_limit as _hfl
                rows = store.list_recent_messages(cid, limit=_hfl(None, 30)) or []
        except Exception:
            rows = []
        msgs = []
        for r in rows:
            try:
                _txt = str((r.get("text") or r.get("original_text") or "")).strip()
                if _txt:
                    msgs.append({"direction": str(r.get("direction") or "in"),
                                 "text": _txt})
            except Exception:
                continue
        history, last_inbound = normalize_history(msgs)
        if not last_inbound:
            last_inbound = str(old.get("peer_text") or "")
        if not last_inbound:
            raise HTTPException(400, tr(request, "err.ws.no_conversation_context"))
        # P3：最近一条入站的平台 message_id → 案例 mid 锚点（rows 已升序）
        _inbound_mid = ""
        try:
            for r in reversed(rows or []):
                if str(r.get("direction") or "") != "in":
                    continue
                mid = str(r.get("message_id") or "").strip()
                if mid and mid not in ("0",):
                    _inbound_mid = mid
                    break
        except Exception:
            _inbound_mid = ""
        out = await generate_persona_reply(
            app=request.app, platform=platform, chat_key=chat_key,
            last_inbound=last_inbound, history=history,
            conversation_id=cid, account_id=account_id,
            inbound_msg_id=_inbound_mid)
        reply = str((out or {}).get("reply") or "").strip()
        if not (out or {}).get("ok") or not reply:
            raise HTTPException(502, tr(request, "err.draft.regen_failed"))

        by = _session_agent_id(request) or "regen"
        # 生成成功才作废旧稿；原子闸门（仅 pending/enriching 可转）防竞态双活
        cancelled = store.update_draft_status(
            draft_id, status="cancelled", decided_by=f"regen:{by}")
        if not cancelled:
            raise HTTPException(409, tr(request, "err.draft.already_resolved"))

        import uuid as _uuid
        from src.ai.chat_assistant_service import quick_analyze
        from src.inbox.drafts import _max_risk, keyword_risk_level, risk_to_autopilot
        analysis = quick_analyze(reply)
        risk_level = _max_risk(
            analysis.get("risk_level", "low"), keyword_risk_level(reply))
        autopilot = risk_to_autopilot(risk_level, "review")
        new_id = store.upsert_draft({
            "source_kind": "inbox",
            "source_id": "regen_" + _uuid.uuid4().hex,
            "conversation_id": cid,
            "platform": platform,
            "account_id": account_id,
            "chat_key": chat_key,
            "chat_name": str(old.get("chat_name") or ""),
            "peer_text": last_inbound,
            "draft_text": reply,
            "draft_lang": str(out.get("reply_lang") or ""),
            "risk_level": risk_level,
            "risk_reasons": analysis.get("risk_reasons") or [],
            "autopilot_level": autopilot,
            "status": "pending",
            "trace_id": f"regen:{draft_id}",
        })
        try:
            store.record_draft_audit(
                new_id, autopilot_level=autopilot, action="regenerate_draft",
                agent_id=by, reason=f"from={draft_id}",
                risk_level=risk_level, conversation_id=cid)
        except Exception:
            pass
        return {"ok": True, "draft_id": new_id, "cancelled": draft_id,
                "risk_level": risk_level, "autopilot_level": autopilot,
                "draft_text": reply}

    @app.post("/api/drafts/{draft_id}/resolve")
    async def api_drafts_resolve(request: Request, draft_id: str, _=Depends(api_auth)):
        """带 L4 拦截 + 敏感词强制升级 + 审计的统一处置（B2）。

        Body: {action, text?, by?}
        action: approve / reject / edit_send / cancel / autosend（L2 自动路径）
        """
        svc = _get_draft_service(request)
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        text = str(body.get("text") or "")
        by = str(body.get("by") or "") or _session_agent_id(request)
        result = svc.resolve_with_audit(draft_id, action, text=text, by=by)
        if not result.get("ok"):
            code = int(result.get("code") or 400)
            # 两种 409 语义不同，别混成一句话（都回 409 但坐席该做的事完全不同）：
            # too_stale＝这稿太老，原样发会穿帮 → 重新生成或改写后发；
            # already_resolved＝刚被其他窗口/同事处置 → 刷新即可（多开防重，非故障）。
            if result.get("too_stale"):
                # 两种陈旧成因也分开说：已回过（会重复/自相矛盾）vs 单纯过期（脱节）
                _key = ("err.draft.stale_replied"
                        if result.get("stale_reason") == "replied"
                        else "err.draft.too_stale")
                raise HTTPException(409, tr(
                    request, _key,
                    age=int(result.get("age_hours") or 0),
                    limit=int(result.get("max_age_hours") or 0)))
            if code == 409 or result.get("already_resolved"):
                raise HTTPException(409, tr(request, "err.draft.already_resolved"))
            raise HTTPException(code, result.get("error") or "处置失败")
        return result

    @app.post("/api/drafts/{draft_id}/force-override")
    async def api_drafts_force_override(
        request: Request, draft_id: str, _=Depends(api_auth),
    ):
        """主管强制放行 L4 草稿（force_override=True）。主管专属。

        Body: {action?, text?, reason?}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_force_l4"))
        svc = _get_draft_service(request)
        body = await request.json()
        action = str(body.get("action") or "approve").strip().lower()
        text = str(body.get("text") or "")
        by = _session_agent_id(request) or str(body.get("by") or "")
        result = svc.resolve_with_audit(
            draft_id, action, text=text, by=by, force_override=True,
        )
        if not result.get("ok"):
            code = int(result.get("code") or 400)
            raise HTTPException(code, result.get("error") or "强制放行失败")
        return result

    @app.post("/api/drafts/bulk-autosend")
    async def api_drafts_bulk_autosend(
        request: Request, _=Depends(api_auth),
    ):
        """批量触发所有 L2（低风险 + auto_ai）草稿自动发送。

        适用场景：定时任务 / 坐席手动触发"一键自动发所有 L2"。
        返回 {ok, sent, errors}。
        """
        svc = _get_draft_service(request)
        by = _session_agent_id(request) or "system"
        drafts = svc.list_drafts(status="pending", limit=200)
        sent, errors = 0, 0
        for d in drafts:
            if d.get("autopilot_level") != "L2":
                continue
            # deliver=True：人工触发的批量 autosend 也要真投递（AutosendWorker 只投递
            # 自己 resolve 的批次；此前该路由只标记 approved，客户实际收不到）。
            result = svc.resolve_with_audit(
                d["draft_id"], "autosend", by=by, deliver=True,
            )
            if result.get("ok"):
                sent += 1
            else:
                errors += 1
        return {"ok": True, "sent": sent, "errors": errors}

    @app.post("/api/drafts/expire-stale")
    async def api_drafts_expire_stale(request: Request, _=Depends(api_auth)):
        """治理：作废搁置过久的 pending 草稿（转 cancelled + 审计，不删行）。主管专属。

        Body（均可选）：
          max_age_hours: int=168   超此时长仍 pending 即作废
          levels: list=["L3","L4"] 仅这些等级；[] = 全部
          groups_only: bool=true   仅群/频道会话（防误伤 1:1 私聊待审）
          dry_run: bool=false      true = 只预览命中数与清单，不写库
        返回 {ok, dry_run, count, drafts:[{draft_id,autopilot_level,conversation_id,age_hours}]}。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = getattr(request.app.state, "inbox_store", None)
        if store is None or not hasattr(store, "expire_stale_pending_drafts"):
            raise HTTPException(503, tr(request, "err.svc.draft_service_disabled"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            max_age = float(body.get("max_age_hours", 168) or 168)
        except (TypeError, ValueError):
            max_age = 168.0
        levels = body.get("levels")
        if levels is None:
            levels = ["L3", "L4"]
        levels = [str(x) for x in (levels or []) if str(x)]
        groups_only = bool(body.get("groups_only", True))
        dry_run = bool(body.get("dry_run", False))
        by = _session_agent_id(request) or "system"
        victims = store.expire_stale_pending_drafts(
            max_age_hours=max_age,
            levels=levels or None,
            groups_only=groups_only,
            agent_id=by,
            dry_run=dry_run,
        )
        return {
            "ok": True,
            "dry_run": dry_run,
            "count": len(victims),
            "drafts": victims,
        }



# ── J3：数据导出 API（独立注册函数，由 admin.py + main.py 共同调用）──────────

def register_metrics_route(app, *, api_auth):
    """L1：注册 GET /api/workspace/metrics（系统指标，主管专属）。

    format=json（默认）→ JSON 对象
    format=prometheus   → Prometheus text format（# HELP / # TYPE / metric lines）
    """
    import io
    from fastapi import Depends
    from fastapi.responses import PlainTextResponse

    @app.get("/api/workspace/metrics")
    async def api_workspace_metrics(
        request: Request,
        format: str = "json",
        _=Depends(api_auth),
    ):
        fmt = (format or "json").strip().lower()
        # Phase9: Prometheus scrape uses static Bearer auth_token (no browser session).
        # api_auth already validated the token; allow text export for machine scrapers
        # without opening JSON metrics to non-supervisor humans.
        auth_h = request.headers.get("Authorization", "") or ""
        bearer_ok = auth_h.startswith("Bearer ") and len(auth_h) > 8
        if fmt == "prometheus":
            if not (_is_supervisor(request) or bearer_ok):
                raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        elif not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        # ── 聚合各子系统指标 ──────────────────────────────────────
        metrics: dict = {"ts": time.time()}

        # AutosendWorker
        try:
            w = getattr(request.app.state, "autosend_worker", None)
            if w is not None:
                snap = w.status_snapshot()
                metrics["autosend"] = snap
            else:
                metrics["autosend"] = {"running": False}
        except Exception:
            metrics["autosend"] = {"running": False}

        # SLAWatcher (K1/K2)
        try:
            sw = getattr(request.app.state, "sla_watcher", None)
            if sw is not None:
                metrics["sla_watcher"] = sw.status_snapshot()
            else:
                metrics["sla_watcher"] = {"running": False}
        except Exception:
            metrics["sla_watcher"] = {"running": False}

        # P3：AutoClaimWorker（auto_assign 自动认领执行端）
        try:
            acw = getattr(request.app.state, "auto_claim_worker", None)
            metrics["auto_claim"] = (acw.status_snapshot()
                                     if acw is not None else {"running": False})
        except Exception:
            metrics["auto_claim"] = {"running": False}

        # 入站翻译存量消化 worker（默认关；扫描/开工/译出累计）
        try:
            bfw = getattr(request.app.state, "inbound_backfill_worker", None)
            if bfw is not None:
                metrics["inbound_backfill"] = bfw.status_snapshot()
        except Exception:
            pass

        # WebhookNotifier (L2)
        try:
            whn = getattr(request.app.state, "webhook_notifier", None)
            if whn is not None:
                metrics["webhook"] = whn.status_snapshot()
            else:
                metrics["webhook"] = {"running": False}
        except Exception:
            metrics["webhook"] = {"running": False}

        # ScheduledReporter (N2)
        try:
            rpt = getattr(request.app.state, "scheduled_reporter", None)
            metrics["scheduled_reporter"] = rpt.status_snapshot() if rpt is not None else {"running": False}
        except Exception:
            metrics["scheduled_reporter"] = {"running": False}

        # P3 账号真相观测（2026-08-17）：账号名录规模趋势——已退出/已移除/仅历史号
        # 异常增长（频繁掉配对、误删）在这里先看见，而不是等坐席报「对不上号」。
        # directory_only=会话库有、注册表没有的号（含 config 来源活跃号的恒定底数，
        # 看趋势不看绝对值）。失败静默：观测键绝不拖垮 metrics 主体。
        try:
            from src.integrations.account_registry import get_account_registry
            _inb = getattr(request.app.state, "inbox_store", None)
            _reg_rows = get_account_registry().list(include_removed=True) or []
            _by_st: dict = {}
            _reg_keys = set()
            for _r in _reg_rows:
                _st = str(_r.get("status") or "unknown")
                _by_st[_st] = _by_st.get(_st, 0) + 1
                _reg_keys.add((str(_r.get("platform") or ""),
                               str(_r.get("account_id") or "")))
            _directory = (_inb.account_directory()
                          if _inb is not None
                          and hasattr(_inb, "account_directory") else {})
            from src.web.routes.unified_inbox_aggregate import directory_ghost_keys
            _ghosts = directory_ghost_keys(_directory, _reg_keys)
            _desktop = sum(
                1 for _r in _reg_rows
                if str(_r.get("mode") or "") == "desktop"
                and str(_r.get("status") or "") != "removed")
            metrics["accounts_truth"] = {
                "registry_total": len(_reg_rows),
                "registry_by_status": _by_st,
                "directory_total": len(_directory),
                # directory_only 保留旧键（趋势不断档）；history_only 剔除 web 工作台
                # 后才是真幽灵——与 accounts_summary 的 history_only 同口径。
                "directory_only": len(_ghosts),
                "history_only": len(_ghosts),
                "desktop": _desktop,
            }
        except Exception:
            pass

        # P1-9 账号健康（2026-08-29）：冻结/掉线/近7天风控一份快照——
        # ops「🛡️ 账号健康」卡数据源；全部 peek 既有单例，逐段软失败
        try:
            from src.ops.account_health import collect_account_health
            metrics["account_health"] = collect_account_health()
        except Exception:
            pass

        # InboxStore 草稿统计
        try:
            inbox = getattr(request.app.state, "inbox_store", None)
            if inbox is not None:
                try:
                    metrics["inbox_dedup"] = inbox.dedup_stats()
                except Exception:
                    pass
                svc = getattr(request.app.state, "draft_service", None)
                if svc is not None:
                    all_drafts = svc.list_drafts(status="pending", limit=1000)
                    by_level: dict = {}
                    for d in all_drafts:
                        lv = str(d.get("autopilot_level") or "?")
                        by_level[lv] = by_level.get(lv, 0) + 1
                    metrics["drafts"] = {
                        "pending_total": len(all_drafts),
                        "by_level": by_level,
                    }
                # EventBus 订阅者数
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    metrics["event_bus"] = {
                        "subscriber_count": get_event_bus().subscriber_count,
                        "history_size": len(get_event_bus().recent_events(50)),
                    }
                except Exception:
                    pass
        except Exception:
            pass

        # P57：翻译引擎用量（调用/成功率/延迟/降级）
        try:
            from src.ai.translation_engine_stats import get_translation_engine_stats
            metrics["translation_engines"] = get_translation_engine_stats().dump()
        except Exception:
            pass

        # P1-4：出向翻译漏斗（覆盖率/auto 解析失败率/降级率/按语言分布）
        try:
            from src.ai.outbound_translation_stats import get_outbound_translation_stats
            metrics["outbound_translation"] = get_outbound_translation_stats().dump()
        except Exception:
            pass

        # P0 多开治理：发送幂等去重（双窗口/双击重复提交拦截量；entries=当前占位窗口）
        try:
            from src.inbox.send_dedup import get_send_dedup
            metrics["send_dedup"] = get_send_dedup().snapshot()
        except Exception:
            pass

        # 所听即所发（P1 2026-08-05）：语音试听产物复用——hits/attempts/hit_rate +
        # miss 原因分布（expired 多→放宽 TTL；text_mismatch 多→改稿没重生成）
        try:
            from src.integrations.shared.tts_preview import reuse_stats_snapshot
            metrics["voice_preview_reuse"] = reuse_stats_snapshot()
        except Exception:
            pass

        # 未读可信化 v2（2026-08-23）：工作台已读→平台回执的推送/节流/成败计数
        # （pushed/push_ok/push_fail/skipped_*）。进程口径重启清零；开关关＝全 0。
        # 「回执链活着吗」从翻日志变成读数（push_fail 持续涨=worker mark_read 断）。
        try:
            from src.inbox.read_sync import stats_snapshot as _read_sync_stats
            metrics["read_sync"] = _read_sync_stats()
        except Exception:
            pass

        # spoken_style 真人感文本层灰度（2026-08-11，AvatarHub 交付包桥接）：
        # l1_inject/l2_inject=注入量、l2_skip_lang=zh_only 外语拦截量（应随外语消息同步涨）、
        # l3_changed=出口清洁真剥了东西、l4_*=改写尝试/生效/直通（直通率>30% 该反馈 AvatarHub 线）。
        # 进程内累计（重启清零）；桥接未启用/包缺席时全 0，照常暴露便于区分「没开」和「没流量」。
        try:
            from src.ai.spoken_style_bridge import stats as _spoken_style_stats
            metrics["spoken_style"] = _spoken_style_stats()
        except Exception:
            pass
        # #40 地区语气档（I-4 D1）：resolve_* 各解析来源计数（人设显式/粤语 dialect/
        # 居住地/会话「发→」/全局默认）+ observed（非 CN 档出站被观测条数）+
        # banned_hit（命中大陆口语禁用词）+ script_hit（繁體档漏简体字）。
        # 命中率 = hit/observed；高了再决定要不要 L4 带负样本重写（现只观测不改文本）。
        try:
            from src.ai.persona_region import stats as _persona_region_stats
            metrics["persona_region"] = _persona_region_stats()
        except Exception:
            pass

        # 风险放行影子台账（#160 I-1，2026-09-04 老板拍板 v2：扣稿全放行、触发只写台账）：
        # total/today/by_reason/by_level/by_stage/top_hits + stop_contact/self_harm 单列。
        # 进程口径重启清零；持久口径是 logs/autosend_shadow/*.jsonl（CLI
        # tools/autosend_shadow_report.py）。恒暴露（total=0 时 ops 卡整卡隐藏）。
        try:
            from src.inbox.autosend_shadow_log import stats_snapshot as _ashadow_stats
            metrics["autosend_shadow"] = _ashadow_stats()
        except Exception:
            pass

        # 「AI 指纹」四格（P-1 D #259 #254）：近 24h 破折号率 / 客服腔命中率 / 引用无锚点 /
        # 承诺无动作（+ 发送门兜底命中）。进程事件环 + logs/ai_fingerprint/*.jsonl 回填，
        # 重启不清零。恒暴露（无样本时卡片按 na 隐藏）。
        try:
            from src.inbox.ai_fingerprint_stats import snapshot as _aifp_snapshot
            metrics["ai_fingerprint"] = _aifp_snapshot(hours=24)
        except Exception:
            pass

        # 出站文本形态守卫（实施74 B118/B121/B104）：monologue=括号独白拦截、
        # lang_mix_hard/soft=语种混杂剥除/观测、unfounded_recall=无出处引用剥句。
        # 进程口径重启清零；恒暴露（全 0=没流量或没命中，与「没接」可区分）。
        try:
            from src.ai.outbound_text_guard import guard_stats as _otg_stats
            metrics["outbound_text_guard"] = _otg_stats()
        except Exception:
            pass

        # 出站收口点守卫（实施91 #97/#105/#106）：呼格纠正（互换/近形/人设名）、
        # 铆定语言冲突（检出/翻译/HOLD/放行）、语音合成前收口两计数。skuio 复测
        # 验收期值守直接读这里判「守卫在不在动手」。进程口径重启清零；恒暴露。
        try:
            from src.ai.sendpoint_guard import sendpoint_guard_stats
            metrics["sendpoint_guard"] = sendpoint_guard_stats()
        except Exception:
            pass

        # 图片翻译链路观测（P1-OBS 2026-08-19）：识别/贴回量、OCR 后端分布
        # （ppocr 微服务灰度决策读数）、块级覆盖率、目标语分布。进程口径重启清零；
        # 零流量 active=false（ops 卡整卡隐藏）。
        try:
            from src.ai.image_xlate_stats import get_image_xlate_stats
            metrics["image_xlate"] = get_image_xlate_stats().dump()
        except Exception:
            pass

        # UI 语言来源分布（系统语言自动跟随 2026-08-27）：negotiated 占比 = 自动
        # 跟随的真实使用量；negotiated_by_lang = 语种校对优先级的真实数据源。
        try:
            from src.web.ui_lang_stats import get_ui_lang_stats
            metrics["ui_lang"] = get_ui_lang_stats().dump()
        except Exception:
            pass

        # Token 计费账本（2026-08-19 Token 定价改版观测三件套）：钱包余额/累计消耗/
        # 按动作分布 + P5b 投递影子计数（出稿↔投递校准）+ P4 公平使用水表 + P6 enforce
        # 态。enabled=False 时全零形状照常暴露（区分「没开」和「没流量」）。
        try:
            from src.licensing.token_ledger import metrics_snapshot as _tok_metrics
            metrics["token_ledger"] = _tok_metrics()
        except Exception:
            pass

        # 发送护栏拦截（P2 2026-08-13）：真实发送尝试被 Kill-Switch/金丝雀/授权/
        # 反封号闸门拦下的进程口径计数（预判/横幅轮询 notify=False 不计——读路径
        # 不污染）；键带 |manual/|auto 后缀（额度族）供「谁在被拦」分道观测。
        try:
            from src.integrations.shared.send_guard import block_stats_snapshot
            metrics["send_gate_blocks"] = block_stats_snapshot()
        except Exception:
            pass

        # 跨平台档案（origin_profile P2 2026-08-18）：provider 消费/渲染命中（进程口径，
        # blocks=AI 真吃到背景的次数）+ 合并联动记忆合流量 + 档案/导入台账总量
        # （contacts.db 持久口径，重启不清零）。contacts 未启用 → 只出进程计数。
        try:
            from src.contacts.origin_context import origin_stats_snapshot
            _osnap: Dict[str, Any] = dict(origin_stats_snapshot())
            _contacts_sub = getattr(request.app.state, "contacts", None)
            _cstore = getattr(_contacts_sub, "store", None) if _contacts_sub else None
            if _cstore is not None and hasattr(_cstore, "origin_profile_totals"):
                _osnap["totals"] = _cstore.origin_profile_totals()
            _ocfg_snap = (getattr(_contacts_sub, "config_snapshot", None) or {}) \
                if _contacts_sub else {}
            _osnap["enabled"] = bool(
                ((_ocfg_snap.get("origin_profile") or {}).get("enabled", False)))
            metrics["origin_profile"] = _osnap
        except Exception:
            pass

        # 入站自动翻译（同步预算 + 后台补译）：sync/bg 三态、deferred、负缓存拦截 +
        # 瞬时 in-flight（后台积压/引擎宕机的工程观测；按日漏斗另见 dashboard translation_inbound）
        try:
            from src.ai.inbound_translation_stats import get_inbound_translation_stats
            from src.workspace.inbound_translate import runtime_snapshot
            _inx = get_inbound_translation_stats().dump(runtime=runtime_snapshot())
            # 7 天趋势（store 按日漏斗，跨重启）：进程计数只能看当次运行，
            # sparkline 需要日粒度 → 顺手从 inbound_xlate_daily 读出附上。
            try:
                _ibx_store = getattr(request.app.state, "inbox_store", None)
                if _ibx_store is not None and hasattr(_ibx_store, "get_inbound_xlate_stats"):
                    _inx["trend_7d"] = _ibx_store.get_inbound_xlate_stats(
                        time.time() - 7 * 86400).get("trend", [])
            except Exception:
                pass
            metrics["inbound_translation"] = _inx
        except Exception:
            pass

        # V：语音克隆合成的「语言纠正」观测（合成语言随文本语种，防中文声纹念英文；纠正率/按语种分布）
        try:
            from src.ai.voice_synth_stats import get_voice_synth_stats
            metrics["voice_synth_language"] = get_voice_synth_stats().dump()
        except Exception:
            pass

        # 音频情绪识别（SER）观测：从声学语气听出的情绪分布 + 模型可用性（软降级次数）
        try:
            from src.ai.speech_emotion_stats import get_speech_emotion_stats
            metrics["speech_emotion"] = get_speech_emotion_stats().dump()
        except Exception:
            pass

        try:
            from src.ai.inbound_video_stats import get_inbound_video_stats
            metrics["inbound_video"] = get_inbound_video_stats().dump()
        except Exception:
            pass

        # AvatarHub 语音观测：合成成败/延迟/通道分布 + 预渲染命中 + GPU 队列水位 + STT
        try:
            from src.ai.avatar_voice_stats import get_avatar_voice_stats
            metrics["avatar_voice"] = get_avatar_voice_stats().dump()
        except Exception:
            pass

        # LINE 媒体收发：出站默认关，放量与否看这里的读数（尤其 orphan_recalled=
        # 「先发占位、传字节失败后撤回」的次数，它是那条两步链在真实网络下的稳定度）。
        # JSON 侧**不按 active 过滤**：零流量时的 active:false 本身就是「接线在、只是没
        # 用上」的确认；Prometheus 侧才过滤，免得没有 LINE 号的部署长期挂一串零序列。
        try:
            from src.integrations.line_media_stats import get_line_media_stats
            metrics["line_media"] = get_line_media_stats().dump()
        except Exception:
            pass

        # B 线 autosend 媒体出站：语音 provider/截断 + 发图失败原因分布
        try:
            from src.inbox.voice_autosend import metrics_snapshot as _vms
            metrics["autosend_voice"] = _vms()
        except Exception:
            pass
        try:
            from src.inbox.image_autosend import metrics_snapshot as _ims
            metrics["autosend_image"] = _ims()
        except Exception:
            pass

        # 命理技能观测：话题触达→生辰采集→灵签/命盘供给→详批变现 漏斗四段
        try:
            from src.companion.bazi_stats import get_bazi_stats
            metrics["bazi"] = get_bazi_stats().dump()
        except Exception:
            pass

        # 唱歌能力观测（实施58 P1）：requests/sent/no_stock/capped 计数 +
        # 按模板分布（进程口径；备货盘点走 avatar-status singing 段）
        try:
            from src.companion.song_stock import metrics_snapshot as _song_ms
            metrics["singing"] = _song_ms()
        except Exception:
            pass

        # 报障群 AI 值守观测（bug_intake）：分类计数 + 今日工单 + 开放工单分布
        try:
            from src.ops.bug_intake import dump_stats as _bi_dump
            metrics["bug_intake"] = _bi_dump()
        except Exception:
            pass

        # 真实世界接轨观测（P0 人设时钟 / P1 天气 / P2 用户侧时钟 + 双侧节日）。
        # 三者都是「静默降级」型能力：推不出时区、天气拉不到、节日缺库，链路照常跑但
        # 价值悄悄归零——不在看板上给出读数就等于没上线。
        try:
            _rw: Dict[str, Any] = {}
            try:
                from src.companion.weather_state import dump_stats as _wx_dump
                _rw["weather"] = _wx_dump()
            except Exception:
                _rw["weather"] = {}
            try:
                from src.companion.user_clock import dump_stats as _uc_dump
                _rw["clock_core"] = _uc_dump()
            except Exception:
                _rw["clock_core"] = {}
            try:
                from src.companion.user_clock_resolver import (
                    distribution as _uc_dist, dump_stats as _ucr_dump,
                )
                _rw["clock"] = _ucr_dump()
                _rw["sources"] = _uc_dist()
            except Exception:
                _rw["clock"], _rw["sources"] = {}, {}
            try:
                from src.companion.locale_holidays import (
                    load_calendar as _hol_cal, lunar_available as _lunar_ok,
                )
                _cal = _hol_cal() or {}
                _ctys = _cal.get("countries") or {}
                _rw["holidays"] = {
                    "countries": len(_ctys),
                    "entries": sum(
                        len(v or []) for v in _ctys.values()
                        if isinstance(v, (list, tuple))),
                    "lunar_ok": bool(_lunar_ok()),
                }
            except Exception:
                _rw["holidays"] = {}
            # active：任一子系统真的动过（全零 → ops 卡整卡隐藏，不占版面）
            _rw["active"] = bool(
                sum(int(v or 0) for v in (_rw.get("weather") or {}).values())
                or sum(int(v or 0) for v in (_rw.get("clock") or {}).values())
                or sum(int(v or 0) for v in (_rw.get("clock_core") or {}).values())
            )
            metrics["real_world"] = _rw
        except Exception:
            pass

        # 中央凭据池观测：池分配 vs 回落自带的比例（pool_share）+ 生效会员档 + 回落原因。
        # 中央池的失败是静默降级，没有这组数就看不出「池到底有没有在生效」。
        try:
            from src.integrations.credpool_stats import get_credpool_stats
            _cp = get_credpool_stats().dump()
            # 风控隔离三盾覆盖率（按**在册账号**算，不是按登录事件比率——
            # 运营要回答的是「我的号里有几个真被隔离了」）。即使本进程还没发生过
            # 分配（active=false），只要有协议号就该看得见覆盖率。
            try:
                from src.integrations.isolation_shields import collect_shields
                _cp["shields"] = collect_shields()
            except Exception:
                pass
            # 池服务自身的健康只有外部看门狗知道（health 200 但 allocate 已死的
            # 「半死」形态本项目吃过 2h20m 的亏）。路径由配置给出，客户桌面不配
            # 这个键 → 这一段自然不存在。
            try:
                _wcm = getattr(request.app.state, "config_manager", None)
                _wcfg = (_wcm.config if _wcm is not None else {}) or {}
                _wpath = ((((_wcfg.get("platform_login") or {}).get("telegram") or {})
                           .get("credpool") or {}).get("watchdog_state_path") or "")
                if _wpath:
                    from src.integrations.credpool_stats import watchdog_state
                    _wd = watchdog_state(str(_wpath))
                    if _wd:
                        _cp["watchdog"] = _wd
            except Exception:
                pass
            if _cp.get("active") or (_cp.get("shields", {}).get("total") or 0) > 0:
                metrics["credpool"] = _cp
        except Exception:
            pass

        # 营销目标观测：建目标→每日拍（含 hold 分桶）→注入生成链→主动桥真发→终态
        try:
            from src.companion.goals.stats import get_goal_stats
            metrics["goals"] = get_goal_stats().dump()
        except Exception:
            pass

        # 跨平台身份影子扫描（P3.2）：读周期扫描落的 state 文件快照（绝不在请求里
        # 现场扫库）；未启用 → {"enabled": false}，ops 卡据此整卡隐藏。
        try:
            _iscm = getattr(request.app.state, "config_manager", None)
            if _iscm is not None:
                from pathlib import Path as _ISPath
                from src.utils.identity_shadow_periodic import (
                    metrics_snapshot as _ism,
                )
                metrics["identity_shadow"] = _ism(
                    getattr(_iscm, "config", None) or {},
                    _ISPath(str(getattr(_iscm, "config_path", "")
                                or "config/config.yaml")).parent)
        except Exception:
            pass

        # 四域真活探针（2026-08-27）：读 watchdog 每轮落的 state 文件快照，**绝不在
        # 请求里现场探针**（那会把一次看板刷新变成四发真推理）。此前探针结果只进日志
        # 和主机弹窗，src/web 里一处引用都没有——「探针没在跑」完全不可观测，
        # stale_sec 正是为区分「四域都绿」与「探针停摆」。未跑过 → {"present": false}。
        try:
            _tpcm = getattr(request.app.state, "config_manager", None)
            if _tpcm is not None:
                from pathlib import Path as _TPPath
                from src.ops.true_probe import metrics_snapshot as _tpm
                metrics["true_probe"] = _tpm(
                    _TPPath(str(getattr(_tpcm, "config_path", "")
                                or "config/config.yaml")).parent)
        except Exception:
            pass

        # 记忆去重观测（P5）：灰区对（差一点就并的近义对）累计——「要不要上
        # LLM 仲裁合并」的两周观察读数；store 未接（如纯 web 部署）→ 键缺省。
        try:
            _edsm = getattr(request.app.state, "skill_manager", None)
            _edst = getattr(_edsm, "_episodic_store", None)
            if _edst is not None and hasattr(_edst, "dedup_stats_snapshot"):
                metrics["episodic_dedup"] = _edst.dedup_stats_snapshot()
        except Exception:
            pass

        # 深度人设观测：巩固/画像/内部梗/经历/未收尾话题/回指 累计（真人感"长出来"的证据）
        try:
            from src.companion.deep_persona_stats import get_deep_persona_stats
            _dpm = get_deep_persona_stats().dump()
            # G1：合入 embedder 命中率/延迟（语义召回"值不值"的量化）
            try:
                from src.companion.deep_persona_runtime import embedder_stats
                _dpm["embedder"] = embedder_stats()
            except Exception:
                pass
            # 趋势快照（默认关 trend_log）：机会式 upsert 当天累计 → 供 7 天 sparkline / AB
            try:
                from src.companion.deep_persona_runtime import trend_log_enabled
                if trend_log_enabled():
                    from src.companion.deep_persona_trend import (
                        get_deep_persona_trend, flatten_stats_for_trend)
                    _tr = get_deep_persona_trend("config/deep_persona.db")
                    if _tr is not None:
                        _tr.upsert_today(flatten_stats_for_trend(_dpm))
                        _dpm["trend_7d"] = _tr.read_recent(7)
            except Exception:
                pass
            metrics["deep_persona"] = _dpm
        except Exception:
            pass

        # 实时语音通话观测（发起/接通率/时长/挂断原因/主机健康/显存生命周期）
        try:
            from src.ai.realtime_voice_stats import get_realtime_voice_stats
            metrics["realtime_voice"] = get_realtime_voice_stats().dump()
        except Exception:
            pass

        # Telegram 原生通话观测（接听率/拒接原因分布/时长/并发/拟人动作/危机升级）
        try:
            from src.voicecall.call_stats import get_call_stats
            metrics["tg_call"] = get_call_stats().dump()
        except Exception:
            pass

        # ASR 降级观测（主用/回落/全失败/幻觉丢弃 + 回落率）——主 ASR 掉线导致全链降级=可见
        try:
            from src.ai.asr_stats import get_asr_stats
            metrics["asr"] = get_asr_stats().dump()
        except Exception:
            pass

        # P58：通用 provider 用量（OCR/ASR 等多模态后端）
        try:
            from src.ai.provider_stats import all_provider_stats
            ap = all_provider_stats()
            if ap:
                metrics["providers"] = ap
        except Exception:
            pass

        # 前端「哑按钮」运行时错误观测（dead-click 守卫 beacon 累计；哪页哪函数点崩、多频）
        try:
            from src.web.frontend_error_stats import get_frontend_error_stats
            metrics["frontend_errors"] = get_frontend_error_stats().dump()
        except Exception:
            pass

        # 出站拦截统一计数（P5：业务频控/安全刹车/额度 三层 × 原因 + unlimited_mode 放行数）
        try:
            from src.ops.outbound_policy import blocked_snapshot as _ob_snapshot
            metrics["outbound_blocked"] = _ob_snapshot()
        except Exception:
            pass

        # 坐席手动出图漏斗（尝试/成功/失败码分布/时延/相册秒发占比，2026-08-22 P1）
        try:
            from src.web.image_gen_stats import get_image_gen_stats
            metrics["image_gen"] = get_image_gen_stats().dump()
        except Exception:
            pass

        # 无兜底纪律拦截计数（语音/翻译/识图/转写/聊天失败未发出）
        try:
            from src.ops.delivery_block import snapshot as _deliv_snap
            metrics["delivery_block"] = _deliv_snap()
        except Exception:
            pass

        # 出站语言硬闸（P1-198）：held/rescued/no_target_sent + 最近事件
        try:
            from src.inbox.outbound_lang_stats import get_outbound_lang_stats
            metrics["outbound_lang_gate"] = get_outbound_lang_stats().dump()
        except Exception:
            pass

        # 对方机器人守卫（P0 2026-08-03 SpamBot 空转实锤）：检出/拦截/降档/预算命中
        try:
            from src.inbox.peer_bot_guard import stats_snapshot as _pbg_snapshot
            metrics["peer_bot_guard"] = _pbg_snapshot()
        except Exception:
            pass

        # CSRF 写请求拒绝观测（中间件 403 计数；kind=cookie_no_header 即「宿主缺
        # fetch 补丁/客户端未带凭证」签名——2026-07-31 人设切换事故的形态）
        try:
            from src.web.csrf_stats import get_csrf_reject_stats
            metrics["csrf_rejects"] = get_csrf_reject_stats().dump()
        except Exception:
            pass

        # 出站媒体归档发布（A 线 publish_outbound_media 成败；失败＝该条媒体在坐席台
        # 静默退化成纯文本占位——「自己发的语音看不到」的根因计数，2026-08-02）
        try:
            from src.integrations.outbound_mirror_stats import get_outbound_mirror_stats
            metrics["outbound_mirror"] = get_outbound_mirror_stats().dump()
        except Exception:
            pass

        # 功能锁触达（E6：档位闸门 API 403 / 页面 302 按族计数——「哪个锁被撞
        # 得最多」＝下一个该降档/该重点卖的功能的定价信号）
        try:
            from src.web.feature_lock_stats import get_feature_lock_stats
            metrics["feature_lock"] = get_feature_lock_stats().dump()
        except Exception:
            pass

        # 人设文档导入/考题观测（解析→抽取→传记入库→一致性考题 漏斗计数与均值）
        try:
            from src.utils.persona_import_stats import get_persona_import_stats
            metrics["persona_import"] = get_persona_import_stats().dump()
        except Exception:
            pass

        # 前端 UI 交互埋点（空态引导按钮点击率/群区模式切换等；观测「引导有效性」）
        try:
            from src.web.ui_event_stats import get_ui_event_stats
            metrics["ui_events"] = get_ui_event_stats().dump()
        except Exception:
            pass

        # 目录同步（好友名单→通讯录）观测（分账号轮数/条数/失败段/上次同步时间）
        try:
            from src.integrations.directory_sync_stats import get_directory_sync_stats
            metrics["directory_sync"] = get_directory_sync_stats().dump()
        except Exception:
            pass

        # 出站语音语言路由观测（哪些语种在被路由/拒发；拒发涨=该语种缺音色映射）
        try:
            from src.ai.lang_route_stats import get_lang_route_stats
            metrics["lang_voice_route"] = get_lang_route_stats().dump()
        except Exception:
            pass

        # 双实例重启冷却（机器级 JSON；连环重启是坐席「加载超时」主因）
        try:
            from src.utils.instance_restart_status import collect_restart_status
            metrics["instance_restart"] = collect_restart_status()
        except Exception:
            pass

        # 会话 peer 身份「惰性解析/自愈补名」观测（数字号 healed 了多少 / 缓存命中 / 取不到）
        try:
            from src.web.peer_identity_stats import get_peer_identity_stats
            metrics["peer_identity"] = get_peer_identity_stats().dump()
        except Exception:
            pass

        # 出站「路由去向」观测（编排器接管 vs 回落适配器；回落率暴露「编排器漏接」）
        try:
            from src.inbox.send_route_stats import get_send_route_stats
            metrics["send_routes"] = get_send_route_stats().dump()
        except Exception:
            pass

        # 每人设「相册/媒体」观测（图/视频备货、命中总数、命中 Top-N；备而不发=需配触发词）
        try:
            from src.companion.persona_media_store import get_persona_media_store
            _pms = get_persona_media_store()
            # 实施90：挑图拦截计数（进程口径）随相册指标一并出（有流量才带键）
            try:
                from src.companion.album_gate_stats import snapshot as _ags_snap
                _gates = _ags_snap()
            except Exception:
                _gates = None
            if _pms is not None:
                metrics["persona_media"] = _pms.analytics()
                if _gates and _gates.get("active"):
                    metrics["persona_media"]["gates"] = _gates
                try:
                    from src.companion.album_semantic_recall import (
                        snapshot as _asr_snap,
                    )
                    _shadow = _asr_snap()
                    if _shadow.get("active"):
                        metrics["persona_media"]["semantic_shadow"] = _shadow
                except Exception:
                    pass
        except Exception:
            pass

        # 表情包（贴纸）观测（2026-08-17）：发送/收藏进程口径 + sent_as 分桶
        # （image 占比高＝目标平台原生能力缺口：WA 边车未升级 / LINE 自建包为主）。
        # 备货水位走 store counts（持久口径）；零流量时 sends=0，ops 卡自行隐藏。
        try:
            from src.inbox.sticker_stats import get_sticker_stats
            _stk = get_sticker_stats().dump()
            from src.inbox.sticker_store import get_sticker_store
            _sst = get_sticker_store()
            if _sst is not None:
                _stk.update(_sst.counts())
            metrics["stickers"] = _stk
        except Exception:
            pass

        # Telegram 群成员提取观测（成员/群/任务；active=false 时 ops 卡整卡隐藏）
        try:
            from src.companion.group_members_store import get_group_members_store
            _gms = get_group_members_store()
            if _gms is not None:
                metrics["group_members"] = _gms.stats()
        except Exception:
            pass

        # 回复时延 SLO（P1-8 2026-08-09）：首答 p50/p95 + 零回复率，inbox 持久库
        # 口径（重启不清零），进程级 300s TTL 缓存防 ops 轮询逐次全扫消息表。
        try:
            _rl_store = getattr(request.app.state, "inbox_store", None)
            if _rl_store is not None:
                from src.ops.reply_latency import reply_latency_snapshot
                _rl = reply_latency_snapshot(_rl_store)
                if _rl:
                    metrics["reply_latency"] = _rl
        except Exception:
            pass

        # 入口可用性 SLO（P1-7 2026-08-12 可靠性复盘）：服务端＝边缘看门狗 7 天
        # tick 可用率+断连段；坐席端＝conn_* 断连回执（P0-2 恢复时刻补发）。两视角
        # 差值=客户端侧损耗。300s TTL 纯读软失败，看门狗日志缺失时 server 为空骨架。
        try:
            from src.ops.entrance_slo import entrance_slo_snapshot
            _slo = entrance_slo_snapshot()
            if _slo:
                metrics["entrance_slo"] = _slo
        except Exception:
            pass

        # 案例中心观测（2026-08-03：自启动立案/结案/升级/告警计数 + 平均结案时长；
        # 当前未结案的 live 口径在 /api/cases/active，两者互补）
        try:
            from src.utils.case_stats import get_case_stats
            metrics["cases"] = get_case_stats().dump()
            try:
                from src.utils.case_trend_store import get_case_trend_store
                _cts = get_case_trend_store()
                if _cts is not None:
                    metrics["cases"]["trend"] = _cts.recent(14)
            except Exception:
                pass
        except Exception:
            pass

        # 发图能力关闭时的出站消毒观测（P1，2026-07-31）：strip_rate 高=
        # 提示层约束不够、靠守卫兜底；运营据此决定要不要加人设级禁令措辞。
        try:
            from src.companion.photo_capability import dump_sanitize_stats
            metrics["photo_capability"] = dump_sanitize_stats()
        except Exception:
            pass

        # 会话级人设覆写观测（出站解析 tier 分布 / 覆写命中 / legacy 被压制 / 治理动作）
        try:
            from src.ai.persona_override_stats import get_persona_override_stats
            metrics["persona_override"] = get_persona_override_stats().dump()
            # 活水位：现在还剩多少条 legacy 债（清零后回升=有路径在重新制造）。
            # 嵌套 try：水位失败不连累计数器段。
            try:
                from src.web.routes.persona_routes import legacy_debt_snapshot
                metrics["persona_override"]["legacy_debt"] = (
                    legacy_debt_snapshot(request.app))
            except Exception:
                pass
        except Exception:
            pass

        # 平台会话健康（P0-2：外部 worker push 的会话状态登记；unhealthy=需人工重登）
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            metrics["platform_sessions"] = get_platform_session_health().dump()
        except Exception:
            pass

        # Messenger 双道就绪度（P2 2026-08-13）：注册表×sidecar×配置闸门的
        # 「人工收发 / 全自动」第一阻塞原因判定——与 tools/diagnose_messenger.py
        # 共用 evaluate_messenger_readiness 同一纯函数（60s TTL 共享探针，
        # watchdog 的 not_restored 对账同源）。
        try:
            from src.integrations.messenger_readiness_collect import (
                collect_messenger_readiness,
            )
            _mr_cm = getattr(request.app.state, "config_manager", None)
            metrics["messenger_readiness"] = collect_messenger_readiness(
                getattr(_mr_cm, "config", None) or {})
        except Exception:
            pass

        # 扫码登录漏斗（started→qr_shown→pin_issued→authorized ↘ failed[reason]）：
        # rows[].stalled=True（发起≥3 次零授权）即某平台某登录方式事实不可用——LINE 事故形态
        try:
            from src.integrations.login_funnel_stats import get_login_funnel_stats
            metrics["login_funnel"] = get_login_funnel_stats().dump()
        except Exception:
            pass

        # TG 断网冷却（web 侧观察态：get_chat 超时→账号级 60s 冷却）附进同一块——
        # 与 worker push 的登记表语义不同（这是「正在断网重连」的瞬时信号），
        # 但用户视角同属「平台会话健康」，ops 卡据此给 telegram 行标「冷却中」。
        try:
            from src.web.routes.unified_inbox_account_routes import tg_cooldown_snapshot
            _cd = tg_cooldown_snapshot()
            if isinstance(metrics.get("platform_sessions"), dict):
                metrics["platform_sessions"]["tg_cooldown"] = _cd
            elif _cd:
                metrics["platform_sessions"] = {"tg_cooldown": _cd}
        except Exception:
            pass

        # 账号官方资料修改推送观测（accounts.profile_push 漏斗：attempts/success/
        # partial/failed/cooldown_blocked/offline_blocked/persona_fill + by_platform）
        try:
            from src.integrations.account_profile_push import (
                get_profile_push_stats,
            )
            metrics["profile_push"] = get_profile_push_stats()
        except Exception:
            pass

        fmt = str(format or "json").lower()

        if fmt == "prometheus":
            buf = io.StringIO()

            def _gauge(name: str, value, help_text: str = "", labels: str = "") -> None:
                if help_text:
                    buf.write(f"# HELP {name} {help_text}\n")
                buf.write(f"# TYPE {name} gauge\n")
                lbl = f"{{{labels}}}" if labels else ""
                buf.write(f"{name}{lbl} {value}\n")

            _gauge("ws_autosend_running",
                   1 if metrics["autosend"].get("running") else 0,
                   "AutosendWorker is running")
            _gauge("ws_autosend_total_sent",
                   metrics["autosend"].get("total_sent", 0),
                   "Total L2 drafts auto-sent")
            _gauge("ws_autosend_total_errors",
                   metrics["autosend"].get("total_errors", 0),
                   "Total autosend errors")
            _gauge("ws_autosend_circuit_open",
                   1 if metrics["autosend"].get("circuit_open") else 0,
                   "AutosendWorker circuit breaker open")
            # P0/P2 多开治理：人工通过投递 + 竞态拦截 + 发送幂等去重
            _gauge("ws_autosend_human_delivered_total",
                   metrics["autosend"].get("total_human_delivered", 0),
                   "Human-approved inbox drafts delivered via worker send chain")
            _gauge("ws_autosend_human_deliver_errors_total",
                   metrics["autosend"].get("total_human_deliver_errors", 0),
                   "Human-approved inbox draft delivery failures")
            _gauge("ws_autosend_raced_skips_total",
                   metrics["autosend"].get("total_skipped_raced", 0),
                   "Draft resolves skipped because another window/agent won the race")
            _sd = metrics.get("send_dedup") or {}
            _gauge("ws_send_dedup_duplicates_total",
                   _sd.get("total_duplicates", 0),
                   "Duplicate manual sends blocked by client_msg_id dedup")
            _gauge("ws_send_dedup_reserved_total",
                   _sd.get("total_reserved", 0),
                   "Manual sends carrying a client_msg_id (dedup-protected)")

            _gauge("ws_sla_watcher_running",
                   1 if metrics["sla_watcher"].get("running") else 0,
                   "SLAWatcher is running")
            _gauge("ws_sla_breach_events_total",
                   metrics["sla_watcher"].get("total_breach_events", 0),
                   "Total SLA breach events published")
            _gauge("ws_sla_reassigned_total",
                   metrics["sla_watcher"].get("total_reassigned", 0),
                   "Total drafts auto-reassigned")
            _gauge("ws_sla_expired_total",
                   metrics["sla_watcher"].get("total_expired", 0),
                   "Total stale pending drafts auto-expired")
            _gauge("ws_sla_quiesced_total",
                   metrics["sla_watcher"].get("quiesced_count", 0),
                   "Total drafts quiesced (stale, alert suppressed)")

            ac = metrics.get("auto_claim", {})
            _gauge("ws_auto_claim_running",
                   1 if ac.get("running") else 0,
                   "AutoClaimWorker is running")
            _gauge("ws_auto_claim_total",
                   ac.get("total_claimed", 0),
                   "Total conversations auto-claimed")
            _gauge("ws_auto_claim_lang_matched_total",
                   ac.get("total_lang_matched", 0),
                   "Auto-claims where agent language matched conversation")

            _gauge("ws_webhook_total_sent",
                   metrics["webhook"].get("total_sent", 0),
                   "Total webhook notifications sent")
            _gauge("ws_webhook_total_errors",
                   metrics["webhook"].get("total_errors", 0),
                   "Total webhook send errors")

            drafts = metrics.get("drafts", {})
            _gauge("ws_drafts_pending_total",
                   drafts.get("pending_total", 0),
                   "Total pending drafts")
            for lv, cnt in (drafts.get("by_level") or {}).items():
                _gauge("ws_drafts_pending_by_level",
                       cnt,
                       labels=f'level="{lv}"')

            eb = metrics.get("event_bus", {})
            _gauge("ws_sse_subscribers",
                   eb.get("subscriber_count", 0),
                   "Active SSE subscriber count")

            # P1-4：出向翻译漏斗（覆盖率/auto 失败/降级/按语言）以 counter 形式输出
            try:
                from src.ai.outbound_translation_stats import get_outbound_translation_stats
                buf.write(get_outbound_translation_stats().dump_prom())
            except Exception:
                pass

            # 入站自动翻译（同步/后台三态 + deferred + 冷却拦截）
            try:
                from src.ai.inbound_translation_stats import get_inbound_translation_stats
                buf.write(get_inbound_translation_stats().dump_prom())
            except Exception:
                pass

            # 实时语音通话观测（发起/接通/时长/主机健康/显存生命周期）
            try:
                from src.ai.realtime_voice_stats import get_realtime_voice_stats
                buf.write(get_realtime_voice_stats().dump_prom())
            except Exception:
                pass

            # Telegram 原生通话观测（接听率/拒接原因/时长/拟人/危机升级）
            try:
                from src.voicecall.call_stats import get_call_stats
                buf.write(get_call_stats().dump_prom())
            except Exception:
                pass

            # ASR 降级观测（主用/回落/全失败/幻觉丢弃 + 按回落 provider）
            try:
                from src.ai.asr_stats import get_asr_stats
                buf.write(get_asr_stats().dump_prom())
            except Exception:
                pass

            # AvatarHub 语音（合成成败/通道分布/预渲染命中/队列峰值/STT）
            try:
                from src.ai.avatar_voice_stats import get_avatar_voice_stats
                buf.write(get_avatar_voice_stats().dump_prom())
            except Exception:
                pass

            # 语音出站断档台账（三链滚动窗成败；attempts>0 且 ok=0 ＝断档告警面）
            try:
                from src.ai.voice_outage import get_voice_outage
                if get_voice_outage().outage_snapshot().get("attempts_24h"):
                    buf.write(get_voice_outage().dump_prom())
            except Exception:
                pass

            # LINE 媒体收发（入站下载/出站两步链；仅有流量时输出，同 credpool 口径）
            try:
                from src.integrations.line_media_stats import get_line_media_stats
                if get_line_media_stats().dump().get("active"):
                    buf.write(get_line_media_stats().dump_prom())
            except Exception:
                pass

            # 出站媒体归档发布（A 线镜像可回放的前提；失败=静默退化文本占位）
            try:
                from src.integrations.outbound_mirror_stats import get_outbound_mirror_stats
                if get_outbound_mirror_stats().dump().get("total"):
                    buf.write(get_outbound_mirror_stats().dump_prom())
            except Exception:
                pass

            # 中央凭据池（分配来源/生效档位/回落原因/池承载比例）
            try:
                from src.integrations.credpool_stats import get_credpool_stats
                if get_credpool_stats().dump().get("active"):
                    buf.write(get_credpool_stats().dump_prom())
            except Exception:
                pass

            # 功能锁触达（档位闸门拦截按族计数；零流量时只出 total=0 行，极轻）
            try:
                from src.web.feature_lock_stats import get_feature_lock_stats
                buf.write(get_feature_lock_stats().dump_prom())
            except Exception:
                pass

            # 命理技能（话题/采集/灵签/详批/K线 漏斗计数）
            try:
                from src.companion.bazi_stats import get_bazi_stats
                buf.write(get_bazi_stats().dump_prom())
            except Exception:
                pass

            # 唱歌能力（requests/sent/no_stock/capped + 按模板分布）
            try:
                from src.companion.song_stock import get_song_stats
                buf.write(get_song_stats().dump_prom())
            except Exception:
                pass

            # 营销目标（建目标/每日拍/注入/主动桥/终态 漏斗计数）
            try:
                from src.companion.goals.stats import get_goal_stats
                buf.write(get_goal_stats().dump_prom())
            except Exception:
                pass

            # 前端「哑按钮」运行时错误（by page / by fn / by type）
            try:
                from src.web.frontend_error_stats import get_frontend_error_stats
                buf.write(get_frontend_error_stats().dump_prom())
            except Exception:
                pass

            # 坐席手动出图（尝试/成功/失败码/时延/相册秒发）
            try:
                from src.web.image_gen_stats import get_image_gen_stats
                buf.write(get_image_gen_stats().dump_prom())
            except Exception:
                pass

            # Token 计费账本（钱包余额/消耗分布/影子计数/公平使用；未启用=零输出）
            try:
                from src.licensing.token_ledger import dump_prom as _tok_dump_prom
                buf.write(_tok_dump_prom())
            except Exception:
                pass

            # 出站拦截统一计数（outbound_blocked_total{layer,reason} + unlimited_mode 开关/放行数）
            try:
                from src.ops.outbound_policy import dump_prom as _ob_dump_prom
                buf.write(_ob_dump_prom())
            except Exception:
                pass

            # 出站语言硬闸（P1-198：held/rescued/no_target_sent 分桶）
            try:
                from src.inbox.outbound_lang_stats import get_outbound_lang_stats
                buf.write(get_outbound_lang_stats().dump_prom())
            except Exception:
                pass

            # CSRF 写请求拒绝（total / by kind / by path，中间件静默 403 可观测化）
            try:
                from src.web.csrf_stats import get_csrf_reject_stats
                buf.write(get_csrf_reject_stats().dump_prom())
            except Exception:
                pass

            # 人设文档导入/考题（漏斗事件计数 + 抽取耗时/完整度/考题分均值）
            try:
                from src.utils.persona_import_stats import get_persona_import_stats
                buf.write(get_persona_import_stats().dump_prom())
            except Exception:
                pass

            # 会话级人设覆写（tier 分布 / legacy 被压制 / 治理动作）
            try:
                from src.ai.persona_override_stats import get_persona_override_stats
                buf.write(get_persona_override_stats().dump_prom())
                try:
                    from src.web.routes.persona_routes import legacy_debt_snapshot
                    _debt = legacy_debt_snapshot(request.app)
                    buf.write(
                        "# HELP persona_override_legacy_debt Remaining legacy "
                        "peer-global bindings (excl. RPA-managed)\n"
                        "# TYPE persona_override_legacy_debt gauge\n"
                        f"persona_override_legacy_debt {_debt}\n"
                    )
                except Exception:
                    pass
            except Exception:
                pass

            # 前端 UI 交互埋点（by action / by page；空态引导点击率等）
            try:
                from src.web.ui_event_stats import get_ui_event_stats
                buf.write(get_ui_event_stats().dump_prom())
            except Exception:
                pass

            # 目录同步（分账号 runs/failures + 最近一轮条数 + 上次同步时间戳）：
            # last_ts 陈旧 = 该号名单同步事实上死了，此前只有日志能看出来
            try:
                from src.integrations.directory_sync_stats import get_directory_sync_stats
                buf.write(get_directory_sync_stats().dump_prom())
            except Exception:
                pass

            # 出站语音语言路由（routed by lang / 拒发守卫 / 克隆兜底对齐）
            try:
                from src.ai.lang_route_stats import get_lang_route_stats
                buf.write(get_lang_route_stats().dump_prom())
            except Exception:
                pass

            # 双实例重启冷却（连环重启 / 坐席加载超时根因可告警）
            try:
                from src.utils.instance_restart_status import dump_prom as _inst_restart_prom
                buf.write(_inst_restart_prom())
            except Exception:
                pass

            # P58 通用 provider 用量（vision 入站识图 / ocr / asr 等；JSON 侧已有，
            # 此前 workspace prom 缺失——补齐后 vision_attempts_total 等可被抓取）
            try:
                from src.ai.provider_stats import all_provider_prom
                buf.write(all_provider_prom())
            except Exception:
                pass
            try:
                from src.ai.inbound_video_stats import get_inbound_video_stats
                buf.write(get_inbound_video_stats().dump_prom())
            except Exception:
                pass

            # 会话 peer 身份自愈补名（by source × outcome）
            try:
                from src.web.peer_identity_stats import get_peer_identity_stats
                buf.write(get_peer_identity_stats().dump_prom())
            except Exception:
                pass

            # 出站路由去向（orchestrator vs adapter，按平台；回落率）
            try:
                from src.inbox.send_route_stats import get_send_route_stats
                buf.write(get_send_route_stats().dump_prom())
            except Exception:
                pass

            # 平台会话健康（events by status + per-session unhealthy gauge）
            try:
                from src.integrations.platform_session_health import (
                    get_platform_session_health,
                )
                buf.write(get_platform_session_health().dump_prom())
            except Exception:
                pass

            # 扫码登录漏斗（started→qr_shown→pin_issued→authorized ↘ failed[reason]）：
            # 读出端此前漏接线（埋点在累积却对外不可见），2026-07-25 功能测试补齐——
            # 「pin_issued 有量 / authorized 近零」即 LINE 事故形态，Prometheus 侧现可抓取告警
            try:
                from src.integrations.login_funnel_stats import get_login_funnel_stats
                buf.write(get_login_funnel_stats().dump_prom())
            except Exception:
                pass

            # B 线 autosend 语音/发图（sent/fallback + provider/截断/失败原因）
            av = metrics.get("autosend_voice") or {}
            if av:
                _gauge("ws_autosend_voice_sent_total", av.get("sent", 0),
                       "Autosend voice messages sent")
                _gauge("ws_autosend_voice_fallback_total", av.get("fallback", 0),
                       "Autosend voice synth/deliver fallbacks")
                _gauge("ws_autosend_voice_truncation_suspects_total",
                       av.get("truncation_suspects", 0),
                       "Autosend voice truncation suspects")
            ai = metrics.get("autosend_image") or {}
            if ai:
                _gauge("ws_autosend_image_sent_total", ai.get("sent", 0),
                       "Autosend images sent")
                _gauge("ws_autosend_image_fallback_total", ai.get("fallback", 0),
                       "Autosend image stage/deliver fallbacks")

            # 每人设相册备货与命中（items/enabled/hits + 按类型）
            pm = metrics.get("persona_media") or {}
            if pm:
                _gauge("ws_persona_media_items", pm.get("total", 0),
                       "Persona album media items (total)")
                _gauge("ws_persona_media_enabled", pm.get("enabled", 0),
                       "Persona album media items enabled")
                _gauge("ws_persona_media_hits_total", pm.get("total_hits", 0),
                       "Persona album media send hits (cumulative)")
                _gauge("ws_persona_media_by_type", pm.get("photo", 0),
                       "Persona album media items by type", labels='type="photo"')
                _gauge("ws_persona_media_by_type", pm.get("video", 0),
                       labels='type="video"')
                # 实施90：挑图门禁拦截（进程口径；零流量不出行）
                _g = pm.get("gates") or {}
                if _g:
                    _gauge("ws_persona_media_gate_picks_total",
                           _g.get("picks", 0),
                           "Album pick attempts that returned a media")
                    _gauge("ws_persona_media_gate_refused_total",
                           _g.get("refused", 0),
                           "Album pick attempts refused by gates")
                    for _reason, _n in (_g.get("refused_by") or {}).items():
                        if _n:
                            _gauge("ws_persona_media_gate_refused_by",
                                   _n, labels=f'reason="{_reason}"')

            # 贴纸：发送/收藏（进程口径）+ 备货水位（包/张，持久口径）
            stk = metrics.get("stickers") or {}
            if stk:
                _gauge("ws_sticker_sends_total", stk.get("sends", 0),
                       "Sticker sends (process counter)")
                _gauge("ws_sticker_collects_total", stk.get("collects", 0),
                       "Inbound stickers collected into packs")
                _gauge("ws_sticker_packs", stk.get("packs", 0),
                       "Sticker packs enabled")
                _gauge("ws_sticker_items", stk.get("stickers", 0),
                       "Stickers enabled (all packs)")

            # 群成员提取库水位（成员/群/运行中任务）
            gm = metrics.get("group_members") or {}
            if gm:
                _gauge("ws_group_members_total", gm.get("members_total", 0),
                       "Telegram group members extracted (total)")
                _gauge("ws_group_members_groups", gm.get("groups", 0),
                       "Distinct groups with extracted members")
                _gauge("ws_group_members_jobs_running", gm.get("jobs_running", 0),
                       "Group member extraction jobs running")

            # 回复时延 SLO（24h 窗：p50/p95/零回复——市场可承诺数字的机器可读面）
            rl = (metrics.get("reply_latency") or {}).get("d1") or {}
            if rl.get("episodes"):
                _gauge("ws_reply_latency_p50_seconds", rl.get("p50_s", 0),
                       "First-reply latency p50 over last 24h (seconds)")
                _gauge("ws_reply_latency_p95_seconds", rl.get("p95_s", 0),
                       "First-reply latency p95 over last 24h (seconds)")
                _gauge("ws_reply_unanswered_24h", rl.get("unanswered", 0),
                       "Inbound bursts unanswered past grace over last 24h")

            # 发送护栏拦截（P2 2026-08-13；零拦截不出行，与 cases 同口径）
            _sgb = metrics.get("send_gate_blocks") or {}
            if _sgb.get("total"):
                _gauge("ws_send_gate_blocked_total", _sgb.get("total", 0),
                       "Send attempts blocked by the send guard (process lifetime)")

            # 案例中心（立案/结案/升级/告警；来源分布走 dump_prom 的 label 行）
            cs = metrics.get("cases") or {}
            if cs and (cs.get("opened") or cs.get("closed")):
                _gauge("ws_cases_opened_total", cs.get("opened", 0),
                       "Cases opened since boot")
                _gauge("ws_cases_closed_total", cs.get("closed", 0),
                       "Cases closed since boot")
                _gauge("ws_cases_upgraded_total", cs.get("upgraded", 0),
                       "Case severity upgrades since boot")
                _gauge("ws_cases_alerts_total", cs.get("alerts_emitted", 0),
                       "Case alerts emitted since boot")
                for _src, _n in sorted((cs.get("opened_by_source") or {}).items()):
                    _safe = "".join(
                        ch if (ch.isalnum() or ch == "_") else "_" for ch in str(_src))
                    _gauge("ws_cases_opened_by_source", _n,
                           "Cases opened by source", labels=f'source="{_safe}"')

            # 账号官方资料修改推送（accounts.profile_push 漏斗）
            ppst = metrics.get("profile_push") or {}
            if ppst:
                _gauge("profile_push_attempts_total", ppst.get("attempts", 0),
                       "Account profile push attempts (process lifetime)")
                _gauge("profile_push_success_total", ppst.get("success", 0),
                       "Account profile pushes with at least one field applied")
                _gauge("profile_push_cooldown_blocked_total",
                       ppst.get("cooldown_blocked", 0),
                       "Account profile pushes blocked by the per-account cooldown")

            return PlainTextResponse(buf.getvalue(), media_type="text/plain; version=0.0.4")

        return {"ok": True, **metrics}


def register_telemetry_route(app, *, api_auth):
    """前端遥测上报（任意登录用户可写，不限主管）。

    POST /api/telemetry/frontend-error  body: {page, fn, type[, endpoint]}
    dead-click 守卫（unified_inbox + _rpa_shared_scripts）捕获 ReferenceError 后 beacon 到此，
    经 FrontendErrorStats 累计，读出走 /api/workspace/metrics.frontend_errors（主管专属）。
    ``endpoint``（可选）＝apiFetch 网络层失败附带的请求 path（消毒：丢查询串、
    数字段掩码 <n>）——修「哪个接口在坏」无从归因的观测盲区（2026-07-29）。
    只收计数用的消毒字段，绝不落原文/堆栈；任何异常都吞掉返回 ok，绝不影响前端。

    POST /api/telemetry/ui-event  body: {page, action}
    UI 交互埋点（空态引导按钮点击/群区显示模式切换等），经 UiEventStats 累计，
    读出走 /api/workspace/metrics.ui_events。同款契约：只收两个消毒字段，吞异常恒返 ok。
    """
    from fastapi import Depends

    @app.post("/api/telemetry/frontend-error")
    async def api_frontend_error(request: Request, _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            try:
                from src.web.frontend_error_stats import get_frontend_error_stats
                get_frontend_error_stats().record(
                    page=str(body.get("page") or ""),
                    fn=str(body.get("fn") or ""),
                    etype=str(body.get("type") or ""),
                    endpoint=str(body.get("endpoint") or ""),
                )
            except Exception:
                pass
            try:
                # P9：按日落库（进程计数重启即清零，本机重启频繁——趋势只能靠 DB 口径；
                # 未开 ops.frontend_error_trend → record 恒 no-op 零 IO）
                from src.web.frontend_error_trend import record_frontend_error_trend
                record_frontend_error_trend(str(body.get("type") or ""))
            except Exception:
                pass
        return {"ok": True}

    @app.post("/api/telemetry/ui-event")
    async def api_ui_event_beacon(request: Request, _=Depends(api_auth)):
        """前端 UI 交互埋点 beacon（空态引导点击率/群区模式切换等「引导有效性」观测）。

        与 frontend-error 同款契约：body 解析失败按 {}、只取 page+action 两个字段
        （消毒在 UiEventStats 内做）、任何异常都吞掉返回 ok，绝不影响前端。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            try:
                from src.web.ui_event_stats import get_ui_event_stats
                get_ui_event_stats().record(
                    page=str(body.get("page") or ""),
                    action=str(body.get("action") or ""),
                )
            except Exception:
                pass
            try:
                # 按日落库（进程计数重启即清零，2026-08-01 施工日实测一天 4 次重启把
                # AI 回复漏斗首批数据清洗掉；未开 ops.ui_event_trend → record 恒 no-op）
                from src.web.ui_event_trend import record_ui_event_trend
                record_ui_event_trend(str(body.get("action") or ""))
            except Exception:
                pass
        return {"ok": True}

    @app.get("/api/workspace/entrances")
    async def api_workspace_entrances(request: Request, _=Depends(api_auth)):
        """入口清单（P1-5 2026-08-12 可靠性复盘）：断连横幅「切换备用入口」的数据源。

        读 ``web_admin.entrance_alternates``（overlay 配置的绝对 base URL 列表，如
        LAN 直连地址 + 公网域名）。前端页面加载时取一次并落 localStorage——断连
        期间本接口本就不可达，缓存才是断连时刻的真数据源。未配置返回空表＝
        横幅不出切换链接（租户实例零污染：他们的 overlay 没有这个键）。
        只回显 http(s) 绝对地址，防配置手误把奇怪字符串塞进 <a href>。
        """
        try:
            cm = getattr(request.app.state, "config_manager", None)
            raw_cfg = (getattr(cm, "config", None) or {}) if cm else {}
            alts = (raw_cfg.get("web_admin") or {}).get("entrance_alternates") or []
            out = []
            for u in alts:
                s = str(u or "").strip().rstrip("/")
                if s.startswith(("http://", "https://")) and len(s) < 200:
                    out.append(s)
            return {"entrances": out[:4]}
        except Exception:
            return {"entrances": []}


def register_glossary_route(app, *, api_auth):
    """P59：术语库管理控制台 API（主管专属）。

    GET  /api/workspace/glossary           → 合并视图（terms/protect + 来源标记 + version）
    POST /api/workspace/glossary           → 增删改覆盖层 {op, term?, translation?, word?}
                                             op ∈ upsert_term|remove_term|add_protect|remove_protect
    覆盖层落 config/glossary_overrides.yaml，重建术语库并热更新到 translation_service。
    """
    from fastapi import Depends

    def _build_view(request: Request):
        from src.ai.translation_glossary import build_glossary
        store = getattr(request.app.state, "glossary_store", None)
        config = getattr(request.app.state, "glossary_config", None) or {}
        domain_files = getattr(request.app.state, "glossary_domain_files", None) or []
        overrides = store.load() if store is not None else {"terms": {}, "protect": []}
        merged = build_glossary(config, domain_files=domain_files, overrides=overrides)
        ov_terms = set((overrides.get("terms") or {}).keys())
        ov_protect = set(overrides.get("protect") or [])
        try:
            from src.ai.glossary_hits import get_glossary_hits
            hits = get_glossary_hits()
        except Exception:
            hits = None
        terms = [
            {"term": k, "translation": v,
             "source": "console" if k in ov_terms else "base",
             "editable": k in ov_terms,
             "hits": hits.term_hits(k) if hits else 0}
            for k, v in sorted(merged.terms.items())
        ]
        protect = [
            {"word": w, "source": "console" if w in ov_protect else "base",
             "editable": w in ov_protect,
             "hits": hits.protect_hits(w) if hits else 0}
            for w in merged.protect
        ]
        hd = hits.dump() if hits else {"total_term_hits": 0, "total_protect_hits": 0}
        return {
            "ok": True,
            "version": merged.version,
            "enabled": not merged.empty() or True,
            "terms": terms,
            "protect": protect,
            "counts": {"terms": len(terms), "protect": len(protect),
                       "console_terms": len(ov_terms), "console_protect": len(ov_protect),
                       "term_hits": hd.get("total_term_hits", 0),
                       "protect_hits": hd.get("total_protect_hits", 0)},
            "has_store": store is not None,
        }

    def _export_csv(request: Request) -> str:
        import csv
        import io
        view = _build_view(request)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["type", "key", "value"])
        for t in view["terms"]:
            w.writerow(["term", t["term"], t["translation"]])
        for p in view["protect"]:
            w.writerow(["protect", p["word"], ""])
        return buf.getvalue()

    def _import_csv(store, text: str) -> dict:
        import csv
        import io
        added_terms = 0
        added_protect = 0
        reader = csv.reader(io.StringIO(text))
        for i, row in enumerate(reader):
            if not row:
                continue
            kind = (row[0] or "").strip().lower()
            if i == 0 and kind == "type":
                continue  # 跳过表头
            key = (row[1] if len(row) > 1 else "").strip()
            val = (row[2] if len(row) > 2 else "").strip()
            try:
                if kind == "term" and key and val:
                    store.upsert_term(key, val)
                    added_terms += 1
                elif kind == "protect" and key:
                    store.add_protect(key)
                    added_protect += 1
            except ValueError:
                continue
        return {"added_terms": added_terms, "added_protect": added_protect}

    def _rebuild_and_apply(request: Request):
        from src.ai.translation_glossary import build_glossary
        store = getattr(request.app.state, "glossary_store", None)
        config = getattr(request.app.state, "glossary_config", None) or {}
        domain_files = getattr(request.app.state, "glossary_domain_files", None) or []
        overrides = store.load() if store is not None else {"terms": {}, "protect": []}
        gl = build_glossary(config, domain_files=domain_files, overrides=overrides)
        svc = getattr(request.app.state, "translation_service", None)
        if svc is not None and hasattr(svc, "update_glossary"):
            svc.update_glossary(gl.terms, gl.protect, gl.version)
        return gl

    @app.get("/api/workspace/glossary")
    async def api_workspace_glossary_get(request: Request, format: str = "json", _=Depends(api_auth)):
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        if str(format or "").lower() == "csv":
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                _export_csv(request),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=glossary.csv"},
            )
        return _build_view(request)

    @app.post("/api/workspace/glossary")
    async def api_workspace_glossary_edit(request: Request, _=Depends(api_auth)):
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = getattr(request.app.state, "glossary_store", None)
        if store is None:
            return {"ok": False, "message": "术语库未初始化（翻译服务未启用）"}
        body = await request.json()
        op = str(body.get("op") or "").strip()
        try:
            if op == "upsert_term":
                store.upsert_term(body.get("term"), body.get("translation"))
            elif op == "remove_term":
                store.remove_term(body.get("term"))
            elif op == "add_protect":
                store.add_protect(body.get("word"))
            elif op == "remove_protect":
                store.remove_protect(body.get("word"))
            elif op == "import_csv":
                imp = _import_csv(store, str(body.get("csv") or ""))
            else:
                return {"ok": False, "message": f"未知操作: {op}"}
        except ValueError as ex:
            return {"ok": False, "message": str(ex)}
        gl = _rebuild_and_apply(request)
        view = _build_view(request)
        view["applied_version"] = gl.version
        if op == "import_csv":
            view["imported"] = imp
        return view


def register_trend_route(app, *, api_auth):
    """O1：注册 GET /api/workspace/trend（CSAT/审批率趋势图数据，主管专属）。"""
    from fastapi import Depends
    import time as _time

    @app.get("/api/workspace/trend")
    async def api_workspace_trend(
        request: Request,
        days: int = 7,
        bucket: str = "day",
        _=Depends(api_auth),
    ):
        """O1：返回 CSAT + L3/L4 占比的时间序列趋势数据（主管专属）。

        days:   7（默认，近一周）| 30（近一月）| 90（近季度）
        bucket: day（默认，每天一个数据点）| week（每周）
        返回：{csat_trend: [...], level_trend: [...], delta: {...}}
        delta 包含本周期 vs 上期 CSAT 均值变化量。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        days = max(1, min(90, int(days)))
        bucket_sec = 604800 if bucket == "week" else 86400
        now = _time.time()
        since_ts = now - days * 86400
        prev_since_ts = now - days * 2 * 86400  # 对比上一周期

        csat_trend = inbox.get_csat_trend(since_ts=since_ts, bucket_sec=bucket_sec)
        level_trend = inbox.get_draft_level_trend(since_ts=since_ts, bucket_sec=bucket_sec)

        # delta：当期 vs 上期 CSAT 均值差
        curr_csat_rows = inbox.get_csat_trend(since_ts=since_ts)
        prev_csat_rows = inbox.get_csat_trend(since_ts=prev_since_ts, bucket_sec=bucket_sec)
        prev_csat_rows_filtered = [r for r in prev_csat_rows if r["bucket_ts"] < since_ts]

        def _avg(rows):
            vals = [r["avg_csat"] for r in rows if r["avg_csat"] is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        curr_avg = _avg(curr_csat_rows)
        prev_avg = _avg(prev_csat_rows_filtered)
        delta_csat = round(curr_avg - prev_avg, 2) if curr_avg is not None and prev_avg is not None else None

        return {
            "ok": True,
            "days": days,
            "csat_trend": csat_trend,
            "level_trend": level_trend,
            "delta": {
                "csat_current": curr_avg,
                "csat_previous": prev_avg,
                "csat_delta": delta_csat,
                "direction": (
                    "up" if delta_csat and delta_csat > 0.05
                    else "down" if delta_csat and delta_csat < -0.05
                    else "stable"
                ),
            },
        }


def register_ab_testing_route(app, *, api_auth):
    """S1：注册 A/B 测试管理 API（主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/ab-tests")
    async def api_list_ab_tests(
        request: Request,
        status: str = "",
        _=Depends(api_auth),
    ):
        """S1：列出所有 A/B 测试（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        tests = ab.list_tests(status=status)
        return {"ok": True, "tests": tests, "count": len(tests)}

    @app.post("/api/workspace/ab-tests")
    async def api_create_ab_test(
        request: Request,
        _=Depends(api_auth),
    ):
        """S1：创建新 A/B 测试（主管专属）。

        Body: {name, intent_filter, template_a_id, template_b_id, description?, min_sample?}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        name = str(body.get("name") or "").strip()
        intent_filter = str(body.get("intent_filter") or "").strip()
        tpl_a = str(body.get("template_a_id") or "").strip()
        tpl_b = str(body.get("template_b_id") or "").strip()
        if not name or not tpl_a or not tpl_b:
            raise HTTPException(400, tr(request, "err.draft.ab_fields_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        test_id = ab.create_test(
            name=name,
            intent_filter=intent_filter,
            template_a_id=tpl_a,
            template_b_id=tpl_b,
            description=str(body.get("description") or ""),
            min_sample=int(body.get("min_sample") or 30),
            created_by=_session_agent_id(request),
        )
        return {"ok": True, "test_id": test_id}

    @app.get("/api/workspace/ab-tests/{test_id}/results")
    async def api_ab_test_results(
        request: Request,
        test_id: str,
        _=Depends(api_auth),
    ):
        """S1：获取 A/B 测试详细结果（含显著性检验，主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        results = ab.get_results(test_id)
        if "error" in results:
            raise HTTPException(404, results["error"])
        return {"ok": True, **results}

    @app.post("/api/workspace/ab-tests/{test_id}/stop")
    async def api_stop_ab_test(
        request: Request,
        test_id: str,
        _=Depends(api_auth),
    ):
        """S1：手动停止 A/B 测试（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        ok = ab.stop_test(test_id, reason="manual_api")
        if not ok:
            raise HTTPException(404, tr(request, "err.draft.test_not_found", id=test_id))
        return {"ok": True, "test_id": test_id, "status": "stopped"}


def register_trace_route(app, *, api_auth):
    """S3：注册 /api/workspace/trace/{trace_id}（全链路时间线查询）。"""
    from fastapi import Depends

    @app.get("/api/workspace/trace/{trace_id}")
    async def api_trace_timeline(
        request: Request,
        trace_id: str,
        _=Depends(api_auth),
    ):
        """S3：重建指定 trace_id 的完整调用链时间线。

        调用链：ingest → draft_created → audit → survey_scheduled
        主管和普通坐席均可访问（用于自助排查生产问题）。
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.tracer import TraceTimeline
        tl = TraceTimeline(inbox)
        result = tl.build(trace_id)
        if not result.get("found"):
            raise HTTPException(404, tr(request, "err.draft.trace_not_found", id=trace_id))
        return {"ok": True, **result}

    @app.get("/api/workspace/trace")
    async def api_recent_traces(
        request: Request,
        limit: int = 20,
        platform: str = "",
        _=Depends(api_auth),
    ):
        """S3：列出最近的 trace_id（主管专属）。

        返回最近 limit 条对话的 trace_id + 基本信息，便于主管选取追踪。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            with inbox._lock:
                q = """SELECT conversation_id, trace_id, platform, msg_count, updated_at,
                              last_intent, last_emotion
                       FROM conversation_meta
                       WHERE trace_id != ''"""
                params = []
                if platform:
                    q += " AND platform=?"
                    params.append(platform)
                q += " ORDER BY updated_at DESC LIMIT ?"
                params.append(max(1, min(100, limit)))
                rows = inbox._conn.execute(q, params).fetchall()
            return {
                "ok": True,
                "traces": [dict(r) for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            raise HTTPException(500, str(e))


def register_anomaly_route(app, *, api_auth):
    """S2：注册 /api/workspace/anomaly（异常检测状态查询，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/anomaly")
    async def api_anomaly_check(
        request: Request,
        _=Depends(api_auth),
    ):
        """S2：即时运行异常检测并返回结果（主管专属）。

        不触发告警；仅返回当前检测结果供主管查看。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        from src.inbox.anomaly import AnomalyDetector
        cfg = getattr(request.app.state, "cfg", {}) or {}
        detector = AnomalyDetector(inbox, cfg)
        results = detector.run_full_check()
        anomaly_dicts = [r.to_dict() for r in results]
        anomalies = [d for d in anomaly_dicts if d["is_anomaly"]]

        return {
            "ok": True,
            "enabled": detector.is_enabled(),
            "sensitivity": detector._sensitivity(),
            "baseline_days": detector._baseline_days(),
            "metrics_checked": len(results),
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "all_metrics": anomaly_dicts,
        }


def register_workload_route(app, *, api_auth):
    """R2：注册 /api/workspace/workload（坐席工作负荷均衡，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/workload")
    async def api_agent_workload(
        request: Request,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """R2：返回坐席工作负荷（主管专属）。

        ?agent_id=xxx → 单坐席详情
        不带参数 → 所有在线坐席负荷列表（用于仪表板均衡视图）
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        cfg = getattr(request.app.state, "cfg", {}) or {}
        max_cap = int((cfg.get("workspace") or {}).get("max_concurrent_convs") or 0)

        if agent_id:
            wl = inbox.get_agent_workload(agent_id.strip())
            if max_cap > 0:
                wl["overloaded"] = wl["active_convs"] >= max_cap
            return {"ok": True, "workload": wl, "max_cap": max_cap}

        workloads = inbox.list_agent_workloads(max_load_cap=max_cap)
        overloaded = [w for w in workloads if w.get("overloaded")]
        return {
            "ok": True,
            "workloads": workloads,
            "max_cap": max_cap,
            "total_agents": len(workloads),
            "overloaded_count": len(overloaded),
            "lightest_agent": inbox.get_lightest_agent(max_load_cap=max_cap),
        }


def register_kb_stats_route(app, *, api_auth):
    """Q2+Q3：注册 KB 命中率统计 + 质量评分分布（主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/kb-stats")
    async def api_kb_stats(
        request: Request,
        days: int = 7,
        _=Depends(api_auth),
    ):
        """Q3：返回 KB 条目推荐/点击/使用统计（主管专属）。

        Query: ?days=7（过去 N 天，默认 7 天）
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        _days = max(1, min(90, int(days or 7)))
        since_ts = time.time() - _days * 86400
        stats = inbox.get_kb_hit_stats(since_ts=since_ts, top_n=30)
        # 额外提供低命中率列表（命中率<30%且推荐>=3次）
        low_hit = sorted(
            [s for s in stats if s["recommended"] >= 3 and s["hit_rate"] < 30],
            key=lambda x: x["hit_rate"],
        )[:10]
        return {
            "ok": True,
            "days": _days,
            "entries": stats,
            "low_hit_entries": low_hit,
        }

    @app.post("/api/workspace/kb-click")
    async def api_kb_click(
        request: Request,
        _=Depends(api_auth),
    ):
        """Q3：记录坐席点击了某次 KB 推荐（client-side tracking）。

        Body: {rec_id, used_in_draft?, draft_id?}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        rec_id = str(body.get("rec_id") or "").strip()
        if not rec_id:
            raise HTTPException(400, tr(request, "err.draft.rec_id_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        inbox.click_kb_recommendation(
            rec_id=rec_id,
            used_in_draft=bool(body.get("used_in_draft")),
            draft_id=str(body.get("draft_id") or ""),
        )
        return {"ok": True, "rec_id": rec_id}

    @app.get("/api/workspace/quality-stats")
    async def api_quality_stats(
        request: Request,
        days: int = 7,
        _=Depends(api_auth),
    ):
        """Q2：草稿质量分分布统计（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        _days = max(1, min(90, int(days or 7)))
        since_ts = time.time() - _days * 86400
        stats = inbox.list_draft_quality_stats(since_ts=since_ts)
        return {"ok": True, "days": _days, **stats}


def register_workspace_route(app, *, api_auth):
    """P3：注册 /api/workspace/workspaces（多租户工作区 CRUD，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/workspaces")
    async def api_list_workspaces(request: Request, _=Depends(api_auth)):
        """P3：列出所有工作区（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        workspaces = inbox.list_workspaces()
        # 为每个工作区追加统计
        for ws in workspaces:
            try:
                ws["stats"] = inbox.get_workspace_stats(ws["workspace_id"])
            except Exception:
                ws["stats"] = {}
        # 当前工作区（从 session 读，默认 default）
        try:
            current_ws = request.scope.get("session", {}).get("workspace_id", "default")
        except Exception:
            current_ws = "default"
        return {"ok": True, "workspaces": workspaces, "current": current_ws}

    @app.post("/api/workspace/workspaces")
    async def api_upsert_workspace(request: Request, _=Depends(api_auth)):
        """P3：创建或更新工作区配置（主管专属）。

        Body: {workspace_id, display_name, config}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        ws_id = str(body.get("workspace_id") or "").strip()
        if not ws_id:
            raise HTTPException(400, tr(request, "err.draft.workspace_id_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        inbox.upsert_workspace(
            ws_id,
            display_name=str(body.get("display_name") or ""),
            config=body.get("config") or {},
        )
        stats = inbox.get_workspace_stats(ws_id)
        return {"ok": True, "workspace_id": ws_id, "stats": stats}

    @app.get("/api/workspace/workspaces/{workspace_id}/stats")
    async def api_workspace_stats(request: Request, workspace_id: str, _=Depends(api_auth)):
        """P3：返回指定工作区统计（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        return {"ok": True, **inbox.get_workspace_stats(workspace_id)}


def register_kb_archive_route(app, *, api_auth):
    """P2：注册 POST /api/workspace/kb-archive（优质回复存入知识库，主管专属）。"""
    from fastapi import Depends

    @app.post("/api/workspace/kb-archive")
    async def api_workspace_kb_archive(
        request: Request,
        _=Depends(api_auth),
    ):
        """P2：将一条已审批草稿的回复文本一键归档进知识库。

        Request body: {
            "draft_id":   str,       # 草稿 ID
            "title":      str,       # KB 条目标题（必填）
            "category":   str,       # 分类（可选，默认"客服回复"）
            "triggers":   list[str], # 关键词触发器（可选）
            "scenario":   str,       # 适用场景描述（可选）
            "language":   str,       # 语言（可选，默认 zh）
        }

        主管专属；坐席可"推荐归档"，触发主管审核（future）。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))

        draft_id = str(body.get("draft_id") or "").strip()
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, tr(request, "err.draft.title_required"))

        kb = getattr(request.app.state, "kb_store", None)
        if kb is None:
            raise HTTPException(503, tr(request, "err.svc.kb_not_ready"))

        # 获取草稿内容（final_text 优先，回退到 draft_text）
        draft_text = ""
        peer_text = ""
        conversation_id = ""
        intent = ""

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is not None and draft_id:
            try:
                draft = inbox.get_draft(draft_id)
                if draft is not None:
                    draft_text = str(draft.get("final_text") or draft.get("draft_text") or "")
                    peer_text = str(draft.get("peer_text") or "")
                    conversation_id = str(draft.get("conversation_id") or "")
            except Exception:
                pass

            # 从 conversation_meta 获取意图标签
            if conversation_id:
                try:
                    meta = inbox.get_conv_meta(conversation_id)
                    if meta:
                        intent = str(meta.get("last_intent") or "")
                except Exception:
                    pass

        # 触发器：优先用请求中的，回退到意图标签
        triggers = list(body.get("triggers") or [])
        if not triggers and intent:
            triggers = [intent]

        # 构建 KB 条目
        agent_id = _session_agent_id(request)
        entry_data = {
            "category": str(body.get("category") or "客服回复"),
            "title": title,
            "triggers": triggers,
            "scenario": str(body.get("scenario") or (f"适用场景: {peer_text[:100]}" if peer_text else "")),
            "steps": "",
            "principles": "",
            "example_reply_zh": draft_text,
            "forbidden": "",
            "enabled": 1,
            "reply_mode": "direct",
            "use_count": 0,
            "rating": 0.0,
        }

        try:
            entry_id = kb.add_entry(entry_data)
        except Exception as e:
            raise HTTPException(500, tr(request, "err.draft.kb_write_failed", err=e))

        # 写审计（便于溯源）
        if inbox is not None and draft_id:
            try:
                inbox.record_draft_audit(
                    draft_id,
                    autopilot_level="",
                    action="kb_archived",
                    agent_id=agent_id,
                    reason=f"KB entry_id={entry_id}, title={title[:40]}",
                    conversation_id=conversation_id,
                )
            except Exception:
                pass

        return {
            "ok": True,
            "entry_id": entry_id,
            "title": title,
            "draft_id": draft_id,
        }


def register_my_perf_route(app, *, api_auth):
    """O3：注册 GET /api/workspace/my-perf（坐席自助绩效查询，无需主管权限）。"""
    from fastapi import Depends
    import time as _time

    @app.get("/api/workspace/my-perf")
    async def api_workspace_my_perf(
        request: Request,
        days: int = 7,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """O3：坐席自助绩效查询。

        无需主管权限；
        agent_id: 可选，主管可指定其他坐席；坐席只能查自己。
        days: 1 / 7（默认）/ 30 / 90
        返回：{agent_id, total, approved, rejected, autosend, avg_csat, timeline, rank}
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        days = max(1, min(90, int(days)))
        now = _time.time()
        since_ts = now - days * 86400

        # 当前登录坐席 ID
        current_uid = _session_agent_id(request)
        is_sup = _is_supervisor(request)

        # 权限：非主管只能查自己
        target_id = str(agent_id or "").strip()
        if not target_id:
            target_id = current_uid
        elif not is_sup and target_id != current_uid:
            raise HTTPException(403, tr(request, "err.perm.agent_self_only"))

        # 个人绩效
        perf_list = inbox.get_agent_perf(since_ts=since_ts, agent_id=target_id)
        perf = perf_list[0] if perf_list else {
            "agent_id": target_id, "total": 0, "approved": 0,
            "rejected": 0, "autosend": 0, "avg_csat": None,
        }

        # 趋势（每天一个点）
        timeline = inbox.get_agent_perf_timeline(
            since_ts=since_ts,
            agent_id=target_id,
            bucket_sec=86400,
        )

        # 排名：在全部坐席中的 CSAT 排名
        all_perf = inbox.get_agent_perf(since_ts=since_ts)
        all_sorted = sorted(
            [p for p in all_perf if p.get("total", 0) > 0],
            key=lambda x: float(x.get("avg_csat") or -1),
            reverse=True,
        )
        rank = next(
            (i + 1 for i, p in enumerate(all_sorted) if p.get("agent_id") == target_id),
            None,
        )
        total_agents = len(all_sorted)

        # 近期处置记录（最近 10 条）
        recent_decisions = [
            r for r in inbox.list_draft_audit(limit=200)
            if str(r.get("agent_id") or "") == target_id
        ][:10]

        return {
            "ok": True,
            "agent_id": target_id,
            "days": days,
            "perf": perf,
            "timeline": timeline,
            "rank": rank,
            "total_agents": total_agents,
            "recent_decisions": recent_decisions,
        }


def register_leaderboard_route(app, *, api_auth):
    """N3：注册 GET /api/workspace/leaderboard（CSAT 坐席排行榜，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/leaderboard")
    async def api_workspace_leaderboard(
        request: Request,
        period: str = "weekly",
        limit: int = 20,
        _=Depends(api_auth),
    ):
        """N3：坐席 CSAT 排行榜（主管专属）。

        period: daily（过去 24h）| weekly（过去 7d）| monthly（过去 30d）
        limit: 最多返回 N 名坐席（默认 20）
        返回按 avg_csat DESC, total DESC 排序的坐席列表，含排名 + 徽章。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        import time as _time
        period_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(str(period), 7)
        since_ts = _time.time() - period_days * 86400

        perf = inbox.get_agent_perf(since_ts=since_ts)
        # 过滤出有 avg_csat 的坐席 + 排序
        ranked = sorted(
            [p for p in perf if p.get("total", 0) > 0],
            key=lambda x: (
                float(x.get("avg_csat") or -1),
                int(x.get("total") or 0),
            ),
            reverse=True,
        )
        ranked = ranked[:max(1, int(limit))]

        # 加排名 + 徽章
        _BADGES = {1: "🏆", 2: "🥈", 3: "🥉"}
        result = []
        for i, p in enumerate(ranked, 1):
            csat = p.get("avg_csat")
            p["rank"] = i
            p["badge"] = _BADGES.get(i, "")
            p["csat_stars"] = (
                "⭐" * int(round(csat)) + "☆" * (5 - int(round(csat)))
                if csat is not None else "—"
            )
            result.append(p)

        return {
            "ok": True,
            "period": period,
            "since_ts": since_ts,
            "updated_at": _time.time(),
            "leaderboard": result,
        }


def register_broadcast_route(app, *, api_auth):
    """M2：注册 POST /api/workspace/broadcast（主管广播事件到 EventBus，触发 Webhook）。"""
    from fastapi import Depends

    @app.post("/api/workspace/broadcast")
    async def api_workspace_broadcast(
        request: Request,
        _=Depends(api_auth),
    ):
        """M2：广播任意事件到 EventBus（主管专属，用于简报推送等）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        event_type = str(body.get("type") or "").strip()
        if not event_type:
            raise HTTPException(400, tr(request, "err.draft.type_required"))
        data = body.get("data") or {}
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish(event_type, data)
        except Exception as e:
            raise HTTPException(500, tr(request, "err.draft.eventbus_failed", err=e))
        return {"ok": True, "type": event_type}


def register_report_route(app, *, api_auth):
    """M2：注册 GET /api/workspace/report（工作日报/周报，主管专属）。"""
    from fastapi import Depends
    from fastapi.responses import PlainTextResponse

    @app.get("/api/workspace/report")
    async def api_workspace_report(
        request: Request,
        period: str = "daily",
        format: str = "json",
        _=Depends(api_auth),
    ):
        """M2：工作日报/周报 API（主管专属）。

        period: daily（过去 24h，默认）| weekly（过去 7 天）
        format: json（默认）| text（Webhook 推送格式）| html（仪表盘嵌入）
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        from src.inbox.report_generator import ReportGenerator
        gen = ReportGenerator(
            inbox_store=inbox,
            draft_service=getattr(request.app.state, "draft_service", None),
            app_state=request.app.state,
        )
        report_data = gen.generate(period=period)

        fmt = str(format or "json").lower()
        if fmt == "text":
            return PlainTextResponse(gen.format_text(report_data))
        if fmt == "html":
            from fastapi.responses import HTMLResponse
            return HTMLResponse(gen.format_html(report_data))
        return {"ok": True, **report_data}


def register_export_route(app, *, api_auth):
    """J3：注册 GET /api/workspace/export（CSV 导出，主管专属）。

    admin.py 和 main.py 各调用一次。FastAPI 允许同一路由被重复注册，
    重复注册时不报错，但为避免重复，应在两者之一中只调用一次。
    实际只在 admin.py 中调用，以保持 inventory 测试覆盖。
    """
    import csv
    import datetime
    import io
    from fastapi import Depends
    from fastapi.responses import StreamingResponse

    @app.get("/api/workspace/export")
    async def api_workspace_export(
        request: Request,
        export_type: str = "drafts",
        days: int = 7,
        _=Depends(api_auth),
    ):
        """J3：导出工作台数据为 CSV（主管专属）。

        export_type: drafts | audit | perf
        days: 最近 N 天（1–90），默认 7
        返回 CSV 文件流（BOM-UTF8，兼容 Excel）。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        days_int = max(1, min(90, int(days or 7)))
        cutoff_ts = time.time() - days_int * 86400
        etype = str(export_type or "drafts").lower()

        inbox_store = getattr(request.app.state, "inbox_store", None)
        svc = getattr(request.app.state, "draft_service", None)
        buf = io.StringIO()
        w = csv.writer(buf)

        if etype == "audit" and inbox_store is not None:
            w.writerow(["时间", "草稿ID", "坐席ID", "动作", "风险等级", "自动化等级", "原因", "会话ID"])
            logs = inbox_store.list_draft_audit(limit=2000)
            for row in logs:
                if float(row.get("ts") or 0) < cutoff_ts:
                    continue
                ts_str = datetime.datetime.fromtimestamp(
                    float(row.get("ts") or 0)
                ).strftime("%Y-%m-%d %H:%M:%S")
                w.writerow([
                    ts_str, row.get("draft_id", ""), row.get("agent_id", ""),
                    row.get("action", ""), row.get("risk_level", ""),
                    row.get("autopilot_level", ""), row.get("reason", ""),
                    row.get("conversation_id", ""),
                ])

        elif etype == "perf" and inbox_store is not None:
            w.writerow(["坐席ID", "总处理", "批准", "拒绝", "自动发送", "强制放行"])
            perf = inbox_store.get_agent_perf(since_ts=cutoff_ts)
            for row in perf:
                w.writerow([
                    row.get("agent_id", ""),
                    row.get("total", 0), row.get("approved", 0),
                    row.get("rejected", 0), row.get("autosend", 0),
                    row.get("force_override", 0),
                ])

        else:  # drafts（默认）
            w.writerow([
                "草稿ID", "会话ID", "平台", "账号", "状态", "风险等级",
                "自动化等级", "草稿文本（截断）", "客户文本（截断）", "处置人", "创建时间",
            ])
            if svc is not None:
                for d in svc.list_drafts(limit=2000):
                    ca = float(d.get("created_at") or d.get("created_ts") or 0)
                    if ca > 0 and ca < cutoff_ts:
                        continue
                    ts_str = datetime.datetime.fromtimestamp(ca).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ) if ca > 0 else ""
                    w.writerow([
                        d.get("draft_id", ""), d.get("conversation_id", ""),
                        d.get("platform", ""), d.get("account_id", ""),
                        d.get("status", ""), d.get("risk_level", ""),
                        d.get("autopilot_level", ""),
                        str(d.get("draft_text", ""))[:200],
                        str(d.get("peer_text", ""))[:100],
                        d.get("decided_by", ""), ts_str,
                    ])

        buf.seek(0)
        filename = f"ws_{etype}_{datetime.date.today().isoformat()}.csv"
        return StreamingResponse(
            iter(["\ufeff" + buf.read()]),  # BOM：兼容 Excel 直接打开 UTF-8
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


# ── C1：坐席绩效 API（不依赖 draft_service，直读 inbox_store） ──────────────

def register_agent_perf_routes(app, *, api_auth, page_auth, templates, config_manager=None):
    """坐席绩效看板：API + 页面路由（admin.py 调用）。

    GET /api/workspace/agent-perf        — 每坐席聚合指标（主管专属）
    GET /api/workspace/agent-perf/timeline — 趋势数据（主管专属）
    GET /workspace/agent-perf            — 绩效看板页面（主管专属）
    """
    import time as _time
    from fastapi import Depends
    from fastapi.responses import HTMLResponse, RedirectResponse

    def _get_store(request):
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            from fastapi import HTTPException
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        return store

    def _ctx(request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": sess.get("display_name") or sess.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    @app.get("/api/workspace/agent-perf")
    async def api_agent_perf(
        request: Request,
        days: int = 30,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """每坐席草稿处置聚合绩效（主管专属）。"""
        if not _is_supervisor(request):
            from fastapi import HTTPException
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 30))) * 86400
        rows = store.get_agent_perf(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "agents": rows, "days": int(days), "total_agents": len(rows)}

    @app.get("/api/workspace/agent-perf/timeline")
    async def api_agent_perf_timeline(
        request: Request,
        days: int = 14,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """坐席绩效趋势（按天分桶；主管专属）。"""
        if not _is_supervisor(request):
            from fastapi import HTTPException
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 14))) * 86400
        timeline = store.get_agent_perf_timeline(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "timeline": timeline, "days": int(days)}

    @app.get("/api/workspace/agent-copilot-stats")
    async def api_agent_copilot_stats(
        request: Request,
        days: int = 14,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """P54：Copilot 采纳率与质量回放（主管专属）。"""
        if not _is_supervisor(request):
            from fastapi import HTTPException
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 14))) * 86400
        stats = store.get_copilot_stats(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "days": int(days), **stats}

    @app.get("/workspace/agent-perf", response_class=HTMLResponse)
    async def workspace_agent_perf_page(request: Request, _=Depends(page_auth)):
        """坐席绩效看板（主管专属；非主管重定向到工作台）。"""
        if not _is_supervisor(request):
            return RedirectResponse(url="/workspace", status_code=302)
        return templates.TemplateResponse(request, "agent_perf.html", _ctx(request))


# ── 页面路由（需 templates + page_auth，由 admin.py create_app 调用） ──────

def register_drafts_page_routes(
    app,
    *,
    page_auth,
    templates,
    config_manager=None,
):
    """挂载草稿审批工作台页面路由（需 Jinja2 templates + page_auth）。

    与 register_drafts_routes（API 路由）分离注册：
    - API 路由在 main.py 里 app 创建后追加（不依赖 templates）
    - 页面路由在 admin.py create_app 内调用（需 templates 和 page_auth）
    """
    from fastapi import Depends
    from fastapi.responses import HTMLResponse, RedirectResponse

    def _ctx(request: Request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": (
                sess.get("display_name") or sess.get("username") or ""
            ),
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    @app.get("/workspace/drafts", response_class=HTMLResponse)
    async def workspace_drafts_page(
        request: Request, _=Depends(page_auth),
    ):
        """草稿审批工作台（坐席/主管均可进；L4 需主管才能 force-override）。"""
        return templates.TemplateResponse(request, "draft_review.html", _ctx(request))

    @app.get("/workspace/draft-audit", response_class=HTMLResponse)
    async def workspace_draft_audit_page(
        request: Request, _=Depends(page_auth),
    ):
        """草稿处置审计日志页（主管专属；非主管重定向到草稿工作台）。"""
        if not _is_supervisor(request):
            return RedirectResponse(url="/workspace/drafts", status_code=302)
        return templates.TemplateResponse(
            request, "draft_audit_page.html", _ctx(request)
        )
