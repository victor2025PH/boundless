# -*- coding: utf-8 -*-
"""小智动作注册表路由（实施58 P1，2026-08-23）——薄包装。

契约（与 shared/assistant 前端 & 未来智能体层钉死）：
  GET  /api/assistant/actions          → {ok, levels, actions:[catalog]}
  POST /api/assistant/act              → 无 token：L0 执行 / L1 回 goto /
                                         L2 回 {need_confirm, token, diff}
                                         带 token：核销后应用 → {applied, undo_id}
  POST /api/assistant/act/undo         → 按快照回写 → {restored}

护栏：assistant.enabled 总闸（与问答同灰度）；坐席角色仅 L0/L1
（allowed_levels_for_role）；每 uid 0.6s 节流；确认 token 单次核销 +
uid 绑定；全部写经 actions.apply_plan（overlay+审计+undo 快照）。
set_reply_delay 应用后对活体 AutosendWorker best-effort 热更
（merged_delay_block → apply_deliver_delay，与 reply-settings 页同链），
hot_applied 如实回传，绝不谎报生效。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import HTTPException, Request

from src.web.web_i18n import tr
from src.assistant import actions as act
from src.web.routes.assistant_routes import _assistant_cfg, _session_user

logger = logging.getLogger(__name__)

_ACT_MIN_INTERVAL_SEC = 0.6


def _nav_whitelist() -> set:
    """nav_schema 提取（多键名防御），失败回落保守核心集。"""
    paths: set = set(act.CORE_NAV_PATHS)
    try:
        from src.web.nav_schema import NAV_ITEMS  # type: ignore

        items = NAV_ITEMS.values() if isinstance(NAV_ITEMS, dict) else NAV_ITEMS
        for it in items:
            if not isinstance(it, dict):
                continue
            for k in ("path", "href", "url", "to"):
                v = it.get(k)
                if isinstance(v, str) and v.startswith("/"):
                    paths.add(v.split("?", 1)[0])
    except Exception:
        logger.debug("nav_schema 提取失败，goto 白名单用核心集", exc_info=True)
    return paths


def register_assistant_action_routes(app, ctx) -> None:
    _api_auth = ctx.api_auth
    config_manager = ctx.config_manager
    telegram_client = getattr(ctx, "telegram_client", None)
    _last_act: Dict[str, float] = {}

    def _cfg() -> Dict[str, Any]:
        c = getattr(config_manager, "config", None)
        return c if isinstance(c, dict) else {}

    def _enabled_or_403(request: Request) -> None:
        if not _assistant_cfg(_cfg()).get("enabled"):
            raise HTTPException(403, tr(request, "asb.err.disabled"))

    def _mobile_guard(request: Request) -> str:
        """手机配对会话逐调用校验（实施58 P4）：被踢/过期/实例重启后注册表
        为空 → 立即 401（页面出重扫提示）。返回审计后缀（''｜'@mobile'）。"""
        msid = str(request.session.get("xz_mobile") or "")
        if not msid:
            return ""
        from src.assistant import pairing

        if not pairing.mobile_ok(msid):
            raise HTTPException(401, tr(request, "asb.pair.revoked"))
        return "@mobile"

    def _throttle(uid: str, request: Request) -> None:
        now = time.time()
        last = _last_act.get(uid, 0.0)
        if now - last < _ACT_MIN_INTERVAL_SEC:
            raise HTTPException(429, tr(request, "asb.err.rate_limited"))
        _last_act[uid] = now
        if len(_last_act) > 500:  # 防长期进程 uid 膨胀
            _last_act.clear()
            _last_act[uid] = now

    def _plan_err(request: Request, plan: Dict[str, Any]) -> HTTPException:
        err = plan.get("error")
        detail = str(plan.get("detail") or "")
        if err == "unknown_action":
            return HTTPException(400, tr(request, "asb.act.unknown"))
        if err == "path_not_allowed":
            return HTTPException(400, tr(request, "asb.act.path_na"))
        return HTTPException(
            400, tr(request, "asb.act.bad_params", detail=detail))

    def _try_hot_apply(plan: Dict[str, Any]) -> bool:
        """set_reply_delay 的活体 worker 热更（best-effort，失败如实报 False）。"""
        if plan.get("action") != "set_reply_delay":
            return bool(plan.get("hot") is True)
        try:
            from src.inbox.reply_pacing_settings import merged_delay_block

            clean = {d["path"]: d["new"] for d in (plan.get("diff") or [])}
            block = merged_delay_block(_cfg(), clean)
            worker = getattr(app.state, "autosend_worker", None)
            if worker is not None and hasattr(worker, "apply_deliver_delay"):
                worker.apply_deliver_delay(block)
                return True
        except Exception:
            logger.debug("deliver_delay 热更失败（按重启口径）", exc_info=True)
        return False

    # ------------------------------------------------------------ catalog
    @app.get("/api/assistant/actions")
    async def api_assistant_actions(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        _uid, _name, role = _session_user(request)
        lang = str(request.query_params.get("lang") or "zh")
        return {"ok": True,
                "levels": list(act.allowed_levels_for_role(role)),
                "actions": act.catalog(role, lang)}

    # ------------------------------------------------------------ act
    @app.post("/api/assistant/act")
    async def api_assistant_act(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        src_tag = _mobile_guard(request)
        uid, uname, role = _session_user(request)
        _throttle(uid, request)
        data = await request.json()
        token = str(data.get("confirm_token") or "").strip()

        # ── 二段：核销确认并应用 ──
        if token:
            plan = act.pop_confirm(token, uid)
            if not plan:
                raise HTTPException(409, tr(request, "asb.act.confirm_expired"))
            res = act.apply_plan(plan, config_manager,
                                 (uname or uid) + src_tag)
            if not res.get("ok"):
                first = (res.get("failed") or [{}])[0]
                raise HTTPException(500, tr(
                    request, "asb.act.apply_failed",
                    detail=str(first.get("msg") or "")))
            hot_applied = _try_hot_apply(plan)
            return {"ok": True, "action": plan.get("action"),
                    "applied": res.get("applied"),
                    "failed": res.get("failed"),
                    "undo_id": res.get("undo_id"),
                    "hot_applied": hot_applied}

        # ── 一段：出计划 ──
        plan = act.plan_action(
            str(data.get("action") or ""),
            data.get("params") if isinstance(data.get("params"), dict) else {},
            _cfg(),
            nav_paths=_nav_whitelist(),
            lang="en" if str(data.get("lang") or "").lower().startswith("en")
            else "zh",
        )
        if not plan.get("ok"):
            raise _plan_err(request, plan)
        if plan["level"] not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))

        if plan["kind"] == "query":
            lang = "en" if str(data.get("lang") or "").lower().startswith(
                "en") else "zh"
            if plan["action"] == "diagnose_autoreply":
                from src.inbox.autoreply_factors import collect_autoreply_health

                return {"ok": True, "action": plan["action"],
                        "result": collect_autoreply_health(_cfg())}
            if plan["action"] == "query_ai_status":
                snap = None
                try:
                    ai = getattr(app.state, "ai_client", None)
                    if ai is not None and hasattr(ai, "degradation_snapshot"):
                        snap = ai.degradation_snapshot()
                except Exception:
                    snap = None
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict":
                                   act.summarize_ai_status(snap, lang)}}
            if plan["action"] == "query_reply_budget":
                n_rows = n_ex = n_rel = 0
                guard = None
                try:
                    from src.inbox.peer_bot_guard import (budget_flags,
                                                          parse_cfg,
                                                          today_key)
                    from src.web.routes.unified_inbox_services import (
                        _inbox_store,
                    )

                    guard = parse_cfg(_cfg())
                    store = _inbox_store(request)
                    rows = []
                    if store is not None and hasattr(
                            store, "list_reply_budget_today"):
                        rows = store.list_reply_budget_today(
                            today_key(), limit=200) or []
                    n_rows = len(rows)
                    for r in rows:
                        fl = budget_flags(r.get("used"), r.get("relieved"),
                                          guard)
                        if fl.get("exhausted") or fl.get("hard_stopped"):
                            n_ex += 1
                        if r.get("relieved"):
                            n_rel += 1
                except Exception:
                    logger.debug("query_reply_budget 读数失败（如实降级）",
                                 exc_info=True)
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict": act.summarize_reply_budget(
                            n_rows, n_ex, n_rel, guard, lang)}}
            if plan["action"] == "query_platform_health":
                dump = None
                try:
                    from src.integrations.platform_session_health import (
                        get_platform_session_health,
                    )

                    dump = get_platform_session_health().dump()
                except Exception:
                    dump = None
                return {"ok": True, "action": plan["action"],
                        "result": {"verdict":
                                   act.summarize_platform_sessions(dump,
                                                                   lang)}}
            raise HTTPException(400, tr(request, "asb.act.unknown"))

        if plan["kind"] == "nav":
            return {"ok": True, "action": plan["action"],
                    "goto": plan.get("goto")}

        # L2：出确认卡素材（token 绑定 uid，单次核销）
        return {"ok": True, "action": plan["action"], "need_confirm": True,
                "level": plan["level"], "hot": plan.get("hot"),
                "diff": plan.get("diff"),
                "token": act.issue_confirm(plan, uid),
                "ttl_sec": int(act.CONFIRM_TTL_SEC)}

    # ------------------------------------------------------------ agent plan
    @app.post("/api/assistant/agent/plan")
    async def api_assistant_agent_plan(request: Request):
        """「替我做」规划（实施58 P2）：一句话目标 → LLM 严格 JSON 计划 →
        逐步 plan_action 结构性复验（未知/坏参/越权全丢弃）。**零副作用**：
        执行由前端逐步转投 /api/assistant/act（确认/撤销/审计在那层）。
        LLM 走主链 generate_reply（自带 key 池/本地兜底/熔断）。"""
        _api_auth(request)
        _enabled_or_403(request)
        _mobile_guard(request)
        acfg = _assistant_cfg(_cfg()).get("agent")
        acfg = acfg if isinstance(acfg, dict) else {}
        if not acfg.get("enabled"):
            raise HTTPException(403, tr(request, "asb.agent.disabled"))
        uid, _uname, role = _session_user(request)
        _throttle(uid, request)
        data = await request.json()
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise HTTPException(400, tr(request, "asb.agent.goal_empty"))
        lang = "en" if str(data.get("lang") or "").lower().startswith("en") \
            else "zh"
        page = str(data.get("page") or "")[:120]

        # ── 流程直通（实施58 P5）：确定性触发在 LLM 之前——「我要登陆飞机」
        #    不需要大模型来懂；命中即回完整流程规格（前端模式运行器接管）。
        from src.assistant import flows as fl

        fid = fl.detect_flow_intent(goal)
        if fid:
            if not fl.flow_allowed_for_role(fid, role):
                raise HTTPException(403, tr(request, "asb.act.forbidden"))
            spec = fl.client_spec(fid, lang)
            if spec:
                return {"ok": True, "goal": goal, "plan_ok": True,
                        "say": (spec.get("texts") or {}).get("say", ""),
                        "steps": [], "dropped": [], "flow": spec}

        from src.assistant import agent_planner as planner

        try:
            from src.web.web_context import resolve_skill_manager

            sm = resolve_skill_manager(telegram_client, app)
        except Exception:
            sm = None
        if sm is None or not hasattr(sm, "ai_client"):
            raise HTTPException(503, tr(request, "asb.agent.llm_down"))

        max_steps = max(1, min(int(acfg.get("max_steps", 5) or 5), 8))
        nav = _nav_whitelist()
        prompt = planner.build_planner_prompt(
            goal, role, sorted(nav), lang=lang, page=page,
            max_steps=max_steps, config=_cfg())
        try:
            raw = str(await sm.ai_client.generate_reply(
                user_message=prompt,
                context={"current_intent": "assistant_agent_plan",
                         "kb_context": ""},
                strategy_overrides={"temperature": 0.1, "max_tokens": 800},
            ) or "")
        except Exception:
            logger.warning("agent 规划 LLM 调用失败", exc_info=True)
            raise HTTPException(503, tr(request, "asb.agent.llm_down"))
        verdict = planner.validate_plan(
            planner.parse_plan_json(raw), _cfg(), role,
            nav_paths=nav, max_steps=max_steps, lang=lang)
        if not verdict.get("ok") and verdict.get("reason") == "parse_failed":
            raise HTTPException(502, tr(request, "asb.agent.plan_failed"))
        return {"ok": True, "goal": goal, "say": verdict.get("say"),
                "plan_ok": bool(verdict.get("ok")),
                "steps": verdict.get("steps"),
                "dropped": verdict.get("dropped"),
                "ask": verdict.get("ask") or ""}

    # ------------------------------------------------------------ flows
    @app.get("/api/assistant/flows")
    async def api_assistant_flows(request: Request):
        """流程目录（实施58 P5）：角色过滤后的可用业务流程清单。"""
        _api_auth(request)
        _enabled_or_403(request)
        _mobile_guard(request)
        _uid, _uname, role = _session_user(request)
        lang = str(request.query_params.get("lang") or "zh")
        from src.assistant import flows as fl

        return {"ok": True, "flows": fl.catalog(role, lang)}

    # ------------------------------------------------------------ history
    @app.get("/api/assistant/act/history")
    async def api_assistant_act_history(request: Request):
        """「小智做过什么」（P1 任务历史+撤销中心）：审计尾部 N 条 +
        逐条标注是否仍可撤销（undo 快照 24h 在册）。桌面/手机同一端点。"""
        _api_auth(request)
        _enabled_or_403(request)
        _mobile_guard(request)
        _uid, _uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        lang = str(request.query_params.get("lang") or "zh")
        zh = not lang.lower().startswith("en")
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 30),
                               100))
        except Exception:
            limit = 30
        items = []
        for rec in act.read_history(config_manager, limit=limit):
            aid = str(rec.get("action") or "")
            spec = act.ACTIONS.get(aid) or {}
            path = str(rec.get("path") or "")
            label, _f = act.describe_path(path, lang)
            op = str(rec.get("op") or "")
            if op == "undo":
                old_h, new_h = "", act.humanize_value(
                    path, rec.get("restored"), lang)
            else:
                old_h = act.humanize_value(path, rec.get("old"), lang)
                new_h = act.humanize_value(path, rec.get("new"), lang)
            undo_id = str(rec.get("undo_id") or "")
            items.append({
                "ts": rec.get("ts"),
                "op": op,
                "actor": str(rec.get("actor") or ""),
                "action": aid,
                "action_label": str(
                    spec.get("label_zh" if zh else "label_en") or aid),
                "label": label,
                "old_h": old_h,
                "new_h": new_h,
                "undo_id": undo_id,
                "undoable": op == "apply" and act.has_undo(undo_id),
            })
        return {"ok": True, "items": items}

    # ------------------------------------------------------------ undo
    @app.post("/api/assistant/act/undo")
    async def api_assistant_act_undo(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        src_tag = _mobile_guard(request)
        uid, uname, role = _session_user(request)
        if "L2" not in act.allowed_levels_for_role(role):
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        data = await request.json()
        res = act.apply_undo(str(data.get("undo_id") or ""),
                             config_manager, (uname or uid) + src_tag)
        if res is None:
            raise HTTPException(404, tr(request, "asb.act.undo_missing"))
        return {"ok": bool(res.get("ok")), "restored": res.get("restored"),
                "failed": res.get("failed"), "action": res.get("action")}
