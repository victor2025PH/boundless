"""统一收件箱——坐席工作台联系人/CRM/跟进任务路由域（巨石拆分 slice 12）。

把"Phase 5-5 手动合并/拆分/审核队列 + Phase 6-1 Contact 360 全景 + Phase 6-2 客户列表/CRM
+ 跟进任务"这一内聚子域，从 ``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_contacts_routes(app, *, api_auth, page_auth, templates, config_manager)``，
由主 register 在**原位置**顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下（services/auth/context/helpers 已成模块）或来自 register 参数（page_auth/templates/
config_manager），无回 routes 依赖。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from src.web.routes.unified_inbox_auth import _publish_follow_up, _session_agent
from src.web.routes.unified_inbox_context import _build_contact_timeline
from src.web.routes.unified_inbox_helpers import (
    _EVENT_LABELS,
    _PLATFORM_LABELS,
    _fmt_ts,
    FUNNEL_STAGE_LABELS,
)
from src.web.routes.unified_inbox_services import _contacts_gateway, _contacts_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_workspace_contacts_routes(
    app, *, api_auth, page_auth, templates, config_manager=None,
) -> None:
    """挂载坐席工作台联系人/CRM/跟进任务端点（/api/workspace/contacts*、/contact*、/follow-up*、/my-tasks 等）。"""

    # ── Phase 5-5：坐席手动合并 / 拆分 / 审核队列 ────────────────
    @app.get("/api/workspace/contacts/overview")
    async def api_workspace_contact_overview(
        request: Request,
        platform: str = "",
        account_id: str = "default",
        chat_key: str = "",
    ):
        """当前会话对应 Contact 档案 + 该 Contact 的渠道身份 + 可合并候选。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled"}
        ci = store.get_ci_by_external(platform, account_id, chat_key)
        if ci is None:
            return {"ok": True, "contact": None, "candidates": []}
        overview = gw.contact_overview(ci.contact_id)
        candidates = gw.merge_candidates_for(ci.contact_id)
        return {
            "ok": True,
            "current_ci_id": ci.channel_identity_id,
            "contact": overview,
            "candidates": candidates,
        }

    def _memory_merge_enabled() -> bool:
        """P2：合并联动记忆合流开关（contacts.origin_profile.merge_memory，默认关）。"""
        cfg = getattr(config_manager, "config", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        return bool(((cfg.get("contacts") or {}).get("origin_profile") or {})
                    .get("merge_memory", False))

    def _run_memory_merge(request: Request, contact_id: str) -> Optional[Dict[str, Any]]:
        """合并成功后的记忆合流（fail-soft：任何缺件/异常只损失合流，不影响合并）。"""
        if not _memory_merge_enabled():
            return None
        try:
            from src.contacts.memory_merge_bridge import merge_contact_memory
            from src.web.web_context import resolve_skill_manager
            sm = resolve_skill_manager(
                getattr(request.app.state, "telegram_client", None), request.app)
            return merge_contact_memory(
                contacts_store=_contacts_store(request),
                inbox_store=getattr(request.app.state, "inbox_store", None),
                cpi=getattr(sm, "_cpi", None) if sm else None,
                episodic_store=getattr(sm, "_episodic_store", None) if sm else None,
                contact_id=contact_id,
            )
        except Exception:
            logger.debug("memory merge bridge failed", exc_info=True)
            return None

    @app.post("/api/workspace/contacts/merge")
    async def api_workspace_contact_merge(request: Request, _=Depends(api_auth)):
        body = await request.json()
        ci_id = str(body.get("ci_id") or "").strip()
        target = str(body.get("target_contact_id") or "").strip()
        if not ci_id or not target:
            raise HTTPException(400, tr(request, "err.ws.ci_target_required"))
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        try:
            ok = gw.manual_merge_identity(
                ci_id=ci_id, target_contact_id=target, operator=agent["agent_id"],
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        out = {"ok": bool(ok), "merged": bool(ok), "target_contact_id": target}
        if ok:
            mm = _run_memory_merge(request, target)
            if mm is not None:
                out["memory_merge"] = mm
        return out

    @app.post("/api/workspace/contacts/merge-contact")
    async def api_workspace_contact_merge_contact(request: Request, _=Depends(api_auth)):
        """contact 级合并：把 source 的所有渠道身份并入 target。

        P2：合并成功且 ``contacts.origin_profile.merge_memory`` 开 → 联动把 target
        名下全部会话身份的情景记忆合流到同一 canonical（AI 不再「合并了人还失忆」）。
        """
        body = await request.json()
        source = str(body.get("source_contact_id") or "").strip()
        target = str(body.get("target_contact_id") or "").strip()
        if not source or not target:
            raise HTTPException(400, tr(request, "err.ws.source_target_required"))
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        ok = gw.merge_contacts(
            source_contact_id=source, target_contact_id=target, operator=agent["agent_id"],
        )
        out = {"ok": bool(ok), "merged": bool(ok), "target_contact_id": target}
        if ok:
            mm = _run_memory_merge(request, target)
            if mm is not None:
                out["memory_merge"] = mm
        return out

    @app.post("/api/workspace/contacts/split")
    async def api_workspace_contact_split(request: Request, _=Depends(api_auth)):
        body = await request.json()
        ci_id = str(body.get("ci_id") or "").strip()
        if not ci_id:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="ci_id"))
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        new_cid = gw.split_identity(ci_id=ci_id, operator=agent["agent_id"])
        if not new_cid:
            return {"ok": False, "error": "nothing_to_split"}
        out: Dict[str, Any] = {"ok": True, "new_contact_id": new_cid}
        # P2 拆分对偶：被拆出身份 unlink 回独立 canonical（只影响未来读写；
        # 已混合历史无法归属拆分——与 /api/identity/unlink 同语义）。
        if _memory_merge_enabled():
            try:
                from src.contacts.memory_merge_bridge import unlink_identity_memory
                from src.web.web_context import resolve_skill_manager
                sm = resolve_skill_manager(
                    getattr(request.app.state, "telegram_client", None), request.app)
                out["memory_unlink"] = unlink_identity_memory(
                    contacts_store=_contacts_store(request),
                    inbox_store=getattr(request.app.state, "inbox_store", None),
                    cpi=getattr(sm, "_cpi", None) if sm else None,
                    contact_id=new_cid, ci_id=ci_id,
                )
            except Exception:
                logger.debug("memory unlink bridge failed", exc_info=True)
        return out

    @app.get("/api/workspace/merge-reviews")
    async def api_workspace_merge_reviews(request: Request):
        """待人工裁决的合并候选队列（含两侧档案摘要供对比）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled", "reviews": []}
        store = _contacts_store(request)
        out: List[Dict[str, Any]] = []
        for rv in gw.list_pending_merge_reviews():
            cand_ci = store.get_channel_identity(rv["candidate_ci_id"]) if store else None
            cand_overview = (
                gw.contact_overview(cand_ci.contact_id) if cand_ci else None
            )
            out.append({
                **rv,
                "candidate": cand_overview,
                "candidate_channel": cand_ci.channel if cand_ci else "",
                "target": gw.contact_overview(rv["target_contact_id"]),
            })
        return {"ok": True, "reviews": out, "count": len(out)}

    @app.post("/api/workspace/merge-reviews/{review_id}")
    async def api_workspace_merge_review_resolve(
        review_id: str, request: Request, _=Depends(api_auth),
    ):
        body = await request.json()
        action = str(body.get("action") or "").lower()
        if action not in ("approve", "reject"):
            raise HTTPException(400, tr(request, "err.ws.action_approve_reject"))
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        if action == "approve":
            ok = gw.approve_merge_review(review_id, resolved_by=agent["agent_id"])
        else:
            ok = gw.reject_merge_review(review_id, resolved_by=agent["agent_id"])
        return {"ok": bool(ok), "action": action, "review_id": review_id}

    # ── Phase 6-1：Contact 360 全景视图 ─────────────────────────
    @app.get("/api/workspace/contacts/search")
    async def api_workspace_contacts_search(request: Request, q: str = "", limit: int = 20):
        """按 名称 / contact_id / 渠道 external_id 搜索 Contact（手动合并目标选择）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        limit = max(1, min(50, int(limit or 20)))
        contacts, total = store.search_contacts(str(q or "").strip(), limit=limit)
        out = []
        for c in contacts:
            ov = gw.contact_overview(c.contact_id)
            if ov:
                out.append(ov)
        return {"ok": True, "contacts": out, "total": total}

    @app.get("/api/workspace/contact/{contact_id}")
    async def api_workspace_contact_detail(
        contact_id: str, request: Request, msg_limit: int = 60, before_ts: float = 0.0,
    ):
        """Contact 360：聚合档案 + 跨渠道消息时间线 + 事件历史 + 合并候选。

        before_ts>0：分页加载更早消息（仅返回 timeline，前端拼接）。
        """
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled"}
        overview = gw.contact_overview(contact_id)
        if overview is None:
            raise HTTPException(404, tr(request, "err.ws.contact_not_found"))
        msg_limit = max(10, min(200, int(msg_limit or 60)))
        cursor = float(before_ts) if before_ts and before_ts > 0 else None
        timeline = _build_contact_timeline(
            request, overview.get("identities") or [], msg_limit, before_ts=cursor,
        )
        # 翻页请求：只回时间线 + 下一页游标
        next_cursor = timeline[0]["ts"] if (len(timeline) >= msg_limit and timeline) else 0
        if cursor is not None:
            return {"ok": True, "timeline": timeline, "next_cursor": next_cursor,
                    "has_more": bool(next_cursor)}
        journey = store.get_journey_by_contact(contact_id)
        events: List[Dict[str, Any]] = []
        if journey is not None:
            for e in store.list_events(journey.journey_id, limit=40):
                et = e.get("event_type") or e.get("type") or ""
                events.append({
                    "event_type": et,
                    "label": _EVENT_LABELS.get(et, et),
                    "ts": e.get("ts") or 0,
                    "payload": e.get("payload") or {},
                })
        candidates = gw.merge_candidates_for(contact_id)
        return {
            "ok": True,
            "contact": overview,
            "timeline": timeline,
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
            "events": events,
            "candidates": candidates,
        }

    @app.get("/api/workspace/contact/{contact_id}/origin")
    async def api_workspace_contact_origin_get(
        contact_id: str, request: Request,
    ):
        """Contact 360 侧栏：跨平台档案 + 导入批次 + AI 注入块预览（P3 2026-08-18）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        if store.get_contact(contact_id) is None:
            raise HTTPException(404, tr(request, "err.ws.contact_not_found"))
        enabled = bool(_origin_cfg().get("enabled", False))
        idents = store.list_channel_identities_of(contact_id)
        platform = ""
        if idents:
            platform = str(getattr(idents[0], "channel", "") or "")
        return {"ok": True, "enabled": enabled, "contact_id": contact_id,
                **_origin_payload(store, contact_id, platform)}

    @app.post("/api/workspace/contact/{contact_id}/origin")
    async def api_workspace_contact_origin_save(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """Contact 360 侧栏写档案（客户级，无需会话三元组）。"""
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        if store.get_contact(contact_id) is None:
            raise HTTPException(404, tr(request, "err.ws.contact_not_found"))
        prof_in = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        if prof_in:
            def _s(key: str, cap: int) -> Optional[str]:
                v = prof_in.get(key)
                return None if v is None else str(v).strip()[:cap]

            prior = prof_in.get("prior_names")
            topics = prof_in.get("topics")
            store.upsert_contact_profile(
                contact_id,
                origin_channel=_s("origin_channel", 24),
                origin_label=_s("origin_label", 80),
                known_since=_s("known_since", 40),
                preferred_name=_s("preferred_name", 40),
                background_note=_s("background_note", 400),
                prior_names=(
                    {str(k)[:24]: str(v)[:40] for k, v in list(prior.items())[:8]}
                    if isinstance(prior, dict) else None),
                topics=(
                    [str(t).strip()[:40] for t in topics[:8] if str(t).strip()]
                    if isinstance(topics, list) else None),
                ai_visible=prof_in.get("ai_visible") if "ai_visible" in prof_in else None,
                updated_by=_session_agent(request) or "",
            )
            from src.contacts.origin_context import invalidate_origin_cache
            invalidate_origin_cache()
        idents = store.list_channel_identities_of(contact_id)
        platform = str(getattr(idents[0], "channel", "") or "") if idents else ""
        return {"ok": True, "enabled": bool(_origin_cfg().get("enabled", False)),
                "contact_id": contact_id,
                **_origin_payload(store, contact_id, platform)}

    @app.get("/workspace/contact/{contact_id}", response_class=HTMLResponse)
    async def workspace_contact_page(
        contact_id: str, request: Request, _=Depends(page_auth),
    ):
        ctx: Dict[str, Any] = {
            "contact_id": contact_id,
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "contact360.html", ctx)

    # ── Phase 6-2：客户列表 / CRM 入口 ──────────────────────────
    @app.get("/api/workspace/contacts/list")
    async def api_workspace_contacts_list(
        request: Request,
        q: str = "",
        stage: str = "",
        has_lead: str = "",
        tag: str = "",
        follow_up: str = "",
        limit: int = 30,
        offset: int = 0,
    ):
        """CRM 客户列表：分页 + 阶段/留资/标签/跟进筛选 + 漏斗阶段汇总。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        limit = max(5, min(100, int(limit or 30)))
        offset = max(0, int(offset or 0))
        lead_filter: Optional[bool] = None
        if has_lead in ("1", "true", "yes"):
            lead_filter = True
        elif has_lead in ("0", "false", "no"):
            lead_filter = False
        fu = follow_up if follow_up in ("due", "any") else ""
        rows, total = store.list_contacts_overview(
            q=str(q or "").strip(), stage=str(stage or "").strip(),
            has_lead=lead_filter, tag=str(tag or "").strip(), follow_up=fu,
            limit=limit, offset=offset,
        )
        for r in rows:
            r["funnel_stage_label"] = FUNNEL_STAGE_LABELS.get(
                r.get("funnel_stage") or "", r.get("funnel_stage") or "")
            r["channel_labels"] = [
                _PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])
            ]
        try:
            stage_counts = store.count_journeys_by_stage()
        except Exception:
            stage_counts = {}
        try:
            due_count = store.count_due_follow_ups()
        except Exception:
            due_count = 0
        return {
            "ok": True,
            "contacts": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "stage_counts": stage_counts,
            "stage_labels": FUNNEL_STAGE_LABELS,
            "due_follow_ups": due_count,
        }

    @app.post("/api/workspace/contact/{contact_id}/crm")
    async def api_workspace_contact_crm(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """保存客户 CRM 字段：备注 / 标签 / 跟进时间。未传的字段不改。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        note = body.get("note")
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise HTTPException(400, tr(request, "err.ws.tags_must_be_array"))
        fu = body.get("follow_up_at")
        follow_up_at = None
        if fu is not None:
            try:
                follow_up_at = int(fu)
            except (TypeError, ValueError):
                raise HTTPException(400, tr(request, "err.ws.follow_up_at_int"))
        agent = _session_agent(request)
        return gw.update_contact_crm(
            contact_id, note=note, tags=tags, follow_up_at=follow_up_at,
            operator=agent["agent_id"],
        )

    @app.get("/api/workspace/follow-ups")
    async def api_workspace_follow_ups(request: Request, scope: str = "due", limit: int = 50):
        """待跟进客户列表（scope=due 已到期 / any 全部有跟进）+ 到期计数（全部/本人）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        scope = scope if scope in ("due", "any") else "due"
        rows, total = store.list_contacts_overview(
            follow_up=scope, limit=max(5, min(100, int(limit or 50))),
        )
        for r in rows:
            r["funnel_stage_label"] = FUNNEL_STAGE_LABELS.get(
                r.get("funnel_stage") or "", r.get("funnel_stage") or "")
            r["channel_labels"] = [
                _PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])
            ]
        agent = _session_agent(request)
        return {"ok": True, "contacts": rows, "total": total,
                "due_follow_ups": store.count_due_follow_ups(),
                "due_tasks": store.count_due_tasks(),
                "due_tasks_mine": store.count_due_tasks(assignee=agent["agent_id"])}

    @app.post("/api/workspace/contact/{contact_id}/follow-up")
    async def api_workspace_follow_up_add(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """为客户新增跟进任务：{due_at, note, assignee?}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        try:
            due_at = int(body.get("due_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, tr(request, "err.ws.due_at_int"))
        if due_at <= 0:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="due_at"))
        agent = _session_agent(request)
        assignee = str(body.get("assignee") or "").strip() or agent["agent_id"]
        out = gw.add_follow_up_task(
            contact_id, due_at=due_at, note=str(body.get("note") or ""),
            assignee=assignee, operator=agent["agent_id"],
        )
        if out.get("ok"):
            _publish_follow_up("added", contact_id=contact_id,
                               task_id=out.get("task_id") or "", assignee=assignee)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/done")
    async def api_workspace_follow_up_done(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """标记跟进任务完成。"""
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        out = gw.complete_follow_up_task(task_id, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("done", task_id=task_id)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/assign")
    async def api_workspace_follow_up_assign(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """改派跟进任务给某坐席：{assignee}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        assignee = str(body.get("assignee") or "").strip()
        if not assignee:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="assignee"))
        agent = _session_agent(request)
        out = gw.reassign_follow_up_task(
            task_id, assignee=assignee, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("assigned", contact_id=out.get("contact_id") or "",
                               task_id=task_id, assignee=assignee)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/snooze")
    async def api_workspace_follow_up_snooze(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """延期跟进任务：{days} 顺延 或 {due_at} 直设。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        try:
            days = int(body.get("days") or 0)
            due_at = int(body.get("due_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, tr(request, "err.ws.days_due_at_int"))
        if days <= 0 and due_at <= 0:
            raise HTTPException(400, tr(request, "err.ws.days_or_due_at_required"))
        agent = _session_agent(request)
        out = gw.snooze_follow_up_task(
            task_id, days=days, due_at=due_at, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("snoozed", contact_id=out.get("contact_id") or "",
                               task_id=task_id)
        return out

    @app.get("/api/workspace/my-tasks")
    async def api_workspace_my_tasks(
        request: Request, scope: str = "mine", due: str = "today", limit: int = 100,
    ):
        """跟进待办列表：scope=mine(本人)/all(全部)，due=today/overdue/all。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "tasks": []}
        agent = _session_agent(request)
        assignee = agent["agent_id"] if scope != "all" else None
        now = int(time.time())
        if due == "overdue":
            due_before: Optional[int] = now
        elif due == "all":
            due_before = None
        else:  # today（含逾期 + 今天到期）
            lt = time.localtime(now)
            due_before = int(time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 59, 59, 0, 0, -1)))
        tasks = store.list_open_tasks(
            assignee=assignee, due_before=due_before,
            limit=max(1, min(500, int(limit or 100))))
        for t in tasks:
            t["channel_labels"] = [_PLATFORM_LABELS.get(c, c) for c in (t.get("channels") or [])]
            t["overdue"] = bool(t.get("due_at") and t["due_at"] <= now)
        return {"ok": True, "tasks": tasks,
                "due_tasks": store.count_due_tasks(),
                "due_tasks_mine": store.count_due_tasks(assignee=agent["agent_id"])}

    @app.get("/api/workspace/contact/{contact_id}/tasks")
    async def api_workspace_contact_tasks(
        contact_id: str, request: Request, include_done: int = 0,
    ):
        """某客户的跟进任务（会话内联面板用，轻量）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "tasks": []}
        return {"ok": True,
                "tasks": store.list_follow_up_tasks(
                    contact_id, include_done=bool(include_done))}

    # 巨石拆分 slice 17：CRM 客户列表 CSV 导出（slice 12 遗留的 contacts 域 straggler 归并入位）。
    @app.get("/api/workspace/contacts/export.csv")
    async def api_workspace_contacts_export(
        request: Request,
        q: str = "",
        stage: str = "",
        has_lead: str = "",
        tag: str = "",
        follow_up: str = "",
        limit: int = 5000,
    ):
        """按当前筛选导出客户列表 CSV（最多 limit 行）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.ws.contacts_disabled"))
        lead_filter: Optional[bool] = None
        if has_lead in ("1", "true", "yes"):
            lead_filter = True
        elif has_lead in ("0", "false", "no"):
            lead_filter = False
        fu = follow_up if follow_up in ("due", "any") else ""
        rows, _total = store.list_contacts_overview(
            q=str(q or "").strip(), stage=str(stage or "").strip(),
            has_lead=lead_filter, tag=str(tag or "").strip(), follow_up=fu,
            limit=max(1, min(20000, int(limit or 5000))), offset=0,
        )
        import csv
        import io
        buf = io.StringIO()
        buf.write("\ufeff")  # Excel UTF-8 BOM
        w = csv.writer(buf)
        w.writerow(["contact_id", "name", "channels", "funnel_stage",
                    "intimacy", "has_lead", "tags", "follow_up_at", "last_active_at"])
        for r in rows:
            stage_lbl = FUNNEL_STAGE_LABELS.get(r.get("funnel_stage") or "",
                                                r.get("funnel_stage") or "")
            w.writerow([
                r.get("contact_id") or "",
                r.get("primary_name") or "",
                " ".join(_PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])),
                stage_lbl,
                "" if r.get("intimacy_score") is None else r.get("intimacy_score"),
                "1" if r.get("has_lead") else "0",
                " ".join(r.get("tags") or []),
                _fmt_ts(r.get("follow_up_at") or 0),
                _fmt_ts(r.get("last_active_at") or 0),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=contacts.csv"},
        )

    # ── 2026-08-18 跨平台档案（cp-origin 面板）────────────────────────────
    # 「客户从哪个平台来 / 原平台称呼 / 聊过的话题域」：GET 供右栏卡展示，
    # POST 供表单写入（档案 → contact_profiles；关键事实 → episodic canonical 键）。

    def _origin_cfg() -> Dict[str, Any]:
        cfg = getattr(config_manager, "config", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        return (cfg.get("contacts") or {}).get("origin_profile") or {}

    def _origin_payload(store, contact_id: str, platform: str) -> Dict[str, Any]:
        """档案 + 轨迹 + 导入批次 + 注入块预览 + 卡头 pill（GET/POST 回显共用装配）。"""
        from src.contacts.origin_context import (
            build_origin_block, derive_origin_trail, origin_pill,
        )
        profile = store.get_contact_profile(contact_id)
        idents = store.list_channel_identities_of(contact_id)
        trail = derive_origin_trail(idents)
        try:
            batches = store.list_memory_imports(contact_id, limit=8)
        except Exception:
            batches = []
        confirmed = [b for b in batches if b.get("status") == "confirmed"]
        block = build_origin_block(
            profile, idents, current_platform=platform,
            max_chars=int(_origin_cfg().get("max_chars", 500) or 500),
            imports=confirmed)
        return {"profile": profile, "trail": trail, "block_preview": block,
                "pill": origin_pill(profile, trail),
                "imports": [
                    {k: b.get(k) for k in (
                        "batch_id", "source_channel", "source_label", "file_name",
                        "msg_count", "date_from", "date_to", "facts_written",
                        "status", "created_at")}
                    for b in batches
                ]}

    @app.get("/api/workspace/origin")
    async def api_workspace_origin_get(
        request: Request,
        platform: str = "",
        account_id: str = "default",
        chat_key: str = "",
    ):
        """当前会话的跨平台档案：档案字段 + 来源轨迹 + AI 注入块预览。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        from src.contacts.origin_context import resolve_contact_for_conversation
        enabled = bool(_origin_cfg().get("enabled", False))
        cid = resolve_contact_for_conversation(
            store, platform=platform, account_id=account_id, chat_key=chat_key)
        if not cid:
            return {"ok": True, "enabled": enabled, "contact_id": "",
                    "profile": None, "trail": [], "block_preview": ""}
        return {"ok": True, "enabled": enabled, "contact_id": cid,
                **_origin_payload(store, cid, platform)}

    @app.post("/api/workspace/origin")
    async def api_workspace_origin_save(request: Request, _=Depends(api_auth)):
        """写跨平台档案（部分更新）+ 关键事实进情景记忆（canonical 键）。

        Body: ``{platform, account_id, chat_key, profile?: {...}, facts?: [str]}``。
        - 会话未关联客户时按规范化 channel 自动建档（``ensure_channel_identity``）；
          平台不在 contacts 命名空间（罕见）→ 400。
        - facts 上限 5 条/每条 ≤200 字，写入键与 B 线拟稿记忆键**同口径**
          （``_episodic_storage_key(chat_key, "", platform, account_id)``），
          source=user_stated（人工填写即人工确认）、category=imported。
        - 写后清 origin provider 进程缓存 → 下一条消息立即吃到新档案。
        """
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        platform = str(body.get("platform") or "").strip()
        account_id = str(body.get("account_id") or "default").strip() or "default"
        chat_key = str(body.get("chat_key") or "").strip()
        if not platform or not chat_key:
            raise HTTPException(
                400, tr(request, "err.ws.field_required", field="platform/chat_key"))
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        from src.contacts.origin_context import (
            invalidate_origin_cache,
            normalize_channel,
            resolve_contact_for_conversation,
        )
        agent = _session_agent(request)
        cid = resolve_contact_for_conversation(
            store, platform=platform, account_id=account_id, chat_key=chat_key)
        if not cid:
            # 未关联 → 自动建档（表单填写隐含「这个会话值得建客户档案」）
            from src.contacts.models import VALID_CHANNELS
            ch = platform if platform in VALID_CHANNELS else normalize_channel(platform)
            if ch not in VALID_CHANNELS:
                raise HTTPException(400, tr(request, "err.ws.origin_unlinked"))
            contact, _ci, _created = store.ensure_channel_identity(
                channel=ch, account_id=account_id, external_id=chat_key)
            cid = contact.contact_id

        prof_in = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        if prof_in:
            def _s(key: str, cap: int) -> Optional[str]:
                v = prof_in.get(key)
                return None if v is None else str(v).strip()[:cap]
            prior = prof_in.get("prior_names")
            topics = prof_in.get("topics")
            store.upsert_contact_profile(
                cid,
                origin_channel=_s("origin_channel", 24),
                origin_label=_s("origin_label", 80),
                known_since=_s("known_since", 40),
                preferred_name=_s("preferred_name", 40),
                background_note=_s("background_note", 400),
                prior_names=(
                    {str(k)[:24]: str(v)[:40] for k, v in list(prior.items())[:8]}
                    if isinstance(prior, dict) else None),
                topics=(
                    [str(t)[:30] for t in topics[:12]]
                    if isinstance(topics, list) else None),
                ai_visible=(
                    bool(prof_in["ai_visible"])
                    if "ai_visible" in prof_in else None),
                updated_by=str(agent.get("agent_id") or ""),
            )

        facts_written = 0
        facts_duplicate = 0
        facts_error = ""
        raw_facts = body.get("facts") if isinstance(body.get("facts"), list) else []
        facts = [str(f or "").strip()[:200] for f in raw_facts[:5]]
        facts = [f for f in facts if len(f) >= 2]
        if facts:
            from src.web.web_context import resolve_skill_manager
            sm = resolve_skill_manager(
                getattr(request.app.state, "telegram_client", None), request.app)
            estore = getattr(sm, "_episodic_store", None) if sm else None
            if sm is None or estore is None:
                facts_error = "memory_unavailable"
            else:
                try:
                    key = sm._episodic_storage_key(
                        chat_key, "", platform, account_id=account_id)
                    for f in facts:
                        rid = estore.add_fact(
                            key, f, category="imported", source="user_stated")
                        if rid is None:
                            facts_duplicate += 1
                        else:
                            facts_written += 1
                except Exception:
                    logger.debug("origin facts write failed", exc_info=True)
                    facts_error = "write_failed"

        invalidate_origin_cache()  # 全清：合并/多会话同客户场景一并生效
        out = {"ok": True, "contact_id": cid,
               "enabled": bool(_origin_cfg().get("enabled", False)),
               "facts_written": facts_written,
               "facts_duplicate": facts_duplicate,
               **_origin_payload(store, cid, platform)}
        if facts_error:
            out["facts_error"] = facts_error
        return out

    # ── P1（2026-08-18）聊天记录导入：解析 → 人工勾选 → 确认写入 / 整批撤销 ──

    def _resolve_sm(request: Request):
        from src.web.web_context import resolve_skill_manager
        return resolve_skill_manager(
            getattr(request.app.state, "telegram_client", None), request.app)

    def _ensure_origin_contact(store, request: Request, *,
                               platform: str, account_id: str, chat_key: str) -> str:
        """会话 → contact_id；未关联时按规范化 channel 自动建档（与 origin 保存同口径）。"""
        from src.contacts.origin_context import (
            normalize_channel, resolve_contact_for_conversation,
        )
        cid = resolve_contact_for_conversation(
            store, platform=platform, account_id=account_id, chat_key=chat_key)
        if cid:
            return cid
        from src.contacts.models import VALID_CHANNELS
        ch = platform if platform in VALID_CHANNELS else normalize_channel(platform)
        if ch not in VALID_CHANNELS:
            raise HTTPException(400, tr(request, "err.ws.origin_unlinked"))
        contact, _ci, _created = store.ensure_channel_identity(
            channel=ch, account_id=account_id, external_id=chat_key)
        return contact.contact_id

    @app.post("/api/workspace/origin/import/parse")
    async def api_workspace_origin_import_parse(
        request: Request,
        file: UploadFile = File(...),
        platform: str = Form(""),
        account_id: str = Form("default"),
        chat_key: str = Form(""),
        source_channel: str = Form(""),
        source_label: str = Form(""),
        customer_sender: str = Form(""),
    ):
        """上传聊天导出文件 → 解析 + LLM 摘要/候选事实（**不落库**，纯预览）。

        原文不留存：本请求内解析完即弃，响应只带 sha256 与统计；换 customer_sender
        重解析 = 前端重传同一文件（≤5MB LAN 场景刻意选无状态，服务端零暂存零清扫）。
        LLM 缺席/失败软降级（summary.error 非空），运营仍可手填话题事实后确认。
        """
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        import hashlib as _hashlib
        from src.contacts.memory_import import (
            MAX_FILE_BYTES, guess_customer_sender, parse_chat_export,
            summarize_import,
        )
        data = await file.read()
        if data and len(data) > MAX_FILE_BYTES:
            return {"ok": False, "error": "file_too_large"}
        parsed = parse_chat_export(str(file.filename or ""), data or b"")
        if not parsed.get("ok"):
            return {"ok": False, "error": parsed.get("error") or "unrecognized_format"}
        senders = parsed["senders"]
        cust = str(customer_sender or "").strip() or guess_customer_sender(senders)
        sha = _hashlib.sha256(data or b"").hexdigest()
        duplicate = False
        try:
            from src.contacts.origin_context import resolve_contact_for_conversation
            cid = resolve_contact_for_conversation(
                store, platform=platform, account_id=account_id, chat_key=chat_key)
            if cid and store.find_confirmed_import_by_sha(cid, sha):
                duplicate = True
        except Exception:
            duplicate = False
        sm = _resolve_sm(request)
        rng = ""
        if parsed.get("date_from") or parsed.get("date_to"):
            rng = f"{parsed.get('date_from') or '?'}~{parsed.get('date_to') or '?'}"
        summary = await summarize_import(
            getattr(sm, "ai_client", None) if sm else None,
            parsed["messages"], cust,
            source_label=source_label or source_channel, date_range=rng)
        return {
            "ok": True,
            "format": parsed.get("format"),
            "msg_count": parsed.get("msg_count"),
            "senders": senders,
            "customer_sender": cust,
            "date_from": parsed.get("date_from"),
            "date_to": parsed.get("date_to"),
            "truncated": bool(parsed.get("truncated")),
            "file_sha256": sha,
            "file_name": str(file.filename or "")[:120],
            "duplicate": duplicate,
            "summary": summary,
        }

    @app.post("/api/workspace/origin/import/confirm")
    async def api_workspace_origin_import_confirm(request: Request, _=Depends(api_auth)):
        """人工勾选后的最终写入：episodic 事实（canonical 键）+ 档案话题/背景合并 +
        批次台账（记 memory_key + fact_hashes，供整批撤销精确删除）。

        Body: ``{platform, account_id, chat_key, source_channel?, source_label?,
        file_name?, file_sha256?, msg_count?, date_from?, date_to?, topics?: [str],
        note?: str, facts?: [str]}``。同客户同文件指纹重复确认 → duplicate_import。
        """
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        platform = str(body.get("platform") or "").strip()
        account_id = str(body.get("account_id") or "default").strip() or "default"
        chat_key = str(body.get("chat_key") or "").strip()
        if not platform or not chat_key:
            raise HTTPException(
                400, tr(request, "err.ws.field_required", field="platform/chat_key"))
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        from src.contacts.memory_import import MAX_FACTS, MAX_TOPICS
        from src.contacts.origin_context import invalidate_origin_cache
        agent = _session_agent(request)
        cid = _ensure_origin_contact(
            store, request, platform=platform, account_id=account_id, chat_key=chat_key)
        sha = str(body.get("file_sha256") or "").strip()[:64]
        if sha and store.find_confirmed_import_by_sha(cid, sha):
            return {"ok": False, "error": "duplicate_import"}

        # ① 事实 → episodic（canonical 键，与表单/B 线同口径）
        raw_facts = body.get("facts") if isinstance(body.get("facts"), list) else []
        facts = [str(f or "").strip()[:200] for f in raw_facts[:MAX_FACTS]]
        facts = [f for f in facts if len(f) >= 2]
        facts_written, facts_duplicate = 0, 0
        fact_hashes: List[str] = []
        memory_key = ""
        facts_error = ""
        if facts:
            sm = _resolve_sm(request)
            estore = getattr(sm, "_episodic_store", None) if sm else None
            if sm is None or estore is None:
                facts_error = "memory_unavailable"
            else:
                try:
                    from src.utils.episodic_memory_store import content_hash_of
                    memory_key = sm._episodic_storage_key(
                        chat_key, "", platform, account_id=account_id)
                    for f in facts:
                        rid = estore.add_fact(
                            memory_key, f, category="imported", source="user_stated")
                        if rid is None:
                            facts_duplicate += 1
                        else:
                            facts_written += 1
                            fact_hashes.append(content_hash_of(f))
                except Exception:
                    logger.debug("origin import facts write failed", exc_info=True)
                    facts_error = "write_failed"

        # ② 话题/背景 → 档案合并（union 话题；来源/背景只补空不覆盖人工值）
        topics_in = [str(t).strip()[:30] for t in (body.get("topics") or [])
                     if str(t).strip()][:MAX_TOPICS]
        note_in = str(body.get("note") or "").strip()[:400]
        src_ch = str(body.get("source_channel") or "").strip()[:24]
        prof = store.get_contact_profile(cid) or {}
        merged_topics = None
        if topics_in:
            merged_topics = list(prof.get("topics") or [])
            for t in topics_in:
                if t not in merged_topics:
                    merged_topics.append(t)
            merged_topics = merged_topics[:12]
        store.upsert_contact_profile(
            cid,
            origin_channel=(src_ch if src_ch and not (prof.get("origin_channel") or "") else None),
            origin_label=(str(body.get("source_label") or "").strip()[:80]
                          if body.get("source_label") and not (prof.get("origin_label") or "") else None),
            background_note=(note_in if note_in and not (prof.get("background_note") or "") else None),
            topics=merged_topics,
            updated_by=str(agent.get("agent_id") or ""),
        )

        # ③ 批次台账（可撤销单位）
        batch_id = store.insert_memory_import(
            contact_id=cid,
            source_channel=src_ch,
            source_label=str(body.get("source_label") or "")[:80],
            file_name=str(body.get("file_name") or "")[:120],
            file_sha256=sha,
            msg_count=int(body.get("msg_count") or 0),
            date_from=str(body.get("date_from") or "")[:10],
            date_to=str(body.get("date_to") or "")[:10],
            summary={"topics": topics_in, "note": note_in},
            memory_key=memory_key,
            fact_hashes=fact_hashes,
            facts_written=facts_written,
            created_by=str(agent.get("agent_id") or ""),
        )
        invalidate_origin_cache()
        out = {"ok": True, "contact_id": cid, "batch_id": batch_id,
               "enabled": bool(_origin_cfg().get("enabled", False)),
               "facts_written": facts_written,
               "facts_duplicate": facts_duplicate,
               **_origin_payload(store, cid, platform)}
        if facts_error:
            out["facts_error"] = facts_error
        return out

    @app.post("/api/workspace/origin/import/revoke")
    async def api_workspace_origin_import_revoke(request: Request, _=Depends(api_auth)):
        """整批撤销一次导入：按台账指纹精确删 episodic 事实 + 批次标记 revoked。

        Body: ``{batch_id, platform?}``。诚实边界：确认时合并进档案的话题/背景
        不自动回滚（叙事层是人工可编辑的合并态，回滚会误伤手工修订）——档案在
        表单里直接改即可。
        """
        body = await request.json()
        body = body if isinstance(body, dict) else {}
        batch_id = str(body.get("batch_id") or "").strip()
        if not batch_id:
            raise HTTPException(
                400, tr(request, "err.ws.field_required", field="batch_id"))
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled"}
        from src.contacts.origin_context import invalidate_origin_cache
        batch = store.get_memory_import(batch_id)
        if batch is None:
            raise HTTPException(404, tr(request, "err.ws.import_not_found"))
        if batch.get("status") != "confirmed":
            return {"ok": False, "error": "already_revoked"}
        deleted = 0
        hashes = list(batch.get("fact_hashes") or [])
        mkey = str(batch.get("memory_key") or "")
        if hashes and mkey:
            sm = _resolve_sm(request)
            estore = getattr(sm, "_episodic_store", None) if sm else None
            if estore is not None and hasattr(estore, "delete_by_hashes"):
                try:
                    deleted = int(estore.delete_by_hashes(mkey, hashes) or 0)
                except Exception:
                    logger.debug("origin import revoke delete failed", exc_info=True)
        store.mark_memory_import_revoked(batch_id)
        invalidate_origin_cache()
        return {"ok": True, "batch_id": batch_id,
                "facts_deleted": deleted,
                "facts_expected": len(hashes)}
