"""运营 case 管理 API 路由（Phase E1 续拆；2026-08-03 案例中心改造）。

端点（路径与抽出前一致，响应向后兼容、只增字段）：
  GET  /api/cases/active            案例列表（内存缓存 + SQLite 兜底，重启不失明）
  POST /api/cases/{case_id}/note    为 case 加备注（写后即刷持久化）
  POST /api/cases/{case_id}/close   结案（写后即刷持久化）

生命周期/排序/行构建等语义全部收在 ``src/utils/case_center``（纯函数核心，
skill_manager 立案侧与本路由消费侧共用同一事实源）。

reason 本地化：上下文里只存 i18n 键 + 参数（``_case_reason_code/_params``），
本路由按请求语言经 ``tr()`` 渲染 ``reason`` 字段——响应文案零硬编码中文
（``_ROUTE_CJK_CEILINGS`` 本文件密封为 0）。
"""

from __future__ import annotations

import time

from fastapi import HTTPException, Request
from src.web.web_i18n import tr


def register_cases_routes(app, ctx) -> None:
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    audit_store = ctx.audit_store

    def _get_ctx_store():
        """获取 context_store 实例（主客户端 → app.state 双通路）。"""
        from src.web.web_context import resolve_skill_manager
        sm = resolve_skill_manager(telegram_client, app)
        return getattr(sm, "_context_store", None) if sm else None

    def _persist(ctx_store, uid: str) -> None:
        """备注/结案写入后立即落盘——否则该用户不再发消息时改动只活在内存，
        重启即丢（旧实现的静默丢数据缺陷）。"""
        try:
            if hasattr(ctx_store, "mark_dirty"):
                ctx_store.mark_dirty(uid)
            if hasattr(ctx_store, "flush"):
                ctx_store.flush(uid)
        except Exception:
            pass

    def _find_case_ctx(ctx_store, case_id: str):
        """按案号定位 (uid, ctx)：先内存缓存，再 SQLite 兜底（经 get() 载回缓存）。"""
        for uid, c in list(getattr(ctx_store, "_cache", {}).items()):
            if isinstance(c, dict) and c.get("_case_id") == case_id:
                return uid, c
        iter_db = getattr(ctx_store, "iter_persisted_case_rows", None)
        if callable(iter_db):
            try:
                for uid, c in iter_db():
                    if c.get("_case_id") == case_id:
                        return uid, ctx_store.get(uid)
            except Exception:
                pass
        return None, None

    def _localized_reason(request: Request, row: dict) -> str:
        code = str(row.get("reason_code") or "case.reason.legacy")
        params = row.get("reason_params")
        fmt = {}
        if isinstance(params, dict):
            fmt = {str(k): v for k, v in params.items()}
        try:
            text = tr(request, code, None, **fmt)
        except Exception:
            text = code
        if text == code and row.get("pattern_desc"):
            # 未登记的键（如域包自定义链模式）→ 退回模式描述，别把键名裸给运营。
            return str(row.get("pattern_desc"))
        return text

    @app.get("/api/cases/active")
    async def api_cases_active(request: Request):
        """全部可见案例（未结案在前；已结案滞留 7 天淡出）。"""
        _api_auth(request)
        ctx_store = _get_ctx_store()
        if not ctx_store:
            return {"cases": [], "count": 0}

        from src.utils.case_center import collect_case_rows
        rows = collect_case_rows(ctx_store)
        for r in rows:
            r["reason"] = _localized_reason(request, r)
            r.pop("reason_params", None)
        return {"cases": rows[:100], "count": len(rows)}

    @app.post("/api/cases/{case_id}/note")
    async def api_case_note(request: Request, case_id: str):
        """运营人员为 case 添加备注。"""
        _api_auth(request)
        data = await request.json()
        note = (data.get("note") or "").strip()
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, tr(request, "err.svc.context_store_not_ready"))
        uid, c = _find_case_ctx(ctx_store, case_id)
        if c is None:
            raise HTTPException(404, tr(request, "err.case.not_found", case_id=case_id))
        c["_case_note"] = note[:500]
        _persist(ctx_store, uid)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "case_note", case_id, uid, note[:80])
        return {"ok": True, "case_id": case_id}

    @app.post("/api/cases/{case_id}/claim")
    async def api_case_claim(request: Request, case_id: str):
        """认领 / 释放案例（body: {"release": bool}；多坐席分工用）。"""
        _api_auth(request)
        data = await request.json()
        release = bool(data.get("release"))
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, tr(request, "err.svc.context_store_not_ready"))
        uid, c = _find_case_ctx(ctx_store, case_id)
        if c is None:
            raise HTTPException(404, tr(request, "err.case.not_found", case_id=case_id))
        actor = request.session.get("username", "web_admin")
        from src.utils.case_center import claim_case, release_case
        if release:
            release_case(c)
            _persist(ctx_store, uid)
            if audit_store:
                audit_store.log(actor, "case_release", case_id, uid, "")
            return {"ok": True, "case_id": case_id, "claimed_by": ""}
        ok, holder = claim_case(c, actor, now=time.time())
        if not ok:
            raise HTTPException(
                409, tr(request, "err.case.claimed_by_other", name=holder or "?"))
        _persist(ctx_store, uid)
        if audit_store:
            audit_store.log(actor, "case_claim", case_id, uid, "")
        return {"ok": True, "case_id": case_id, "claimed_by": holder}

    @app.post("/api/cases/{case_id}/close")
    async def api_case_close(request: Request, case_id: str):
        """运营人员结案（复发时由 case_center 归档另立新案）。"""
        _api_auth(request)
        data = await request.json()
        resolution = (data.get("resolution") or "").strip()
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, tr(request, "err.svc.context_store_not_ready"))
        uid, c = _find_case_ctx(ctx_store, case_id)
        if c is None:
            raise HTTPException(404, tr(request, "err.case.not_found", case_id=case_id))
        from src.utils.case_center import close_case
        close_case(c, resolution, now=time.time())
        _persist(ctx_store, uid)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "case_close", case_id, uid, resolution[:80])
        return {"ok": True, "case_id": case_id}
