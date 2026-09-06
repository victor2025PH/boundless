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
  POST /api/learner/drafts/translate
  POST /api/learner/feed

2026-09-06 L-4 F（#201 止血）：approve-all 只通过已选（body.ids）且须 confirm=1、每批
≤ APPROVE_BATCH_LIMIT；私事条目通过 → 写该客户 AI 记忆（memory_writer 在这里注入，
它才拿得到 skill_manager）；translate 端点复用翻译服务缓存给外语原文补中文译文。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request

from src.utils.daily_learner import (APPROVE_BATCH_LIMIT, DailyLearner,
                                     LearnerApproveError, resolve_learner_ai)
from src.utils.domain_policy import effective_domain_name
from src.web.web_i18n import tr


def register_learner_routes(app, ctx):
    config_manager = ctx.config_manager
    _kb_store = ctx.kb_store
    telegram_client = ctx.telegram_client
    audit_store = ctx.audit_store
    _api_auth = ctx.api_auth
    _kb_db_path = Path(config_manager.config_path).parent / "knowledge_base.db"

    def _memory_writer(conversation_id: str, content: str, quote: str):
        """私事条目 → 该客户 AI 记忆（与工作台「跨平台档案」写事实同一条路）。

        记忆键由 skill_manager._episodic_storage_key 算（与抽取链同源，否则写进去
        召回不到）；add_fact 返回 None＝同样事实已在（不算失败）。取不到 skill_manager /
        记忆存储 → None（学习器转 memory_unavailable）。
        """
        try:
            from src.inbox.peer_delete_purge import split_conversation_id
            from src.web.web_context import resolve_skill_manager
            sm = resolve_skill_manager(telegram_client, app)
            estore = getattr(sm, "_episodic_store", None) if sm else None
            if sm is None or estore is None:
                return None
            plat, acct, chat = split_conversation_id(conversation_id)
            if not chat:
                return None
            key = sm._episodic_storage_key(chat, "", plat, account_id=acct)
            rid = estore.add_fact(key, str(content or "")[:500], category="learner",
                                  source="user_stated", source_quote=str(quote or ""))
            return {"key": key, "row_id": rid}
        except Exception:
            return None

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
        if getattr(learner, "_memory_writer", None) is None:
            learner.set_memory_writer(_memory_writer)
        return learner

    def _approve_error(request: Request, e: LearnerApproveError) -> HTTPException:
        key = {
            "private_no_customer": "err.learner.private_no_customer",
            "memory_unavailable": "err.learner.memory_unavailable",
        }.get(e.reason, "err.learner.memory_write_failed")
        return HTTPException(400, tr(request, key))

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
    async def api_learner_stats(request: Request, _=Depends(_api_auth),
                                effect: int = Query(0)):
        learner = _get_learner()
        if not learner:
            # 旧前端兼容：零值 + error 键；新前端认 available/reason
            return {"available": False, "reason": "kb_unavailable",
                    "ai_ready": False, "error": "kb store not available",
                    "pending": 0, "approved": 0, "rejected": 0, "dup_flagged": 0}
        out = learner.stats()
        out["available"] = True
        out["ai_ready"] = learner.ai_ready
        # 融合 P2：效果回访（近 7 天入库条目真实命中数）——只有 learner 页
        # 显式带 ?effect=1 才算（todo-summary/徽标等高频轮询保持轻量 stats）。
        # P3 追加 zero_hit：入库 ≥3 天零命中的淘汰建议（同 gated 路径）。
        if effect:
            try:
                eff = learner.effect_report()
                eff["zero_hit"] = learner.retirement_candidates()
                out["effect_7d"] = eff
            except Exception:
                pass
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
        # 融合 P2 溯源：可选来源锚点（case:CASE-xxx / conv:会话id），随草稿落库
        source_ref = str((body or {}).get("source_ref", "")).strip()[:120]
        domain_ctx, mmc = _learn_params()
        result = await learner.feed_and_learn(
            query, domain_context=domain_ctx, min_miss_count=mmc,
            source_ref=source_ref)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "learner_feed",
                            f"{query[:80]}"
                            + (f" [{source_ref}]" if source_ref else "")
                            + f" -> {json.dumps(result, ensure_ascii=False)}")
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
        try:
            entry_id = learner.approve_draft(draft_id, operator=actor)
        except LearnerApproveError as e:
            raise _approve_error(request, e)
        if not entry_id:
            raise HTTPException(400, "draft cannot be approved")
        if audit_store:
            audit_store.log(actor, "learner_approve", f"{draft_id} -> {entry_id}")
        target = "memory" if str(entry_id).startswith("mem:") else "kb"
        return {"ok": True, "entry_id": entry_id, "target": target}

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
        """「通过已选（N）」（L-4 F #201）：body ``{ids:[…], confirm:1}``。

        - 无 ``confirm=1`` → 428（前端二次确认弹层列出将入库条目后才带 confirm 重发）；
        - ``ids`` 为空 → 400；> APPROVE_BATCH_LIMIT → 400（一批最多 20 条）。
        旧的「不传 ids＝整队列一键入库」不再存在。
        """
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        ids = [str(i) for i in (body.get("ids") or []) if str(i).strip()]
        if not ids:
            raise HTTPException(400, tr(request, "err.learner.ids_required"))
        if len(ids) > APPROVE_BATCH_LIMIT:
            raise HTTPException(400, tr(request, "err.learner.batch_limit",
                                        n=APPROVE_BATCH_LIMIT))
        if str(body.get("confirm") or "") not in ("1", "true", "True"):
            raise HTTPException(428, tr(request, "err.learner.confirm_required"))
        actor = request.session.get("username", "web_admin")
        result = learner.batch_action(ids, "approve", operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_approve_all",
                            f"{len(ids)} selected -> {json.dumps(result, ensure_ascii=False)}")
        return {"ok": True, "approved": result.get("approved", 0), **result}

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

    # ── L-4 F（#201）：外语原文 → 中文译文（复用入站翻译服务的缓存/翻译记忆） ──
    @app.post("/api/learner/drafts/translate")
    async def api_learner_translate(request: Request, _=Depends(_api_auth)):
        """Body ``{ids:[…]}``（≤50）→ ``{translations:{id: zh}}``。

        已有 ``query_zh`` 的直接回；中文原文（identity）不落库不回；引擎失败静默跳过
        （best-effort：译文缺席不阻塞审核）。译文写回 ``kb_drafts.query_zh`` 下次列表直出。
        """
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        try:
            body = await request.json()
        except Exception:
            body = {}
        ids = [str(i) for i in ((body or {}).get("ids") or []) if str(i).strip()][:50]
        out = {}
        if not ids:
            return {"ok": True, "translations": out}
        svc = None
        try:
            from src.web.routes.unified_inbox_services import _get_translation_service
            svc = _get_translation_service(request)
        except Exception:
            svc = None
        for did in ids:
            d = learner.get_draft(did)
            if not d:
                continue
            if d.get("query_zh"):
                out[did] = d["query_zh"]
                continue
            if svc is None:
                continue
            try:
                res = await svc.translate(str(d.get("query") or ""), target_lang="zh",
                                          style="chat")
            except Exception:
                continue
            if not getattr(res, "ok", False):
                continue
            if str(getattr(res, "provider", "") or "") == "identity":
                continue  # 本就是中文，无需译文
            zh = str(getattr(res, "translated_text", "") or "").strip()
            if zh and zh != str(d.get("query") or "").strip():
                learner.set_translation(did, zh)
                out[did] = zh
        return {"ok": True, "translations": out}
