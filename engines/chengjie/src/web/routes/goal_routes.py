"""营销目标（marketing goals）后台 API。

端点（挂 ``/api/goals*``；静态路径先于 ``{goal_id}`` 注册防吞路由）：
- ``GET  /api/goals/templates``        —— 模板库（建目标表单用；含里程碑/参数 schema）
- ``GET  /api/goals/for-conversation`` —— 会话活跃目标视图（右栏卡；settle-on-read）
- ``GET  /api/goals/report``           —— 结果闭环聚合（P2：模板×终态成功率/天数/反馈）
- ``POST /api/goals/batch``            —— 批量 campaign（P2：多会话同款目标，逐条护栏）
- ``GET  /api/goals``                  —— 目标列表 + 状态聚合（看板；逐条 settle）
- ``POST /api/goals``                  —— 建目标（viewer 只读拦截；每会话活跃数上限）
- ``GET  /api/goals/{goal_id}``        —— 详情（视图 + 拍时间线 + 事件台账）
- ``POST /api/goals/{goal_id}/update`` —— 改字段（title/autonomy/priority/deadline/params）
- ``POST /api/goals/{goal_id}/status`` —— 生命周期操作（pause/resume/cancel）
- ``POST /api/goals/{goal_id}/beat/feedback`` —— 坐席采纳/驳回今日拍（P2 回流 planner）

口径唯一性：读路径一律经 ``service.refresh_goal``（settle + 当日拍）再出
``service.goal_view``——右栏卡、看板、prompt 注入三个消费面读同一份结算结果。
功能未启用（``companion.goals.enabled=false``）时全部端点 403，不装死也不 500。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.goal_routes")

_ROLE_VIEWER = "viewer"

# list 接口逐条 settle（每条=进程内 DB 读），上限护住最坏情况
_LIST_SETTLE_CAP = 100


def _split_conversation_id(conversation_id: str):
    """``platform:account_id:chat_key`` → 三元组（chat_key 可含冒号，split 限 2 刀）。"""
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) == 3 and all(p.strip() for p in parts[:2]):
        return parts[0].strip(), parts[1].strip(), parts[2].strip()
    return "", "", ""


def register_goal_routes(app, auth_dep, config_manager=None):
    """注册营销目标路由。``config_manager`` 供配置段/库路径解析。"""

    def _cfg_root() -> Dict[str, Any]:
        try:
            cfg = getattr(config_manager, "config", None)
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _config_path():
        return getattr(config_manager, "config_path", None)

    def _svc():
        from src.companion.goals import service as goal_service
        return goal_service

    def _require_enabled(request: Request):
        svc = _svc()
        if not svc.goals_enabled(_cfg_root()):
            raise HTTPException(403, tr(request, "err.goals.disabled"))
        return svc

    def _deny_viewer(request: Request):
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.goals.readonly"))

    def _store(svc):
        return svc.get_configured_store(_cfg_root(), _config_path())

    def _inbox_store():
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            return get_inbox_store()
        except Exception:
            return None

    def _lang(request: Request) -> str:
        try:
            return str(getattr(request.state, "ui_lang", "") or "zh")
        except Exception:
            return "zh"

    def _refreshed_view(svc, store, goal, *, lang: str) -> Dict[str, Any]:
        res = svc.refresh_goal(store, _cfg_root(), goal, inbox_store=_inbox_store())
        return svc.goal_view(
            res["goal"], res.get("action"), res.get("hold"), lang=lang)

    # ── 静态路径（先注册）────────────────────────────────────────────────────
    @app.get("/api/goals/templates")
    async def goals_templates(request: Request, _auth=Depends(auth_dep)):
        _require_enabled(request)
        from src.companion.goals.templates import (
            AUTONOMY_LEVELS,
            GOAL_STATUSES,
            list_templates,
        )
        return {
            "templates": list_templates(),
            "autonomy_levels": list(AUTONOMY_LEVELS),
            "statuses": list(GOAL_STATUSES),
        }

    @app.get("/api/goals/for-conversation")
    async def goals_for_conversation(
        request: Request, conversation_id: str = "", _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        conv = str(conversation_id or "").strip()
        if not conv:
            raise HTTPException(400, tr(request, "err.goals.conversation_required"))
        store = _store(svc)
        platform, account_id, chat_key = _split_conversation_id(conv)
        goal = store.find_active_goal(
            conversation_id=conv, platform=platform, chat_key=chat_key,
            account_id=account_id)
        if goal is None:
            # 无活跃目标 → 给最近一条终态目标（卡片显示「上一个目标的结果」）
            recent = [g for g in store.list_goals(limit=20)
                      if str(g.get("conversation_id") or "") == conv]
            last = recent[0] if recent else None
            return {
                "goal": None,
                "last": svc.goal_view(last, lang=_lang(request)) if last else None,
            }
        return {"goal": _refreshed_view(svc, store, goal, lang=_lang(request)),
                "last": None}

    @app.get("/api/goals/report")
    async def goals_report(
        request: Request, days: int = 30, _auth=Depends(auth_dep)
    ):
        """结果闭环读数面（P2）：窗口期终态聚合（模板×结果、成功率、
        平均达成天数）+ 拍/坐席反馈计数 + 最近终态列表。周审据此调模板/曲线。"""
        svc = _require_enabled(request)
        store = _store(svc)
        import time as _t
        d = max(1, min(int(days or 30), 180))
        report = store.outcome_report(_t.time() - d * 86400.0)
        report["window_days"] = d
        return report

    @app.post("/api/goals/batch")
    async def goals_batch(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        """批量 campaign（P2）：一次给多个会话建同款目标。逐目标沿用单建的
        全部护栏（模板校验/每会话活跃上限），单条失败只记 skipped 不整批中断。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        from src.companion.goals.templates import get_template
        body = payload or {}
        template_id = str(body.get("template") or "").strip()
        tmpl = get_template(template_id)
        if tmpl is None:
            raise HTTPException(400, tr(request, "err.goals.template_unknown"))
        raw_targets = body.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise HTTPException(400, tr(request, "err.goals.batch_targets_required"))

        cfg = svc.resolve_goals_cfg(_cfg_root())
        batch_cfg = cfg.get("batch") or {}
        try:
            max_per_call = int(batch_cfg.get("max_per_call", 50) or 50)
        except (TypeError, ValueError):
            max_per_call = 50
        max_per_call = max(1, min(max_per_call, 200))
        if len(raw_targets) > max_per_call:
            raise HTTPException(400, tr(
                request, "err.goals.batch_too_many", n=max_per_call))

        try:
            cap = int(cfg.get("max_active_per_conversation", 1) or 1)
        except (TypeError, ValueError):
            cap = 1
        from src.companion.goals.store import MAX_ACTIVE_PER_CONVERSATION
        cap = max(1, min(cap, MAX_ACTIVE_PER_CONVERSATION))

        try:
            deadline_days = float(body.get("deadline_days") or 0)
        except (TypeError, ValueError):
            deadline_days = 0.0
        if deadline_days <= 0:
            deadline_days = float(tmpl.get("default_days") or 14)
        deadline_days = max(1.0, min(deadline_days, 180.0))
        autonomy = str(body.get("autonomy") or "suggest")
        try:
            priority = int(body.get("priority") or 1)
        except (TypeError, ValueError):
            priority = 1
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        title = str(body.get("title") or "")[:120]
        try:
            actor = str(request.session.get("user", "") or "")[:52]
        except Exception:
            actor = ""

        store = _store(svc)
        created = 0
        skipped = []
        seen = set()
        for raw in raw_targets:
            if isinstance(raw, str):
                conv = raw.strip()
                platform, account_id, chat_key = _split_conversation_id(conv)
            elif isinstance(raw, dict):
                platform = str(raw.get("platform") or "").strip()
                account_id = str(raw.get("account_id") or "").strip()
                chat_key = str(raw.get("chat_key") or "").strip()
                conv = str(raw.get("conversation_id") or "").strip()
                if conv and not (platform and chat_key):
                    platform, account_id, chat_key = _split_conversation_id(conv)
                elif not conv and platform and account_id and chat_key:
                    conv = f"{platform}:{account_id}:{chat_key}"
            else:
                skipped.append({"target": str(raw)[:80], "reason": "bad_target"})
                continue
            if not conv or not platform or not chat_key:
                skipped.append({"target": conv or str(raw)[:80],
                                "reason": "bad_target"})
                continue
            if conv in seen:                     # 同批重复 → 只建一条
                skipped.append({"target": conv, "reason": "duplicate"})
                continue
            seen.add(conv)
            if store.count_active_for_conversation(conv) >= cap:
                skipped.append({"target": conv, "reason": "active_limit"})
                continue
            goal = store.create_goal(
                conversation_id=conv, platform=platform, account_id=account_id,
                chat_key=chat_key, template=template_id, title=title,
                params=params, autonomy=autonomy, priority=priority,
                deadline_days=deadline_days,
                created_by=(actor + ":batch") if actor else "batch",
            )
            if goal is None:
                skipped.append({"target": conv, "reason": "create_failed"})
                continue
            created += 1
            try:
                from src.companion.goals.stats import get_goal_stats
                get_goal_stats().record_created()
            except Exception:
                pass
        return {"ok": True, "requested": len(raw_targets),
                "created": created, "skipped": skipped}

    @app.get("/api/goals")
    async def goals_list(
        request: Request,
        status: str = "",
        platform: str = "",
        account_id: str = "",
        limit: int = 50,
        _auth=Depends(auth_dep),
    ):
        svc = _require_enabled(request)
        store = _store(svc)
        lang = _lang(request)
        goals = store.list_goals(
            status=status, platform=platform, account_id=account_id,
            limit=min(int(limit or 50), _LIST_SETTLE_CAP))
        views = []
        for g in goals:
            if str(g.get("status")) == "active":
                views.append(_refreshed_view(svc, store, g, lang=lang))
            else:
                views.append(svc.goal_view(g, lang=lang))
        return {"goals": views, "summary": store.summary()}

    @app.post("/api/goals")
    async def goals_create(
        request: Request, payload: Dict[str, Any], _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        _deny_viewer(request)
        from src.companion.goals.templates import get_template
        body = payload or {}
        template_id = str(body.get("template") or "").strip()
        if get_template(template_id) is None:
            raise HTTPException(400, tr(request, "err.goals.template_unknown"))

        conv = str(body.get("conversation_id") or "").strip()
        platform = str(body.get("platform") or "").strip()
        account_id = str(body.get("account_id") or "").strip()
        chat_key = str(body.get("chat_key") or "").strip()
        if conv and not (platform and chat_key):
            platform, account_id, chat_key = _split_conversation_id(conv)
        elif not conv and platform and account_id and chat_key:
            conv = f"{platform}:{account_id}:{chat_key}"
        if not conv or not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.goals.conversation_required"))

        store = _store(svc)
        cfg = svc.resolve_goals_cfg(_cfg_root())
        try:
            cap = int(cfg.get("max_active_per_conversation", 1) or 1)
        except (TypeError, ValueError):
            cap = 1
        from src.companion.goals.store import MAX_ACTIVE_PER_CONVERSATION
        cap = max(1, min(cap, MAX_ACTIVE_PER_CONVERSATION))
        if store.count_active_for_conversation(conv) >= cap:
            raise HTTPException(409, tr(request, "err.goals.active_limit", n=cap))

        try:
            deadline_days = float(body.get("deadline_days") or 0)
        except (TypeError, ValueError):
            deadline_days = 0.0
        if deadline_days <= 0:
            tmpl = get_template(template_id) or {}
            deadline_days = float(tmpl.get("default_days") or 14)
        deadline_days = max(1.0, min(deadline_days, 180.0))

        try:
            actor = str(request.session.get("user", "") or "")[:60]
        except Exception:
            actor = ""
        goal = store.create_goal(
            conversation_id=conv, platform=platform, account_id=account_id,
            chat_key=chat_key, template=template_id,
            title=str(body.get("title") or "")[:120],
            params=body.get("params") if isinstance(body.get("params"), dict) else {},
            autonomy=str(body.get("autonomy") or "suggest"),
            priority=int(body.get("priority") or 1),
            deadline_days=deadline_days,
            created_by=actor,
        )
        if goal is None:
            raise HTTPException(500, tr(request, "err.goals.create_failed"))
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_created()
        except Exception:
            pass
        return {"ok": True,
                "goal": _refreshed_view(svc, store, goal, lang=_lang(request))}

    # ── 动态路径（后注册）────────────────────────────────────────────────────
    @app.get("/api/goals/{goal_id}")
    async def goals_detail(
        request: Request, goal_id: str, _auth=Depends(auth_dep)
    ):
        svc = _require_enabled(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        lang = _lang(request)
        view = (_refreshed_view(svc, store, goal, lang=lang)
                if str(goal.get("status")) == "active"
                else svc.goal_view(goal, lang=lang))
        return {
            "goal": view,
            "actions": store.list_actions(goal_id, limit=30),
            "events": store.list_events(goal_id, limit=50),
        }

    @app.post("/api/goals/{goal_id}/update")
    async def goals_update(
        request: Request, goal_id: str, payload: Dict[str, Any],
        _auth=Depends(auth_dep),
    ):
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        body = payload or {}
        fields: Dict[str, Any] = {}
        if isinstance(body.get("title"), str):
            fields["title"] = body["title"].strip()[:120]
        if body.get("autonomy") is not None:
            fields["autonomy"] = str(body.get("autonomy") or "")
        if body.get("priority") is not None:
            try:
                fields["priority"] = int(body.get("priority"))
            except (TypeError, ValueError):
                pass
        if body.get("deadline_days") is not None:
            try:
                dd = float(body.get("deadline_days"))
                if dd > 0:
                    import time as _t
                    fields["deadline_ts"] = float(
                        goal.get("start_ts") or _t.time()) + min(dd, 180.0) * 86400.0
            except (TypeError, ValueError):
                pass
        if isinstance(body.get("params"), dict):
            merged = dict(goal.get("params") or {})
            merged.update(body["params"])
            fields["params"] = merged
        if not fields:
            raise HTTPException(400, tr(request, "err.goals.nothing_to_update"))
        if not store.update_goal_fields(goal_id, **fields):
            raise HTTPException(500, tr(request, "err.goals.update_failed"))
        store.add_event(goal_id, "updated", ",".join(sorted(fields.keys())))
        goal = store.get_goal(goal_id)
        return {"ok": True,
                "goal": svc.goal_view(goal, lang=_lang(request))}

    @app.post("/api/goals/{goal_id}/beat/feedback")
    async def goals_beat_feedback(
        request: Request, goal_id: str, payload: Dict[str, Any],
        _auth=Depends(auth_dep),
    ):
        """suggest 档坐席闭环（P2）：对「今日拍」采纳/驳回。
        - adopt：正向信号入事件台账（不改拍状态；detail 标记供卡片显示 ✓）。
        - reject：今日拍置 skipped（当天立即停注入/停主动带意图），事件回流
          planner——近窗驳回 1 次力度封顶 soft、≥2 次退避陪伴日。"""
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        if str(goal.get("status")) != "active":
            raise HTTPException(409, tr(request, "err.goals.not_active"))
        body = payload or {}
        verdict = str(body.get("verdict") or "").strip().lower()
        if verdict not in ("adopt", "reject"):
            raise HTTPException(400, tr(request, "err.goals.bad_verdict"))
        from src.companion.goals.planner import day_key
        action = store.get_action(goal_id, day_key())
        if action is None:
            raise HTTPException(409, tr(request, "err.goals.no_beat_today"))
        reason = str(body.get("reason") or "agent").strip()[:120]
        aid = str(action.get("action_id") or "")
        if verdict == "reject":
            store.mark_action(aid, "skipped", detail=f"rejected:{reason}")
            store.add_event(goal_id, "beat_rejected", reason)
        else:
            # 采纳保持拍状态（planned 后续照常 consumed），只落 detail + 事件
            store.mark_action(
                aid, str(action.get("status") or "planned"), detail="adopted")
            store.add_event(goal_id, "beat_adopted", reason)
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_feedback(verdict)
        except Exception:
            pass
        goal = store.get_goal(goal_id) or goal
        action = store.get_action(goal_id, day_key())
        return {"ok": True,
                "goal": svc.goal_view(goal, action, lang=_lang(request))}

    @app.post("/api/goals/{goal_id}/status")
    async def goals_status(
        request: Request, goal_id: str, payload: Dict[str, Any],
        _auth=Depends(auth_dep),
    ):
        svc = _require_enabled(request)
        _deny_viewer(request)
        store = _store(svc)
        goal = store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, tr(request, "err.goals.not_found"))
        action = str((payload or {}).get("action") or "").strip().lower()
        cur = str(goal.get("status") or "")
        transitions = {
            ("active", "pause"): "paused",
            ("paused", "resume"): "active",
            ("active", "cancel"): "cancelled",
            ("paused", "cancel"): "cancelled",
        }
        new_status = transitions.get((cur, action))
        if new_status is None:
            raise HTTPException(400, tr(request, "err.goals.bad_transition",
                                        status=cur, action=action or "?"))
        import time as _t
        fields: Dict[str, Any] = {"status": new_status}
        if new_status == "cancelled":
            fields["done_at"] = _t.time()
        if not store.update_goal_fields(goal_id, **fields):
            raise HTTPException(500, tr(request, "err.goals.update_failed"))
        store.add_event(goal_id, "status", f"{cur}->{new_status}:manual")
        try:
            from src.companion.goals.stats import get_goal_stats
            if new_status == "cancelled":
                get_goal_stats().record_terminal("cancelled")
            elif new_status == "paused":
                get_goal_stats().record_paused()
        except Exception:
            pass
        goal = store.get_goal(goal_id)
        return {"ok": True,
                "goal": svc.goal_view(goal, lang=_lang(request))}

    logger.info("营销目标路由已注册（/api/goals*）")
