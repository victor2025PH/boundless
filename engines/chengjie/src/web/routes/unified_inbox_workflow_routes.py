"""统一收件箱——下一步动作推荐 / 自定义动作·工作链 / 工作链执行可视化路由域（巨石拆分 slice 20）。

把两段连续且共享依赖面的子域，从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_workflow_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 37 下一步动作推荐 + 自定义动作/工作链：
  ``conv/{id}/next-actions`` / ``conv/{id}/execute-action`` /
  ``workflow-actions`` CRUD / ``workflow-chains`` CRUD
- Phase 47 工作链执行可视化：
  ``chain-executions`` / ``conv/{id}/chain-executions`` /
  ``chain-executions/{exec_id}/cancel`` / ``conv/{id}/start-chain``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 20 端点契约断言）。

依赖全部朝下：services 存储、auth._agent_from_request；推荐器/工作链监控/event_bus
均为 handler 内局部 import。只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def workflows_disabled_reason_cfg(cfg: Any) -> str:
    """C3/D1/E1：工作流模块可用性判定（config 版单一事实源），返回
    ``""``（可用）/ ``config`` / ``license``。

    两道闸（顺序有意义——运营显式关闭是明确意图，报「未启用」而非「请升级」）：
    1. ``inbox.workflows.enabled``（缺省 True=旧行为零变化，运营 kill-switch）；
    2. 授权档位 ``feature_gate.feature_enabled("workflows")``（FEATURE_MIN_PLAN
       登记为 pro；gate 总开关默认关=全放行，licensing 层异常恒放行）。

    消费方：本文件 API 闸门（`_require_workflows` → 403 两种文案分流）+
    admin.py 的 ``/workspace/workflows`` 页面路由（license 锁 → 302 /membership
    升级引导；运营关闭 → 404 模块不存在于此部署）。判定逻辑必须同源——
    「API 拦了页面没拦」或口径漂移都是缝。

    语义边界（商业化分层打包用，不是安全闸门）：
    - 关闭/锁定时链管理/执行/漏斗端点返回 403，工作台「工作链执行」卡据此自动隐藏；
    - **只拦新动作，不中断在途执行**——WorkflowRunner 刻意不闸（运行中的客户跟进
      不该因打包开关被悄悄掐断；要停用先去 /workflows 取消）；
    - NBA 下一步动作推荐 / 自定义动作不受影响（独立能力面），仅其中「启动工作链」
      动作分支同步拦截（否则「不含工作流模块」的打包经 NBA 漏出链能力）；
    - 读配置失败按启用处理（配置系统异常不应把已出货模块打死）。
    """
    try:
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            return ""
        wf = (cfg.get("inbox") or {}).get("workflows") or {}
        if not bool(wf.get("enabled", True)):
            return "config"
        try:
            from src.licensing.feature_gate import feature_enabled
            if not feature_enabled("workflows", cfg):
                return "license"
        except Exception:
            pass  # licensing 层异常恒放行（与 feature_gate 内部口径一致）
        return ""
    except Exception:
        return ""


def _workflows_disabled_reason(request: Request) -> str:
    try:
        cfg = getattr(
            getattr(request.app.state, "config_manager", None), "config", None) or {}
    except Exception:
        cfg = {}
    return workflows_disabled_reason_cfg(cfg)


def _workflows_enabled(request: Request) -> bool:
    return _workflows_disabled_reason(request) == ""


def _require_workflows(request: Request) -> None:
    """链域端点统一闸门：不可用 → 403（前端组件按 status 隐藏整卡）。
    文案分流：运营关闭=「未启用」；档位不够=「升级解锁」——坐席该做的事不同。
    仅 license 分支计锁触达（E6 定价信号；config 关闭是部署选择，刻意不计）。"""
    reason = _workflows_disabled_reason(request)
    if reason == "config":
        raise HTTPException(403, tr(request, "err.ws.workflows_disabled"))
    if reason == "license":
        try:
            from src.web.feature_lock_stats import get_feature_lock_stats
            get_feature_lock_stats().record("workflows", "api")
        except Exception:
            pass
        raise HTTPException(403, tr(request, "err.lic.feature_locked"))


def register_workflow_routes(app, *, api_auth) -> None:
    """挂载动作推荐 / 自定义动作·工作链 CRUD / 工作链执行可视化端点。"""

    def _goal_chain_event(request: Request, conversation_id: str, kind: str, detail: str = "") -> None:
        """C 弱联动：链生命周期回写当前会话活跃目标的事件台账（best-effort）。"""
        try:
            cm = getattr(request.app.state, "config_manager", None)
            if cm is None:
                return
            from src.companion.goals.service import record_chain_event
            record_chain_event(
                getattr(cm, "config", None) or {},
                getattr(cm, "config_path", None),
                conversation_id, kind, detail)
        except Exception:
            logger.debug("goal chain event 回写失败（已忽略）", exc_info=True)

    # ─── Phase 37: 下一步动作推荐 + 自定义动作/工作链 ───────────────────

    @app.get("/api/workspace/conv/{conversation_id}/next-actions")
    async def api_conv_next_actions(conversation_id: str, request: Request):
        """AA1：推荐当前会话下一步动作（内置场景动作 + 用户自定义）。

        Query 参数可传入会话上下文加速推荐（否则从 store 自动拉取）：
          silence_hours, message_count, churn_risk_level
        """
        api_auth(request)
        store = _inbox_store(request)
        from src.inbox.next_action_recommender import NextActionRecommender

        # 拉取最新消息（用于信号检测）
        last_msg_text = ""
        last_msg_direction = "in"
        message_count = 0
        silence_hours = 0.0
        churn_risk_level = ""
        risk_signals: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {}

        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text, ts FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 30""",
                    (conversation_id,),
                ).fetchall()
                if rows:
                    message_count = store._conn.execute(
                        "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchone()["c"]
                    last = rows[0]
                    last_msg_text = str(last["text"] or "")
                    last_msg_direction = str(last["direction"] or "in")
                    import time as _t
                    silence_hours = max(0.0, (_t.time() - float(last["ts"] or 0)) / 3600)

                # 读取流失风险
                meta = store.get_conv_meta(conversation_id) or {}
                churn_raw = str(meta.get("churn_risk") or "").strip()
                if churn_raw:
                    import json as _j
                    cd = _j.loads(churn_raw)
                    churn_risk_level = str(cd.get("level") or "")
            except Exception:
                logger.debug("next-actions 上下文拉取失败（已忽略）", exc_info=True)

        # 拉取自定义动作（已启用）
        custom_actions: List[Dict[str, Any]] = []
        if store is not None:
            try:
                raw = store.list_workflow_actions()
                for act in raw:
                    import json as _j
                    try:
                        cfg = _j.loads(act.get("config_json") or "{}")
                    except Exception:
                        cfg = {}
                    try:
                        triggers = _j.loads(act.get("trigger_conditions") or '["any"]')
                    except Exception:
                        triggers = ["any"]
                    custom_actions.append({**act, "config": cfg, "trigger_conditions": triggers})
            except Exception:
                pass

        # P1-198：回访任务可部署级关闭（与工作目标计划语义重叠时避免双入口）。
        _followup_on = True
        try:
            _cfg = getattr(
                getattr(request.app.state, "config_manager", None), "config", None) or {}
            _na = ((_cfg.get("inbox") or {}).get("next_actions") or {})
            _followup_on = bool(_na.get("follow_up_task", True))
        except Exception:
            _followup_on = True

        rec = NextActionRecommender()
        actions = rec.recommend(
            risk_signals=risk_signals,
            last_msg_text=last_msg_text,
            last_msg_direction=last_msg_direction,
            message_count=message_count,
            silence_hours=silence_hours,
            churn_risk_level=churn_risk_level,
            custom_actions=custom_actions,
            limit=6,
            followup_task_enabled=_followup_on,
        )
        # P1-198 情绪标记状态化（2026-08-02 拆双维度）：
        #   current_mood      = conv_tags ∩ emotion 组（后打的胜出）——坐席标注的客户情绪
        #   current_attention = conv_tags ∩ attention 组——跟进状态（不进 AI 仲裁）
        #   mood_manual       = 转向信号状态（effective_mood：TTL/在场校验；供卡片显示
        #                       「x 小时前 / 已失效」，与消费链完全同一判据——预判≠护栏是大忌）
        #   ai_emotion        = 机器自动分析（last_emotion/intensity/trend）——让坐席看见
        #                       「AI 自己的判断」，人工标注的覆写语义才立得住
        # 单一事实源在 recommender.MOOD_TAGS_*，前端据此高亮 chip + 状态行。
        current_mood = ""
        current_attention = ""
        mood_manual: Dict[str, Any] = {}
        ai_emotion: Dict[str, Any] = {}
        _mood_ms: Dict[str, Any] = {"enabled": True, "ttl_hours": 24.0}
        if store is not None:
            try:
                from src.inbox.effective_mood import (
                    manual_mood_state,
                    resolve_mood_steering_cfg,
                )
                from src.inbox.next_action_recommender import (
                    MOOD_TAGS_ATTENTION,
                    MOOD_TAGS_EMOTION,
                )
                _tags = store.get_conv_tags(conversation_id) or []
                for _t in reversed(list(_tags)):
                    if not current_mood and _t in MOOD_TAGS_EMOTION:
                        current_mood = _t
                    if not current_attention and _t in MOOD_TAGS_ATTENTION:
                        current_attention = _t
                    if current_mood and current_attention:
                        break
                import time as _t2
                _cfg_root = getattr(
                    getattr(request.app.state, "config_manager", None),
                    "config", None) or {}
                _mood_ms = resolve_mood_steering_cfg(_cfg_root)
                _st = manual_mood_state(
                    meta, now=_t2.time(), ttl_hours=_mood_ms["ttl_hours"])
                _same = bool(current_mood and _st.get("tag") == current_mood)
                mood_manual = {
                    "tag": current_mood,
                    "active": bool(
                        _mood_ms["enabled"] and _same and _st.get("active")),
                    "age_hours": (
                        round(float(_st.get("age_hours") or 0.0), 1)
                        if (_same and float(_st.get("age_hours") or -1) >= 0)
                        else None),
                    "by": _st.get("by") if _same else "",
                }
                ai_emotion = {
                    "label": str((meta or {}).get("last_emotion") or ""),
                    "intensity": float(
                        (meta or {}).get("last_emotion_intensity", -1) or -1),
                    "trend": str((meta or {}).get("emotion_trend") or ""),
                }
            except Exception:
                logger.debug("next-actions 读当前情绪失败（已忽略）", exc_info=True)
        # 情绪卡展示层 i18n（值仍存中文 canonical；EN 界面此前直显中文，P1-198 客户实测点名）
        try:
            from src.inbox.next_action_recommender import MOOD_TAG_I18N
            for _a in actions:
                if _a.get("action_id") == "__add_internal_note":
                    _a["name"] = tr(request, "inbox.nba.note_name", _a.get("name"))
                    _cfg_n = dict(_a.get("config") or {})
                    _cfg_n["hint"] = tr(
                        request, "inbox.nba.note_hint", _cfg_n.get("hint"))
                    _a["config"] = _cfg_n
                    continue
                if _a.get("action_id") != "__add_mood_tag":
                    continue
                _a["name"] = tr(request, "inbox.nba.mood_name", _a.get("name"))
                _hint_key = (
                    "inbox.nba.mood_hint_steer"
                    if _mood_ms.get("enabled") else "inbox.nba.mood_hint_plain")
                _cfg_a = dict(_a.get("config") or {})
                _cfg_a["hint"] = tr(
                    request, _hint_key, _cfg_a.get("hint"),
                    ttl=int(_mood_ms.get("ttl_hours") or 24))
                _grp_label = {
                    "emotion": tr(request, "inbox.nba.grp_emotion", "客户情绪"),
                    "attention": tr(request, "inbox.nba.grp_attention", "跟进状态"),
                }
                _cfg_a["tag_groups"] = [
                    {
                        "key": g.get("key") or "",
                        "label": _grp_label.get(g.get("key") or "", ""),
                        "options": [
                            {"value": v,
                             "label": tr(request, MOOD_TAG_I18N.get(v, ""), v)}
                            for v in (g.get("options") or [])
                        ],
                    }
                    for g in (_cfg_a.get("tag_groups") or [])
                ]
                _a["config"] = _cfg_a
        except Exception:
            logger.debug("next-actions 情绪卡 i18n 失败（已忽略）", exc_info=True)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "actions": actions,
            "current_mood": current_mood,
            "current_attention": current_attention,
            "mood_manual": mood_manual,
            "ai_emotion": ai_emotion,
            "context": {
                "message_count": message_count,
                "silence_hours": round(silence_hours, 1),
                "churn_risk_level": churn_risk_level,
                "last_direction": last_msg_direction,
            },
        }

    @app.post("/api/workspace/conv/{conversation_id}/execute-action")
    async def api_conv_execute_action(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：执行一个动作（发话术/创建任务/打标签/启动工作链）。"""
        body = await request.json()
        action_type = str(body.get("action_type") or "")
        config = body.get("config") or {}
        store = _inbox_store(request)
        import time as _t
        now = _t.time()
        result: Dict[str, Any] = {"ok": True, "action_type": action_type}

        if action_type == "task":
            # 创建跟进任务。P1-198 诚实化：此前 contact_id 缺失/contacts 未启用时静默
            # 空转（toast 显示成功、实际什么都没建，198 客户机全部会话 contact_id 为空
            # ——「点了没反应」是最伤信任的一类缺陷）。现在把「没建成」如实报出去。
            due_hours = float(config.get("due_hours") or 72)
            note = str(config.get("note") or "")
            contacts_store = _contacts_store(request)
            agent_id = request.session.get("agent_id") or request.session.get("username") or ""
            if not contacts_store:
                result["ok"] = False
                result["error"] = tr(request, "err.ws.task_contacts_disabled")
            else:
                meta = {}
                try:
                    meta = store.get_conv_meta(conversation_id) if store else {}
                except Exception:
                    meta = {}
                contact_id = (meta or {}).get("contact_id", "")
                if not contact_id:
                    result["ok"] = False
                    result["error"] = tr(request, "err.ws.task_no_contact")
                else:
                    try:
                        contacts_store.add_follow_up_task(
                            contact_id, now + due_hours * 3600, note=note, assignee=agent_id
                        )
                        result["task_created"] = True
                    except Exception:
                        logger.debug("execute-action 建任务失败", exc_info=True)
                        result["ok"] = False
                        result["error"] = tr(request, "err.ws.task_create_failed")

        elif action_type == "tag":
            # 添加标签（情绪/跟进两组组内互斥，见 merge_mood_tag；情绪组同步落
            # arbitration 列 → AI 语气/主动节奏/让路在 TTL 窗内跟随，effective_mood
            # 是唯一仲裁口径）。词表单一事实源仍在 recommender.MOOD_TAGS。
            tag = str(config.get("tag") or "")
            if tag and store:
                try:
                    from src.inbox.effective_mood import apply_mood_tag
                    _agent = str(
                        request.session.get("agent_id")
                        or request.session.get("username") or "")
                    _r = apply_mood_tag(
                        store, conversation_id, tag, by=_agent, now=now)
                    result["tag"] = tag
                    if _r.get("mood_manual"):
                        result["mood_manual"] = _r["mood_manual"]
                except Exception:
                    pass

        elif action_type == "note":
            # 添加内部注解
            body_text = str(config.get("note_body") or config.get("hint") or "")
            agent_id = request.session.get("agent_id") or request.session.get("username") or ""
            agent_name = request.session.get("display_name") or agent_id
            if body_text and store:
                try:
                    store.add_conv_note(
                        conversation_id, body_text,
                        agent_id=agent_id, agent_name=agent_name,
                    )
                    result["note_added"] = True
                except Exception:
                    pass

        elif action_type == "chain":
            # 启动工作链（C3：模块关时软拒——本端点还服务 task/tag/note 等其他动作，
            # 不能整口 403；只掐这一分支防「不含工作流」的打包经 NBA 漏出链能力）
            if not _workflows_enabled(request):
                result["ok"] = False
                result["error"] = tr(request, "err.ws.workflows_disabled")
                return result
            chain_id = str(config.get("chain_id") or "")
            if chain_id and store:
                try:
                    exec_id = store.start_chain_execution(
                        chain_id, conversation_id,
                        {"agent": request.session.get("username")},
                        schedule_first_step=True,
                    )
                    result["exec_id"] = exec_id
                    _ch = store.get_workflow_chain(chain_id) or {}
                    _goal_chain_event(request, conversation_id, "chain_started",
                                      str(_ch.get("name") or chain_id))
                except Exception:
                    pass

        elif action_type == "escalate":
            # 发布升级事件
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("escalation", {
                    "conversation_id": conversation_id,
                    "reason": str(config.get("reason") or "human_escalate"),
                    "initiated_by": request.session.get("username") or "",
                    "ts": now,
                })
                result["escalated"] = True
            except Exception:
                pass

        return result

    # ── 自定义动作管理 ────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-actions")
    async def api_workflow_actions_list(request: Request):
        """AA1：列出所有自定义动作。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "actions": []}
        actions = store.list_workflow_actions()
        import json as _j
        for a in actions:
            try:
                a["config"] = _j.loads(a.get("config_json") or "{}")
            except Exception:
                a["config"] = {}
        return {"ok": True, "actions": actions}

    @app.post("/api/workspace/workflow-actions")
    async def api_workflow_actions_create(request: Request, _=Depends(api_auth)):
        """AA1：创建自定义动作。"""
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        action_id = store.upsert_workflow_action(body)
        return {"ok": True, "action_id": action_id}

    @app.put("/api/workspace/workflow-actions/{action_id}")
    async def api_workflow_actions_update(action_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["action_id"] = action_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        store.upsert_workflow_action(body)
        return {"ok": True, "action_id": action_id}

    @app.delete("/api/workspace/workflow-actions/{action_id}")
    async def api_workflow_actions_delete(action_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_action(action_id)
        return {"ok": ok}

    # ── 工作链管理 ────────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-chains")
    async def api_workflow_chains_list(request: Request):
        """AA1：列出所有工作链。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "chains": []}
        import json as _j
        chains = store.list_workflow_chains()
        for c in chains:
            try:
                c["steps"] = _j.loads(c.get("steps_json") or "[]")
            except Exception:
                c["steps"] = []
            try:
                c["trigger_conditions"] = _j.loads(c.get("trigger_conditions") or "{}")
            except Exception:
                c["trigger_conditions"] = {}
        return {"ok": True, "chains": chains}

    @app.post("/api/workspace/workflow-chains")
    async def api_workflow_chains_create(request: Request, _=Depends(api_auth)):
        _require_workflows(request)
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        chain_id = store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.put("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_update(chain_id: str, request: Request, _=Depends(api_auth)):
        _require_workflows(request)
        body = await request.json()
        body["chain_id"] = chain_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.delete("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_delete(chain_id: str, request: Request, _=Depends(api_auth)):
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_chain(chain_id)
        return {"ok": ok}

    @app.post("/api/workspace/workflow-chains/seed")
    async def api_workflow_chains_seed(request: Request, _=Depends(api_auth)):
        """B1：一键导入出厂模板链（幂等——按固定 chain_id 判重，已存在一律跳过不覆盖）。"""
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        from src.inbox.workflow_starter import ensure_starter_chains
        res = ensure_starter_chains(store)
        return {"ok": True, **res}

    # ─── Phase 47: 工作链执行可视化 ─────────────────────────────────────

    @app.get("/api/workspace/chain-executions")
    async def api_chain_executions_list(
        request: Request,
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ):
        """P47：全局工作链执行监控列表。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "count": 0}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            status=status.strip(), conversation_id=conversation_id.strip(), limit=limit,
        )
        enriched = enrich_executions(rows)
        running = sum(1 for e in enriched if e.get("status") == "running")
        return {
            "ok": True,
            "executions": enriched,
            "count": len(enriched),
            "running_count": running,
        }

    @app.get("/api/workspace/chain-funnel")
    async def api_chain_funnel(request: Request, days: int = 14):
        """B2：链效果漏斗——窗口内启动/完成/失败/取消 + 启动后 72h 回复率（近似归因，
        分母只算已满窗口期的执行）。监控页与 ops 消费同一口径。

        J 推荐跟随率：``rec_follow = {followed, eligible, rate}``——目标归因的
        启动里，goal 模板**有推荐链**的进分母（eligible），启动的恰是推荐链的
        进分子（followed）。模板无推荐 / 目标已删 / goals 未启用 → 不进分母
        （诚实：没有推荐可跟就不算「没跟」）。goal_id 明细在路由层消费后剔除，
        不对外暴露。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "total": {}, "chains": [],
                    "rec_follow": {"followed": 0, "eligible": 0, "rate": None}}
        from src.inbox.workflow_monitor import chain_funnel
        data = chain_funnel(store, days=max(1, min(90, int(days or 14))))
        pairs = data.pop("goal_chain_starts", {}) or {}
        followed = eligible = 0
        try:
            if pairs:
                from src.inbox.workflow_starter import GOAL_CHAIN_REC
                cm = getattr(request.app.state, "config_manager", None)
                gstore = None
                if cm is not None:
                    from src.companion.goals.service import (
                        get_configured_store, goals_enabled,
                    )
                    cfg = getattr(cm, "config", None) or {}
                    if goals_enabled(cfg):
                        gstore = get_configured_store(
                            cfg, getattr(cm, "config_path", None))
                if gstore is not None:
                    for gid, chains_map in pairs.items():
                        try:
                            goal = gstore.get_goal(str(gid)) or {}
                        except Exception:
                            goal = {}
                        rec = GOAL_CHAIN_REC.get(
                            str(goal.get("template") or ""), "")
                        if not rec:
                            continue
                        eligible += sum(int(n) for n in chains_map.values())
                        followed += int(chains_map.get(rec, 0))
        except Exception:
            logger.debug("chain-funnel 推荐跟随计算失败（已忽略）", exc_info=True)
        data["rec_follow"] = {
            "followed": followed,
            "eligible": eligible,
            "rate": round(followed / eligible, 3) if eligible else None,
        }
        # P3 2026-08-09：链推进循环心跳（bootstrap 挂 app.state；空 dict=循环
        # 没挂载）——链推进曾挂在 report.enabled 闸死的调度器上**从未运行**，
        # 「点了启动步骤永不走」这类静默断线从此在监控口可见。
        data["autorun"] = dict(
            getattr(request.app.state, "workflow_autorun_state", None) or {})
        return data

    @app.get("/api/workspace/conv/{conversation_id}/chain-executions")
    async def api_conv_chain_executions(
        conversation_id: str, request: Request, status: str = "", limit: int = 20,
    ):
        """P47：会话级工作链执行记录。"""
        api_auth(request)
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "conversation_id": conversation_id}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            conversation_id=conversation_id, status=status.strip(), limit=limit,
        )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "executions": enrich_executions(rows),
            "count": len(rows),
        }

    @app.post("/api/workspace/chain-executions/{exec_id}/cancel")
    async def api_cancel_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """P47：取消运行中的工作链执行。"""
        _require_workflows(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        ex = store.get_workflow_execution(exec_id)
        if not ex:
            raise HTTPException(404, tr(request, "err.ws.exec_record_not_found"))
        if ex.get("status") != "running":
            raise HTTPException(422, tr(request, "err.ws.only_cancel_running_chain"))
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        reason = str(body.get("reason") or "坐席手动取消").strip()
        agent_id, agent_name = _agent_from_request(request)
        ok = store.cancel_workflow_execution(exec_id)
        if not ok:
            raise HTTPException(422, tr(request, "msg_js_2058"))
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish("workflow_execution_cancelled", {
                "exec_id": exec_id,
                "conversation_id": ex.get("conversation_id"),
                "chain_id": ex.get("chain_id"),
                "chain_name": ex.get("chain_name", ""),
                "agent_id": agent_id,
                "agent_name": agent_name,
                "reason": reason,
                "ts": _t.time(),
            })
        except Exception:
            pass
        from src.inbox.workflow_monitor import enrich_execution
        _goal_chain_event(request, str(ex.get("conversation_id") or ""), "chain_cancelled",
                          str(ex.get("chain_name") or ex.get("chain_id") or ""))
        refreshed = store.get_workflow_execution(exec_id)
        return {
            "ok": True,
            "exec_id": exec_id,
            "execution": enrich_execution(refreshed or ex),
        }

    @app.post("/api/workspace/conv/{conversation_id}/start-chain")
    async def api_conv_start_chain(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：为会话启动指定工作链。

        C1/C2：可选 ``goal_id``——坐席从「目标推荐」入口启动时带上，落执行 context
        （``workflow_executions.context_json``），为漏斗按目标分段/归因留数据地基；
        纯透传，无 goal_id 时行为零变化。
        """
        _require_workflows(request)
        body = await request.json()
        chain_id = str(body.get("chain_id") or "")
        store = _inbox_store(request)
        if not chain_id or store is None:
            return {"ok": False, "error": tr(request, "err.ws.missing_chain_id")}
        if store.has_running_chain(conversation_id, chain_id):
            return {"ok": False, "error": tr(request, "err.ws.chain_already_running")}
        ctx: Dict[str, Any] = {"agent": request.session.get("username") or ""}
        goal_id = str(body.get("goal_id") or "").strip()[:64]
        if goal_id:
            ctx["goal_id"] = goal_id
        exec_id = store.start_chain_execution(
            chain_id, conversation_id, ctx, schedule_first_step=True,
        )
        _ch = store.get_workflow_chain(chain_id) or {}
        _goal_chain_event(request, conversation_id, "chain_started",
                          str(_ch.get("name") or chain_id))
        return {"ok": True, "exec_id": exec_id, "conversation_id": conversation_id}
