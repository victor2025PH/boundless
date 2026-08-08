"""每日自动学习路由 — /api/learner/*（Phase E1 批 5J）。

从 admin.py 抽出，复用 AdminRouteContext。_get_learner 缓存在 app.state._daily_learner，
与 ai-studio summary 等其它读取点共享同一实例。

2026-08-02：AI 客户端改回落链解析（telegram 协议客户端下线的实例此前整族假空），
学习器降级为"无 AI 也能审"（stats/drafts/审核纯 DB），仅 run/feed 需要 AI。
新增 POST /api/learner/feed（手动喂料）。

端点：
  GET  /api/learner/stats           POST /api/learner/run
  GET  /api/learner/drafts          GET  /api/learner/drafts/{draft_id}
  PUT  /api/learner/drafts/{draft_id}
  POST /api/learner/drafts/{draft_id}/approve   POST /api/learner/drafts/{draft_id}/reject
  POST /api/learner/drafts/approve-all          POST /api/learner/drafts/batch-action
  POST /api/learner/drafts/{draft_id}/recheck-dup
  POST /api/learner/feed
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request

from src.utils.daily_learner import DailyLearner, resolve_learner_ai
from src.utils.domain_policy import effective_domain_name
from src.web.web_i18n import tr


def register_learner_routes(app, ctx):
    config_manager = ctx.config_manager
    _kb_store = ctx.kb_store
    telegram_client = ctx.telegram_client
    audit_store = ctx.audit_store
    _api_auth = ctx.api_auth
    _kb_db_path = Path(config_manager.config_path).parent / "knowledge_base.db"

    def _get_learner() -> Optional[DailyLearner]:
        """取共享学习器。AI 缺席不阻塞构造（审核链纯 DB）；AI 恢复后晚绑定。"""
        learner = getattr(app.state, "_daily_learner", None)
        ai = resolve_learner_ai(app, telegram_client)
        if learner is None:
            if _kb_store is None:
                return None
            learner = DailyLearner(_kb_store, ai, db_path=_kb_db_path)
            app.state._daily_learner = learner
        elif ai is not None and not learner.ai_ready:
            learner.attach_ai(ai)
        return learner

    def _learn_params():
        """域上下文 + 未命中门槛（config kb_learner.min_miss_count，默认 2）。"""
        cfg_obj = config_manager.config if hasattr(config_manager, 'config') else {}
        cfg_obj = cfg_obj if isinstance(cfg_obj, dict) else {}
        domain_name = effective_domain_name(cfg_obj)
        domain_ctx = f"当前行业: {domain_name}" if domain_name else ""
        lcfg = cfg_obj.get("kb_learner") or {}
        try:
            mmc = int(lcfg.get("min_miss_count", 2))
        except (TypeError, ValueError):
            mmc = 2
        return domain_ctx, max(1, mmc)

    @app.get("/api/learner/stats")
    async def api_learner_stats(request: Request, _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            # 旧前端兼容：零值 + error 键；新前端认 available/reason
            return {"available": False, "reason": "kb_unavailable",
                    "ai_ready": False, "error": "kb store not available",
                    "pending": 0, "approved": 0, "rejected": 0, "dup_flagged": 0}
        out = learner.stats()
        out["available"] = True
        out["ai_ready"] = learner.ai_ready
        return out

    @app.post("/api/learner/run")
    async def api_learner_run(request: Request, _=Depends(_api_auth)):
        """手动触发一次学习"""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, tr(request, "err.learner.kb_unavailable"))
        if not learner.ai_ready:
            raise HTTPException(503, tr(request, "err.learner.ai_unavailable"))
        domain_ctx, mmc = _learn_params()
        result = await learner.run_daily_learn(
            domain_context=domain_ctx, min_miss_count=mmc, source="manual")
        actor = request.session.get("username", "system")
        if audit_store:
            audit_store.log(actor, "learner_run", json.dumps(result))
        return result

    @app.post("/api/learner/feed")
    async def api_learner_feed(request: Request, _=Depends(_api_auth)):
        """手动喂料：运营把没答好的问题直接入队，AI 可用时当场生成草稿。"""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, tr(request, "err.learner.kb_unavailable"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        query = str((body or {}).get("query", "")).strip()
        if len(query) < 2:
            raise HTTPException(400, tr(request, "err.learner.feed_query_required"))
        if len(query) > 200:
            raise HTTPException(400, tr(request, "err.learner.feed_query_too_long"))
        domain_ctx, mmc = _learn_params()
        result = await learner.feed_and_learn(
            query, domain_context=domain_ctx, min_miss_count=mmc)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "learner_feed",
                            f"{query[:80]} -> {json.dumps(result, ensure_ascii=False)}")
        return {"ok": True, **result}

    @app.get("/api/learner/drafts")
    async def api_learner_drafts(request: Request, _=Depends(_api_auth),
                                 status: str = Query("pending"),
                                 sort: str = Query("priority")):
        learner = _get_learner()
        if not learner:
            return {"drafts": []}
        return {"drafts": learner.list_drafts(status=status, sort=sort)}

    @app.get("/api/learner/drafts/{draft_id}")
    async def api_learner_draft_detail(request: Request, draft_id: str,
                                       _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        draft = learner.get_draft(draft_id)
        if not draft:
            raise HTTPException(404, "draft not found")
        return draft

    @app.put("/api/learner/drafts/{draft_id}")
    async def api_learner_draft_update(request: Request, draft_id: str,
                                        _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        body = await request.json()
        learner.update_draft(draft_id, body)
        return {"ok": True}

    @app.post("/api/learner/drafts/{draft_id}/approve")
    async def api_learner_draft_approve(request: Request, draft_id: str,
                                         _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        entry_id = learner.approve_draft(draft_id, operator=actor)
        if not entry_id:
            raise HTTPException(400, "draft cannot be approved")
        if audit_store:
            audit_store.log(actor, "learner_approve", f"{draft_id} -> {entry_id}")
        return {"ok": True, "entry_id": entry_id}

    @app.post("/api/learner/drafts/{draft_id}/reject")
    async def api_learner_draft_reject(request: Request, draft_id: str,
                                        _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        learner.reject_draft(draft_id, operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_reject", draft_id)
        return {"ok": True}

    @app.post("/api/learner/drafts/approve-all")
    async def api_learner_approve_all(request: Request, _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        count = learner.approve_all_pending(operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_approve_all", str(count))
        return {"ok": True, "approved": count}

    @app.post("/api/learner/drafts/batch-action")
    async def api_learner_batch_action(request: Request, _=Depends(_api_auth)):
        """A2: Batch approve/reject selected drafts."""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        body = await request.json()
        ids = body.get("ids", [])
        action = body.get("action", "")
        if not ids or action not in ("approve", "reject"):
            raise HTTPException(400, "ids[] and action (approve|reject) required")
        actor = request.session.get("username", "web_admin")
        result = learner.batch_action(ids, action, operator=actor)
        if audit_store:
            audit_store.log(actor, f"learner_batch_{action}",
                            f"{len(ids)} ids -> {result}")
        return {"ok": True, **result}

    # ── A3: Duplicate recheck ─────────────────────────────────
    @app.post("/api/learner/drafts/{draft_id}/recheck-dup")
    async def api_learner_recheck_dup(request: Request, draft_id: str,
                                       _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        dup = learner.recheck_duplicate(draft_id)
        return {"ok": True, "dup": dup}
