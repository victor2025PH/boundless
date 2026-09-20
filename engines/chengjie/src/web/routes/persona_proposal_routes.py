# -*- coding: utf-8 -*-
"""人设补丁提案 API（P0）。

- GET  /api/personas/proposals/status
- POST /api/personas/profiles/{profile_id}/proposals/generate
- GET  /api/personas/profiles/{profile_id}/proposals
- POST /api/personas/profiles/{profile_id}/proposals/{proposal_id}/accept
- POST /api/personas/profiles/{profile_id}/proposals/{proposal_id}/reject

Feature flag：``personas.proposals.enabled``（默认关）。
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.persona_proposal_routes")
_ROLE_VIEWER = "viewer"


def register_persona_proposal_routes(app, auth_dep, audit_store=None,
                                     config_manager=None):
    def _live_config(request: Request) -> dict:
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        return getattr(cm, "config", None) or {}

    def _flag_enabled(request: Request) -> bool:
        sect = (_live_config(request).get("personas") or {}).get("proposals") or {}
        return bool(sect.get("enabled", False))

    def _require_enabled(request: Request) -> None:
        if not _flag_enabled(request):
            raise HTTPException(403, tr(request, "err.psn.proposals_off"))

    def _check_write_role(request: Request) -> None:
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.persona.readonly_no_edit"))

    def _actor(request: Request) -> str:
        try:
            return (request.session.get("username")
                    or request.session.get("user", "") or "api")
        except Exception:
            return "api"

    def _persist(pm, request: Request):
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        enabled = True
        try:
            cfg = getattr(cm, "config", None) or {}
            enabled = bool((cfg.get("persona_persistence") or {}).get("enabled", True))
        except Exception:
            pass
        persisted = False
        try:
            persisted = bool(pm.persist_profiles(cm))
        except Exception:
            logger.warning("proposal persist_profiles 异常", exc_info=True)
            persisted = False
        return persisted, (enabled and not persisted)

    @app.get("/api/personas/proposals/status")
    async def api_persona_proposals_status(request: Request, _=Depends(auth_dep)):
        return {"ok": True, "enabled": _flag_enabled(request)}

    @app.post("/api/personas/profiles/{profile_id}/proposals/generate")
    async def api_persona_proposals_generate(profile_id: str, request: Request,
                                             _=Depends(auth_dep)):
        _require_enabled(request)
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager, profile_rev
        from src.utils.persona_proposals import collect_drafts, draft_to_row
        from src.utils import persona_proposal_store as pps

        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(profile_id)
        if persona is None:
            raise HTTPException(
                404, tr(request, "err.persona.profile_not_found", name=profile_id))
        store = pps.get()
        if store is None:
            raise HTTPException(500, tr(request, "err.psn.proposals_store"))
        rev = profile_rev(persona)
        store.mark_stale_outdated(profile_id, rev)
        created = 0
        reused = 0
        for draft in collect_drafts(persona):
            before = store.list(profile_id, status="pending")
            keys = {r.get("dedupe_key") for r in before}
            rec = store.insert_pending(draft_to_row(profile_id, draft, rev))
            if rec is None:
                continue
            if rec.get("dedupe_key") in keys:
                reused += 1
            else:
                created += 1
        pending = store.list(profile_id, status="pending")
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_proposal_generate",
                                f"id={profile_id} add={created} reuse={reused}")
            except Exception:
                pass
        return {"ok": True, "generated": created, "reused": reused,
                "pending": len(pending), "proposals": pending, "rev": rev}

    @app.get("/api/personas/profiles/{profile_id}/proposals")
    async def api_persona_proposals_list(profile_id: str, request: Request,
                                         _=Depends(auth_dep)):
        _require_enabled(request)
        from src.utils import persona_proposal_store as pps

        store = pps.get()
        if store is None:
            raise HTTPException(500, tr(request, "err.psn.proposals_store"))
        status = str(request.query_params.get("status") or "pending")
        try:
            lim = int(request.query_params.get("limit") or 50)
        except Exception:
            lim = 50
        return {"ok": True, "proposals": store.list(profile_id, status=status, limit=lim),
                "pending": store.pending_count(profile_id)}

    @app.post("/api/personas/profiles/{profile_id}/proposals/{proposal_id}/accept")
    async def api_persona_proposals_accept(profile_id: str, proposal_id: int,
                                           request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager, profile_rev
        from src.utils.persona_proposals import (
            build_apply_patch, is_hard_lock, is_writable_proposal,
        )
        from src.utils import persona_proposal_store as pps

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        store = pps.get()
        if store is None:
            raise HTTPException(500, tr(request, "err.psn.proposals_store"))
        rec = store.get(profile_id, proposal_id)
        if rec is None:
            raise HTTPException(404, tr(request, "err.psn.proposal_not_found"))
        if rec.get("status") != "pending":
            raise HTTPException(409, tr(request, "err.psn.proposal_not_pending"))

        field = str(rec.get("field") or "")
        action = str(rec.get("action") or "")
        kind = str(rec.get("kind") or "")
        if is_hard_lock(field, action) or not is_writable_proposal(kind, field, action):
            raise HTTPException(409, tr(request, "err.psn.proposal_hard_lock"))

        proposed = body["proposed"] if "proposed" in body else rec.get("proposed")
        if proposed in (None, "", [], {}):
            raise HTTPException(400, tr(request, "err.psn.proposal_needs_value"))

        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(profile_id)
        if persona is None:
            raise HTTPException(
                404, tr(request, "err.persona.profile_not_found", name=profile_id))
        cur_rev = profile_rev(persona)
        if rec.get("base_rev") and rec.get("base_rev") != cur_rev:
            store.set_status(profile_id, proposal_id, "stale",
                             reviewed_by=_actor(request))
            raise HTTPException(409, tr(request, "err.psn.proposal_stale"))

        try:
            patch = build_apply_patch(persona, field, action, proposed)
        except ValueError as exc:
            code = str(exc.args[0] if exc.args else exc)
            if code == "proposed_empty":
                raise HTTPException(400, tr(request, "err.psn.proposal_needs_value"))
            raise HTTPException(409, tr(request, "err.psn.proposal_hard_lock"))

        merged = pm.deep_merge_profile(persona, patch)
        pm.upsert_profile(profile_id, merged)
        persisted, persist_warn = _persist(pm, request)
        new_rev = profile_rev(pm.get_persona_by_id(profile_id))
        store.set_status(profile_id, proposal_id, "applied",
                         reviewed_by=_actor(request),
                         proposed=proposed, update_proposed=True)
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_proposal_apply",
                                f"id={profile_id} proposal={proposal_id} field={field}"
                                + ("" if persisted else " persisted=0"))
            except Exception:
                pass
        return {"ok": True, "profile_id": profile_id, "proposal_id": proposal_id,
                "field": field, "rev": new_rev,
                "persisted": persisted, "persist_warning": persist_warn}

    @app.post("/api/personas/profiles/{profile_id}/proposals/{proposal_id}/reject")
    async def api_persona_proposals_reject(profile_id: str, proposal_id: int,
                                           request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        _check_write_role(request)
        from src.utils import persona_proposal_store as pps

        store = pps.get()
        if store is None:
            raise HTTPException(500, tr(request, "err.psn.proposals_store"))
        rec = store.get(profile_id, proposal_id)
        if rec is None:
            raise HTTPException(404, tr(request, "err.psn.proposal_not_found"))
        if rec.get("status") != "pending":
            raise HTTPException(409, tr(request, "err.psn.proposal_not_pending"))
        store.set_status(profile_id, proposal_id, "rejected",
                         reviewed_by=_actor(request))
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_proposal_reject",
                                f"id={profile_id} proposal={proposal_id}")
            except Exception:
                pass
        return {"ok": True, "proposal_id": proposal_id, "status": "rejected"}
