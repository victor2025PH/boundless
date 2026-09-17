# -*- coding: utf-8 -*-
"""视觉记忆 API（#333 EREM2H / REA733，2026-09-17）：图片记忆**可看、可纠正、可删**。

挂 ``/api/visual-memory/*``（登录用户）：

- ``GET    /api/visual-memory/status``                          边车健康 + 开关（不带密钥）
- ``GET    /api/visual-memory/{cid}``                           该会话的实体 + 最近观察（向量剥掉）
- ``POST   /api/visual-memory/{cid}/confirm``  ``{kind: self|relation, relation?, observation_id?}``
- ``POST   /api/visual-memory/{cid}/deny``     最近一张带脸观察改「不是本人」
- ``POST   /api/visual-memory/{cid}/entities/{eid}/retire``    退休一个实体（AI 记错了）
- ``DELETE /api/visual-memory/{cid}``                           整会话清空

纠正一律 ``source=user_confirmed``（人工 > AI 推断）；审计 ``vmem_*``。
存储单源 ``src/companion/visual_memory``；路由不带语义。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_OBS_LIMIT_MAX = 100


def _strip(d: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(d)
    emb = out.pop("embedding", None)
    out["has_embedding"] = bool(emb)
    return out


def register_visual_memory_routes(app: Any, *, auth_dep: Any, audit_store: Any = None,
                                  config_manager: Any = None, store_getter: Any = None) -> None:
    def _store():
        if store_getter is not None:
            return store_getter()
        from src.companion.visual_memory import get_visual_memory_store
        return get_visual_memory_store()

    def _cfg() -> Dict[str, Any]:
        try:
            return getattr(config_manager, "config", None) or {}
        except Exception:
            return {}

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web_admin")
        except Exception:
            return "web_admin"

    def _audit(request: Request, action: str, target: str = "", detail: str = "") -> None:
        if audit_store is None:
            return
        try:
            audit_store.log(_actor(request), action, target, "", detail)
        except Exception:
            logger.debug("[vmem] audit write failed", exc_info=True)

    def _require(request: Request):
        st = _store()
        if st is None:
            raise HTTPException(503, tr(request, "err.vmem.store_unavailable",
                                        "visual memory store unavailable"))
        return st

    def _cid(request: Request, conversation_id: str) -> str:
        cid = str(conversation_id or "").strip()
        if not cid or len(cid) > 200:
            raise HTTPException(400, tr(request, "err.vmem.bad_conversation", "bad conversation id"))
        return cid

    @app.get("/api/visual-memory/status")
    async def vmem_status(request: Request, _=Depends(auth_dep)):
        from src.companion.face_identity import FaceEmbedClient, face_cfg, stats as _fi_stats
        cfg = face_cfg(_cfg())
        healthy: Optional[bool] = None
        if cfg["enabled"]:
            try:
                import asyncio
                cl = FaceEmbedClient(cfg["base_url"], timeout_sec=min(cfg["timeout_sec"], 4.0),
                                     api_key=cfg["api_key"])
                healthy = await asyncio.to_thread(cl.health)
            except Exception:
                healthy = False
        # 2026-09-18 首验沉淀：进程级退出原因计数 + 边车耗时分位。「calls 涨、face_ok/no_face 不涨、
        # no_path 涨」= 平台落了图但身份层拿不到文件（渠道盲区）；「service_fail 涨」= 边车/隧道；
        # 「calls 不涨」= 真没流量或接线没调到。三种此前长得一模一样。
        try:
            st = _fi_stats()
        except Exception:
            st = {}
        # 库侧总量（跨重启）：观察行数 / 已确认实体数——「记忆到底攒了多少」
        totals: Dict[str, Any] = {}
        try:
            store = _store()
            if store is not None:
                conn = getattr(store, "_conn", None)
                lock = getattr(store, "_lock", None)
                if conn is not None and lock is not None:
                    with lock:
                        r1 = conn.execute("SELECT COUNT(*), SUM(embedding<>''), SUM(confirmed) FROM visual_observations").fetchone()
                        r2 = conn.execute("SELECT COUNT(*) FROM visual_entities WHERE retired=0 AND source='user_confirmed'").fetchone()
                    totals = {"observations": int(r1[0] or 0), "with_face": int(r1[1] or 0),
                              "confirmed_observations": int(r1[2] or 0), "confirmed_entities": int(r2[0] or 0)}
        except Exception:
            totals = {}
        return {"ok": True, "enabled": cfg["enabled"], "base_url": cfg["base_url"],
                "hosted": cfg["hosted"], "timeout_sec": cfg["timeout_sec"], "service_healthy": healthy,
                "stats": st, "totals": totals}

    @app.get("/api/visual-memory/{conversation_id}")
    async def vmem_get(conversation_id: str, request: Request, limit: int = 20, _=Depends(auth_dep)):
        st = _require(request)
        cid = _cid(request, conversation_id)
        lim = max(1, min(int(limit or 20), _OBS_LIMIT_MAX))
        vec, src = st.self_prototype(cid)
        return {
            "ok": True, "conversation_id": cid,
            "self_prototype": {"present": bool(vec), "source": src},
            "entities": [_strip(e) for e in st.entities(cid)],
            "observations": [_strip(o) for o in st.list_observations(cid, limit=lim)],
        }

    @app.post("/api/visual-memory/{conversation_id}/confirm")
    async def vmem_confirm(conversation_id: str, request: Request, _=Depends(auth_dep)):
        st = _require(request)
        cid = _cid(request, conversation_id)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        kind = str(body.get("kind") or "self").strip().lower()
        oid = int(body.get("observation_id") or 0)
        if kind == "self":
            ok = st.confirm_self(cid, observation_id=oid)
        elif kind == "relation":
            rel = str(body.get("relation") or "").strip()[:32]
            if not rel:
                raise HTTPException(400, tr(request, "err.vmem.relation_required", "relation required"))
            ok = st.confirm_relation(cid, rel, observation_id=oid)
        else:
            raise HTTPException(400, tr(request, "err.vmem.bad_kind", "kind must be self|relation"))
        if not ok:
            raise HTTPException(409, tr(request, "err.vmem.no_face_observation",
                                        "no face observation to confirm"))
        _audit(request, "vmem_confirm", cid, f"kind={kind} oid={oid} rel={body.get('relation') or ''}")
        return {"ok": True, "conversation_id": cid, "kind": kind}

    @app.post("/api/visual-memory/{conversation_id}/deny")
    async def vmem_deny(conversation_id: str, request: Request, _=Depends(auth_dep)):
        st = _require(request)
        cid = _cid(request, conversation_id)
        ok = st.deny_self(cid)
        if not ok:
            raise HTTPException(409, tr(request, "err.vmem.no_face_observation",
                                        "no face observation to deny"))
        _audit(request, "vmem_deny", cid)
        return {"ok": True, "conversation_id": cid}

    @app.post("/api/visual-memory/{conversation_id}/entities/{entity_id}/retire")
    async def vmem_retire(conversation_id: str, entity_id: int, request: Request, _=Depends(auth_dep)):
        st = _require(request)
        cid = _cid(request, conversation_id)
        ok = st.retire_entity(cid, int(entity_id))
        if not ok:
            raise HTTPException(404, tr(request, "err.vmem.entity_not_found", "entity not found"))
        _audit(request, "vmem_retire", cid, f"eid={entity_id}")
        return {"ok": True, "conversation_id": cid, "entity_id": int(entity_id)}

    @app.delete("/api/visual-memory/{conversation_id}")
    async def vmem_delete(conversation_id: str, request: Request, _=Depends(auth_dep)):
        st = _require(request)
        cid = _cid(request, conversation_id)
        n = st.delete_conv(cid)
        _audit(request, "vmem_delete", cid, f"observations={n}")
        return {"ok": True, "conversation_id": cid, "deleted_observations": n}


__all__ = ["register_visual_memory_routes"]
