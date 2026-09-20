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
        """全部可见案例（未结案在前；已结案滞留 7 天淡出）。

        ``include_drill=True``：本页是演练号段案例唯一可见处（前端默认过滤、
        「演练」筛选下可见可清账）；其余消费方（看门狗/徽标/监控）走
        collect_case_rows 默认口径自动排除。事件时间线逐条带本地化 ``label``
        （code 即 reason i18n 键，与卡片主 reason 同一词典）。
        """
        _api_auth(request)
        ctx_store = _get_ctx_store()
        if not ctx_store:
            return {"cases": [], "count": 0}

        from src.utils.case_center import (
            collect_case_rows, effectiveness_alerts,
            media_resolution_effectiveness, summarize_case_board,
            summarize_takeover_episodes,
        )
        rows = collect_case_rows(ctx_store, include_drill=True)
        for r in rows:
            r["reason"] = _localized_reason(request, r)
            r.pop("reason_params", None)
            for e in (r.get("events") or []):
                code = str(e.get("code") or "")
                label = ""
                if code:
                    fmt2 = e.get("params") if isinstance(e.get("params"), dict) else {}
                    try:
                        label = tr(request, code, None,
                                   **{str(k): v for k, v in fmt2.items()})
                    except Exception:
                        label = code
                    if label == code:
                        # 未登记键（域包自定义链模式等）→ 只留尾段别裸奔整个键名
                        label = code.rsplit(".", 1)[-1]
                e["label"] = label
                e.pop("params", None)
        # P4：看板摘要（媒体超龄 / 演练残影）— ops 卡与前端 chips 同源，不二次扫库
        summary = summarize_case_board(rows)
        # P6：安抚有效性回环（媒体质疑结案后 7 天同源复发率，按分桶）——
        # 有样本才带字段（零流量不占 payload），异常吞掉不阻断列表主体。
        # P7：alerts＝需要人检视的分桶（样本够且复发率高），前端/周报同一规则。
        effectiveness = None
        try:
            eff = media_resolution_effectiveness(ctx_store)
            if int(eff.get("closed") or 0) > 0:
                eff["alerts"] = effectiveness_alerts(eff)
                effectiveness = eff
        except Exception:
            effectiveness = None
        # P7：人工接管闭环（case:handoff 切档 → 回流时长）——inbox store 未挂载/
        # 旧 store 无方法一律静默省略。
        takeover_stats = None
        try:
            from src.web.routes.unified_inbox_services import _inbox_store
            ibx = _inbox_store(request)
            if ibx is not None and hasattr(ibx, "takeover_episodes"):
                eps = ibx.takeover_episodes()
                if eps:
                    takeover_stats = summarize_takeover_episodes(eps)
        except Exception:
            takeover_stats = None
        body = {
            "cases": rows[:100],
            "count": len(rows),
            "summary": summary,
        }
        if effectiveness:
            body["effectiveness"] = effectiveness
        if takeover_stats:
            body["takeover_stats"] = takeover_stats
        return body

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

    @app.post("/api/cases/close-drill")
    async def api_cases_close_drill(request: Request):
        """批量结案未结演练号段案例（duel_nightly 收尾；零误伤真实客户）。

        body 可选 ``{resolution}``；默认 ``drill nightly cleanup``。返回
        ``{ok, closed}``。看门狗/徽标本就不数 drill，本端点只是清 /cases 残影。
        """
        _api_auth(request)
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        resolution = str(data.get("resolution") or "").strip()
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, tr(request, "err.svc.context_store_not_ready"))
        from src.utils.case_center import (
            DRILL_CLEANUP_RESOLUTION,
            close_open_drill_cases,
            count_open_drill_cases,
        )
        n = close_open_drill_cases(
            ctx_store,
            resolution=resolution or DRILL_CLEANUP_RESOLUTION,
            now=time.time(),
            persist=True,
        )
        remaining = count_open_drill_cases(ctx_store)
        actor = request.session.get("username", "web_admin")
        if audit_store and n:
            audit_store.log(actor, "case_close_drill", f"n={n}", "",
                            (resolution or DRILL_CLEANUP_RESOLUTION)[:80])
        return {
            "ok": True,
            "closed": int(n),
            "remaining_open_drill": int(remaining),
        }

    @app.post("/api/cases/{case_id}/close")
    async def api_case_close(request: Request, case_id: str):
        """运营人员结案（复发时由 case_center 归档另立新案）。"""
        _api_auth(request)
        data = await request.json()
        resolution = (data.get("resolution") or "").strip()
        try:
            mute_hours = float(data.get("mute_hours") or 0)
        except (TypeError, ValueError):
            mute_hours = 0.0
        takeover = bool(data.get("takeover"))
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, tr(request, "err.svc.context_store_not_ready"))
        uid, c = _find_case_ctx(ctx_store, case_id)
        if c is None:
            raise HTTPException(404, tr(request, "err.case.not_found", case_id=case_id))
        from src.utils.case_center import (
            FALSE_ALARM_MUTE_MAX_HOURS, close_case, conv_ref, mute_allowed,
        )
        mute_hours = min(max(0.0, mute_hours), FALSE_ALARM_MUTE_MAX_HOURS)
        close_case(c, resolution, now=time.time(),
                   mute_same_source_hours=mute_hours)
        # 与 close_case 同一条规则（mute_allowed）算「实际生效」——crisis 勾了也不静默
        applied = (mute_hours if (mute_hours > 0
                                  and mute_allowed(str(c.get("_case_source") or "")))
                   else 0.0)
        # P6：转人工结案可选把会话切 manual（AI 停发、人工接管；takeover_rearm
        # 既有生命周期会在空闲后按其规则回流）。soft-fail：inbox store 未挂载 /
        # conv_ref 拼不出 → 回包 takeover=false，结案本体不受影响。
        takeover_applied = False
        if takeover:
            try:
                from src.web.routes.unified_inbox_services import _inbox_store
                ibx = _inbox_store(request)
                ref = conv_ref(c)
                if ibx is not None and ref:
                    ibx.set_automation_mode(ref, "manual", source="case:handoff")
                    takeover_applied = True
            except Exception:
                takeover_applied = False
        # 备注空时把结案原因抄进 note——列表「备注」列立刻可读，免二次填写
        if resolution and not str(c.get("_case_note") or "").strip():
            c["_case_note"] = resolution[:500]
        _persist(ctx_store, uid)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            detail = (resolution[:80]
                      + (f" [mute={applied:g}h]" if applied else "")
                      + (" [takeover]" if takeover_applied else ""))
            audit_store.log(actor, "case_close", case_id, uid, detail)
        return {
            "ok": True,
            "case_id": case_id,
            "resolution_bucket": str(c.get("_case_resolution_bucket") or ""),
            "muted_hours": applied,
            "takeover": takeover_applied,
        }
