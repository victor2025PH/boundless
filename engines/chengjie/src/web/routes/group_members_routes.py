"""Telegram 群成员提取 —— 后台 API。

挂 ``/api/tg-members/*``。职责：建/查/停「提取任务」、列成员库、CSV 导出、每日配额水位。
提取本体是 async 协程，**调度到该号 pyrogram client 自己的事件循环上**（web loop 与
pyro loop 不同 → ``run_coroutine_threadsafe``），不阻塞 web 请求；进度写任务行，前端轮询。

提取是只读。同群开口是另一组端点（/outreach/*）：每日 5–10、逐条确认才发，
不在提取任务里顺手发。
写操作（建/停任务）viewer 角色 403。总闸＝``companion.group_members.enabled``
**或** 开发者页「群成员提取」显隐（``ui_visibility.group_extract``）。勾上入口即可用，
不必再改 yaml。两者都关才 403。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.group_members_routes")

_ROLE_VIEWER = "viewer"
_DEFAULT_DAILY_CAP = 200
_DEFAULT_SCAN_LIMIT = 3000


def register_group_members_routes(app, auth_dep, audit_store=None, config_manager=None,
                                  page_auth=None):
    """挂载 Telegram 群成员提取后台 API。``auth_dep``=登录校验；``audit_store``=操作审计（可选）。

    ``page_auth`` 给 HTML 管理台页用（未登录 303 去 /login）。缺省回落 ``auth_dep``。
    管理台是整页导航，不能走 API 的 401 JSON——桌面 ``target=_blank`` 弹窗会把
    ``{"detail":"Unauthorized"}`` 渲染成 Chromium JSON 预览，坐席以为管理台坏了。
    """
    html_auth = page_auth if page_auth is not None else auth_dep

    def _full() -> Dict[str, Any]:
        try:
            full = (config_manager.config if config_manager is not None else {}) or {}
            return full if isinstance(full, dict) else {}
        except Exception:
            return {}

    def _cfg() -> Dict[str, Any]:
        try:
            return ((_full().get("companion") or {}).get("group_members") or {})
        except Exception:
            return {}

    def _enabled() -> bool:
        """yaml 总闸或开发者页「群成员提取」勾选，任一为真即可用。"""
        try:
            if bool(_cfg().get("enabled", False)):
                return True
            from src.web.ui_visibility import resolve_ui_visibility
            return bool(resolve_ui_visibility(_full()).get("group_extract"))
        except Exception:
            return False

    def _store():
        from src.companion.group_members_store import get_group_members_store
        return get_group_members_store()

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
            logger.debug("[group_members] 审计写入失败（已忽略）", exc_info=True)

    def _require_enabled(request: Request) -> None:
        if not _enabled():
            raise HTTPException(403, tr(request, "err.gm.disabled"))

    def _require_store(request: Request):
        st = _store()
        if st is None:
            raise HTTPException(503, tr(request, "err.gm.store_unavailable"))
        return st

    def _require_write(request: Request) -> None:
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.gm.readonly"))

    def _default_cap() -> int:
        try:
            return int(_cfg().get("daily_cap_per_account", _DEFAULT_DAILY_CAP))
        except Exception:
            return _DEFAULT_DAILY_CAP

    def _default_scan() -> int:
        try:
            return int(_cfg().get("scan_limit", _DEFAULT_SCAN_LIMIT))
        except Exception:
            return _DEFAULT_SCAN_LIMIT

    def _default_group_cap() -> int:
        try:
            return int(_cfg().get("group_daily_cap", 0))
        except Exception:
            return 0

    def _default_global_cap() -> int:
        try:
            return int(_cfg().get("global_daily_cap", 0))
        except Exception:
            return 0

    # ── 提取任务 ─────────────────────────────────────────────────────────────

    @app.post("/api/tg-members/jobs")
    async def create_extract_job(request: Request, _=Depends(auth_dep)):
        """建并立即启动提取（支持多号并行：一号一 job、各自分片、互不重叠）。

        body: ``account_ids``（号数组）或 ``account_id``（单号）, ``group``/``chat_key``
        （群 id 或 @username）, ``filter``(all|spoke|spoke_no_admin，默认 spoke_no_admin),
        ``daily_cap_per_account``, ``scan_limit``（历史扫描条数=「发过言」口径）。

        多号：先解析各号活体 client，**只在可用号之间分片**（离线号跳过、不留空 shard）；
        每个可用号建一个 job（shard_index/num_shards + 同 batch_ref 便于归组），
        各自调度到自己的 pyro loop 上跑。全部号不可用 → 503。
        """
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        raw_accounts = body.get("account_ids")
        if isinstance(raw_accounts, list):
            accounts = [str(a).strip() for a in raw_accounts if str(a).strip()]
        else:
            accounts = [str(body.get("account_id") or "").strip()]
        # 去重保序
        seen = set()
        accounts = [a for a in accounts if a and not (a in seen or seen.add(a))]
        group = str(body.get("group") or body.get("chat_key") or "").strip()
        if not accounts or not group:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))

        from src.companion.group_members_store import _VALID_FILTERS, FILTER_SPOKE_NO_ADMIN
        filter_mode = str(body.get("filter") or FILTER_SPOKE_NO_ADMIN).strip()
        if filter_mode not in _VALID_FILTERS:
            raise HTTPException(400, tr(request, "err.gm.bad_filter"))
        try:
            daily_cap = int(body.get("daily_cap_per_account") or _default_cap())
            scan_limit = int(body.get("scan_limit") or _default_scan())
            _gc = body.get("group_daily_cap")
            group_cap = int(_gc if _gc is not None else _default_group_cap())
        except (TypeError, ValueError):
            daily_cap, scan_limit, group_cap = _default_cap(), _default_scan(), _default_group_cap()
        global_cap = _default_global_cap()

        # 先解析各号活体 client（复用多账号取数入口）——只在**可用号**之间分片
        try:
            from src.web.routes.unified_inbox_account_routes import _get_tg_pyro_for_account
        except Exception:
            logger.debug("[group_members] 导入 pyro 取数入口失败", exc_info=True)
            _get_tg_pyro_for_account = None
        resolved = []
        skipped: List[str] = []
        for acct in accounts:
            pyro = None
            if _get_tg_pyro_for_account is not None:
                try:
                    pyro = _get_tg_pyro_for_account(request.app, acct)
                except Exception:
                    pyro = None
            loop = getattr(pyro, "loop", None)
            if pyro is not None and loop is not None and loop.is_running():
                resolved.append((acct, pyro, loop))
            else:
                skipped.append(acct)
        if not resolved:
            raise HTTPException(503, tr(request, "err.gm.client_unavailable"))

        from src.companion.group_member_extract import run_extraction
        from src.companion.group_members_store import JOB_ERROR

        def _mk_done(jid: str):
            def _cb(fut) -> None:
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[group_members] 提取任务异常 job=%s: %s", jid, exc)
                    try:
                        st.update_job(jid, status=JOB_ERROR, last_error=str(exc)[:200])
                    except Exception:
                        pass
            return _cb

        batch_ref = "gmbatch_" + uuid.uuid4().hex[:12]
        num_shards = len(resolved)
        job_ids: List[str] = []
        for i, (acct, pyro, loop) in enumerate(resolved):
            job = st.create_job(
                group_id=group, account_ids=[acct], filter=filter_mode,
                daily_cap_per_account=daily_cap, scan_limit=scan_limit,
                created_by=_actor(request), shard_index=i, num_shards=num_shards,
                batch_ref=batch_ref, group_daily_cap=group_cap,
                global_daily_cap=global_cap)
            jid = job.get("job_id") or ""
            self_id = 0
            try:
                self_id = int(getattr(getattr(pyro, "me", None), "id", 0) or 0)
            except Exception:
                self_id = 0
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    run_extraction(pyro, st, jid, account=acct, shard_index=i,
                                   num_shards=num_shards, self_id=self_id), loop)
                fut.add_done_callback(_mk_done(jid))
                job_ids.append(jid)
            except Exception:
                logger.warning("[group_members] 调度提取失败 job=%s", jid, exc_info=True)
                st.update_job(jid, status=JOB_ERROR, last_error="schedule_failed")

        if not job_ids:
            raise HTTPException(503, tr(request, "err.gm.client_unavailable"))

        _audit(request, "tg_members_extract_start", group,
               "accounts=%s shards=%s filter=%s cap=%s" % (
                   ",".join(a for a, _, _ in resolved), num_shards, filter_mode, daily_cap))
        return {"ok": True, "batch_ref": batch_ref, "jobs": job_ids,
                "job_id": job_ids[0], "status": "running", "shards": num_shards,
                "skipped": skipped, "group": group, "filter": filter_mode}

    @app.get("/api/tg-members/jobs")
    async def list_extract_jobs(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        return {"jobs": st.list_jobs(limit=limit)}

    @app.get("/api/tg-members/jobs/{job_id}")
    async def get_extract_job(job_id: str, request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        job = st.get_job(str(job_id))
        if job is None:
            raise HTTPException(404, tr(request, "err.gm.job_not_found"))
        return {"job": job}

    @app.post("/api/tg-members/jobs/{job_id}/stop")
    async def stop_extract_job(job_id: str, request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        job = st.get_job(str(job_id))
        if job is None:
            raise HTTPException(404, tr(request, "err.gm.job_not_found"))
        st.request_stop(str(job_id))
        _audit(request, "tg_members_extract_stop", str(job_id))
        return {"ok": True}

    # ── 成员库 ───────────────────────────────────────────────────────────────

    @app.get("/api/tg-members/groups")
    async def list_member_groups(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        return {"groups": st.group_summaries()}

    @app.get("/api/tg-members/members")
    async def list_members(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        group_id = str(request.query_params.get("group_id") or "").strip()
        if not group_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        only = str(request.query_params.get("only") or "").strip()
        q = str(request.query_params.get("q") or "").strip()
        sort = str(request.query_params.get("sort") or "recent").strip()
        try:
            limit = int(request.query_params.get("limit") or 500)
            offset = int(request.query_params.get("offset") or 0)
        except (TypeError, ValueError):
            limit, offset = 500, 0
        items = st.list_members(group_id, only=only, q=q, sort=sort,
                                limit=limit, offset=offset)
        from src.companion.group_member_outreach import strip_access_hash
        items = [strip_access_hash(m) for m in items]
        return {"items": items,
                "total": st.count_members(group_id),
                "spoke": st.count_members(group_id, only="spoke"),
                "spoke_no_admin": st.count_members(group_id, only="spoke_no_admin")}

    @app.get("/api/tg-members/members/export")
    async def export_members(request: Request, _=Depends(auth_dep)):
        """导出某群成员为 CSV（供人工/下游导入）。"""
        _require_enabled(request)
        st = _require_store(request)
        group_id = str(request.query_params.get("group_id") or "").strip()
        if not group_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        only = str(request.query_params.get("only") or "").strip()
        rows = st.list_members(group_id, only=only, limit=5000)
        cols = ["user_id", "username", "first_name", "last_name",
                "is_admin", "spoke", "group_title", "extracted_at"]

        def _csv_cell(v: Any) -> str:
            s = "" if v is None else str(v)
            if any(c in s for c in [",", '"', "\n", "\r"]):
                s = '"' + s.replace('"', '""') + '"'
            return s

        lines = [",".join(cols)]
        for r in rows:
            lines.append(",".join(_csv_cell(r.get(c)) for c in cols))
        body = "\r\n".join(lines) + "\r\n"
        fname = "tg_members_%s.csv" % "".join(
            c for c in group_id if c.isalnum() or c in "_-")[:40]
        return Response(
            content=body, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=%s" % fname})

    @app.get("/api/tg-members/account-groups")
    async def account_groups(request: Request, _=Depends(auth_dep)):
        """列该号所在的群/超级群（管理台群下拉数据源；只读 get_dialogs，软失败回空）。"""
        _require_enabled(request)
        account_id = str(request.query_params.get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        try:
            from src.web.routes.unified_inbox_account_routes import _get_tg_pyro_for_account
            pyro = _get_tg_pyro_for_account(request.app, account_id)
        except Exception:
            pyro = None
        loop = getattr(pyro, "loop", None)
        if pyro is None or loop is None or not loop.is_running():
            raise HTTPException(503, tr(request, "err.gm.client_unavailable"))
        from src.companion.group_member_extract import list_account_groups
        try:
            groups = await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(list_account_groups(pyro), loop))
        except Exception:
            logger.debug("[group_members] 列群异常", exc_info=True)
            groups = []
        return {"groups": groups}

    # ── 配额水位（副驾卡/管理台读，判「今天还能拉多少」）──────────────────────

    @app.get("/api/tg-members/quota")
    async def extract_quota(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        account_id = str(request.query_params.get("account_id") or "").strip()
        cap = _default_cap()
        used = 0
        if account_id:
            from src.companion.group_member_extract import local_midnight_ts
            used = st.count_extracted_since(account_id, local_midnight_ts())
        return {"account_id": account_id, "cap": cap, "used_today": used,
                "remaining": max(0, cap - used)}

    # ── 同群开口（与提取配额分开；逐条确认，无后台循环）────────────────────

    _OUTREACH_HTTP = {
        "text": "err.gm.outreach_text",
        "paused": "err.gm.outreach_hold",
        "flood": "err.gm.outreach_hold",
        "hours": "err.gm.outreach_hours",
        "gate": "err.gm.outreach_gate",
        "cap": "err.gm.outreach_cap",
        "gap": "err.gm.outreach_gap",
        "state": "err.gm.outreach_state",
        "followup_off": "err.gm.outreach_followup_off",
        "followup_cap": "err.gm.outreach_followup_cap",
        "followup_state": "err.gm.outreach_followup_state",
        "followup_early": "err.gm.outreach_followup_early",
        "gtouch_cap": "err.gm.gtouch_cap",
        "gtouch_group_cap": "err.gm.gtouch_group_cap",
        "gtouch_state": "err.gm.gtouch_state",
        "gtouch_stale": "err.gm.gtouch_stale",
        "group_denied": "err.gm.gtouch_group_denied",
        "gtouch_text_empty": "err.gm.gtouch_text",
        "gtouch_text_too_long": "err.gm.gtouch_text",
        "gtouch_text_pitch": "err.gm.gtouch_text",
        "gtouch_text_dm_ask": "err.gm.gtouch_text",
        "gtouch_wait": "err.gm.gtouch_wait",
    }

    def _outreach_policy():
        from src.companion.group_member_outreach import OutreachPolicy
        return OutreachPolicy.from_config(_cfg())

    def _outreach_clock():
        import time as _time
        from src.companion.group_member_extract import local_midnight_ts
        now = _time.time()
        return now, local_midnight_ts(now)

    def _registry(request: Request):
        reg = getattr(request.app.state, "account_registry", None)
        if reg is not None:
            return reg
        try:
            from src.integrations.account_registry import get_account_registry
            return get_account_registry()
        except Exception:
            return None

    def _registry_row(request: Request, account_id: str):
        try:
            reg = _registry(request)
            return reg.get("telegram", str(account_id)) if reg is not None else None
        except Exception:
            return None

    def _account_age_days(request: Request, account_id: str, now: float):
        """号龄 = max(注册表 created_at 起算, 坐席申报)。都取不到回 None（按新号爬坡）。"""
        from src.companion.group_member_outreach import account_age_days
        return account_age_days(_registry_row(request, account_id), _store(), account_id, now)

    def _outreach_gate(request: Request, account_id: str, *, notify: bool):
        """总发送闸门（companion_send_gate + 急停 + 金丝雀）。返回 (blocked, reason)。

        开口按自动链口径（origin=auto）：冷开口是最先该让路的那种发送。
        """
        try:
            from src.integrations.shared.send_guard import send_blocked
            return send_blocked(
                "telegram", str(account_id), config=_full(),
                registry=_registry(request), chat_key="", notify=notify,
                origin="auto")
        except Exception:
            return False, ""

    def _outreach_gate_quota(request: Request, account_id: str):
        try:
            from src.inbox.send_gate_status import send_gate_snapshot
            snap = send_gate_snapshot(
                "telegram", str(account_id), config=_full(),
                registry=_registry(request), origin="auto")
        except Exception:
            snap = None
        if not snap:
            return None
        q = snap.get("quota") or {}
        return {
            "blocked": bool(snap.get("blocked")),
            "reason": str(snap.get("reason") or ""),
            "used": int(q.get("used") or 0),
            "cap": int(q.get("auto_cap") or q.get("cap") or 0),
            "light": str(q.get("light") or ""),
        }

    def _count_toward_send_gate(account_id: str, now: float) -> None:
        """成功开口也算这个号今天的一条外发（和自动回复同一个计数器）。"""
        try:
            from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
            get_autoreply_limiter(_full()).record_sent("telegram:%s" % account_id, now)
        except Exception:
            logger.debug("[gm_outreach] 总发送计数失败", exc_info=True)

    def _live_pyro(request: Request, account_id: str):
        try:
            from src.web.routes.unified_inbox_account_routes import _get_tg_pyro_for_account
            pyro = _get_tg_pyro_for_account(request.app, account_id)
        except Exception:
            pyro = None
        loop = getattr(pyro, "loop", None)
        if pyro is None or loop is None or not loop.is_running():
            return None, None
        return pyro, loop

    def _raise_outreach(request: Request, kind: str, http: int) -> None:
        key = _OUTREACH_HTTP.get(kind, "err.gm.bad_request")
        raise HTTPException(http, tr(request, key))

    @app.get("/api/tg-members/outreach/preview")
    async def outreach_preview(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        account_id = str(request.query_params.get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        from src.companion.group_member_outreach import build_preview
        now, since = _outreach_clock()
        ctx = _opener_ctx(request, account_id)
        out = build_preview(
            st, account_id, now=now, since_ts=since, policy=_outreach_policy(),
            age_days=_account_age_days(request, account_id, now), intent=ctx.get("intent"))
        out["gate"] = _outreach_gate_quota(request, account_id)
        from src.companion.group_member_outreach import account_age_days
        reg_age = account_age_days(_registry_row(request, account_id), None, account_id, now)
        declared = st.declared_age_days(account_id, now)
        out["age"] = {"registry_days": round(reg_age, 1) if reg_age is not None else None,
                      "declared_days": round(declared, 1) if declared is not None else None}
        out["persona"] = {"id": ctx.get("persona_id") or "", "name": ctx.get("persona_name") or ""}
        out["goal"] = {"template": ctx.get("goal_template") or "",
                       "name": ctx.get("goal_name") or ""}
        pol = _outreach_policy()
        out["settings"] = {"hours": [int(pol.hours_start), int(pol.hours_end)],
                           "daily_cap": int(pol.cap), "writable": _settings_writable()}
        from src.companion.group_member_outreach import (
            auto_mode_block_reason,
            followup_due_before,
        )
        out["auto_eligible"] = auto_mode_block_reason(
            st, account_id, now=now, age_days=_account_age_days(request, account_id, now),
            policy=pol)
        due = st.list_followup_due(account_id, followup_due_before(pol, now), limit=20) \
            if pol.followup_enabled else []
        from src.companion.group_member_outreach import public_member
        out["followups_due"] = [public_member(m) for m in due]
        out["followup"] = {"enabled": bool(pol.followup_enabled),
                           "after_hours": int(pol.followup_after_hours),
                           "close_after_hours": int(pol.followup_close_after_hours),
                           "daily_cap": int(pol.followup_daily_cap),
                           "used_today": st.count_followups_since(account_id, since)}
        return out

    @app.get("/api/tg-members/outreach/stats")
    async def outreach_stats(request: Request, _=Depends(auth_dep)):
        """回复率切片（文案来源 / 人设 / 小时 / 账号）+ 入库→排队→发出→回复漏斗。"""
        _require_enabled(request)
        st = _require_store(request)
        account_id = str(request.query_params.get("account_id") or "").strip()
        try:
            days = max(1, min(int(request.query_params.get("days") or 7), 90))
        except (TypeError, ValueError):
            days = 7
        now, _since = _outreach_clock()
        out = st.outreach_stats(now - days * 86400.0, account_id)
        out["days"] = days
        from src.companion.group_member_outreach import attach_won, public_member
        out["recent_replies"] = [public_member(m) for m in (out.get("recent_replies") or [])]
        out["won"] = None
        try:
            from src.companion.goals import service as goal_svc
            full = _full()
            if goal_svc.goals_enabled(full):
                contacted = st.contacted_replies(now - days * 86400.0, account_id)
                gs = goal_svc.get_configured_store(full, getattr(config_manager, "config_path", None))
                won = gs.won_chats("telegram", [str(m.get("user_id") or "") for m in contacted])
                attach_won(out, contacted, won)
        except Exception:
            logger.debug("[gm_outreach] 成交归因失败", exc_info=True)
        return out

    @app.post("/api/tg-members/outreach/followup")
    async def outreach_followup(request: Request, _=Depends(auth_dep)):
        """手动给一个发出 ≥N 小时没回音的人补一句（只此一次）。text 留空 → AI 拟。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        if body.get("confirm") is not True:
            raise HTTPException(400, tr(request, "err.gm.outreach_confirm"))
        account_id = str(body.get("account_id") or "").strip()
        group_id = str(body.get("group_id") or "").strip()
        user_id = str(body.get("user_id") or "").strip()
        text = " ".join(str(body.get("text") or "").split())
        if not account_id or not group_id or not user_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        from src.companion.group_member_opener import compose_followup
        from src.companion.group_member_outreach import (
            classify_send_error,
            deliver_outreach,
            finalize_followup,
            prepare_followup,
        )
        now, since = _outreach_clock()
        gate_blocked, gate_reason = _outreach_gate(request, account_id, notify=True)
        if gate_blocked:
            _audit(request, "tg_members_outreach_followup", account_id,
                   "group=%s user=%s kind=gate reason=%s" % (group_id, user_id, gate_reason))
            _raise_outreach(request, "gate", 409)
        source = "manual"
        if not text:
            m = st.get_member(group_id, user_id)
            if m is None:
                _raise_outreach(request, "followup_state", 409)
            got = await compose_followup(getattr(request.app.state, "ai_client", None),
                                         member=m, ctx=_opener_ctx(request, account_id))
            text = str(got.get("text") or "").strip()
            source = str(got.get("source") or "")
        prep = prepare_followup(
            st, account_id=account_id, group_id=group_id, user_id=user_id, text=text,
            now=now, since_ts=since, policy=_outreach_policy())
        if not prep.get("ok"):
            _raise_outreach(request, str(prep.get("kind") or ""), int(prep.get("http") or 409))
        pyro, loop = _live_pyro(request, account_id)
        if pyro is None:
            st.unclaim_followup(group_id, user_id)
            raise HTTPException(503, tr(request, "err.gm.client_unavailable"))
        try:
            sent = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                deliver_outreach(pyro, user_id=prep["user_id"], access_hash=prep["access_hash"],
                                 text=prep["text"]),
                loop))
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "kind": classify_send_error(exc)}
        done = finalize_followup(
            st, account_id=account_id, group_id=group_id, user_id=user_id, now=now,
            text=prep["text"],
            result=sent if isinstance(sent, dict) else {"ok": False, "kind": "retryable"})
        if done.get("ok"):
            _count_toward_send_gate(account_id, now)
        _audit(request, "tg_members_outreach_followup", account_id,
               "group=%s user=%s kind=%s source=%s" % (group_id, user_id, done.get("kind"), source))
        return {"ok": bool(done.get("ok")), "kind": done.get("kind"), "text": prep["text"],
                "source": source}

    def _opener_ctx(request: Request, account_id: str) -> Dict[str, Any]:
        from src.companion.group_member_opener import build_opener_context
        inbox = getattr(request.app.state, "inbox_store", None)
        try:
            return build_opener_context(_full(), "telegram", account_id, inbox,
                                        registry=_registry(request))
        except Exception:
            logger.debug("[gm_outreach] 开口上下文装配失败", exc_info=True)
            return {}

    def _settings_writable() -> bool:
        return config_manager is not None and hasattr(config_manager, "set_overlay_flag")

    async def _compose_for(request: Request, st, account_id: str, *, force: bool,
                           pairs=None, reset_manual: bool = False) -> Dict[str, Any]:
        from src.companion.group_member_opener import compose_queue
        now, since = _outreach_clock()
        ai = getattr(request.app.state, "ai_client", None)
        ctx = _opener_ctx(request, account_id)
        return await compose_queue(st, ai, account_id=account_id, ctx=ctx, since_ts=since,
                                   force=force, pairs=pairs, reset_manual=reset_manual)

    @app.post("/api/tg-members/outreach/compose")
    async def outreach_compose(request: Request, _=Depends(auth_dep)):
        """给队列里还没文案的人 AI 拟稿（按这个号的人设 + 默认目标）。不发送。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        account_id = str(body.get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        pairs = None
        items = body.get("items")
        if isinstance(items, list) and items:
            pairs = [(str(i.get("group_id") or ""), str(i.get("user_id") or ""))
                     for i in items if isinstance(i, dict)]
        reset_manual = body.get("reset_manual") is True and pairs is not None
        result = await _compose_for(request, st, account_id,
                                    force=body.get("force") is True, pairs=pairs,
                                    reset_manual=reset_manual)
        _audit(request, "tg_members_outreach_compose", account_id,
               "composed=%s ai=%s template=%s%s" % (result.get("composed"), result.get("ai"),
                                                    result.get("template"),
                                                    " reset_manual" if reset_manual else ""))
        result["ok"] = True
        return result

    @app.post("/api/tg-members/outreach/approve")
    async def outreach_approve(request: Request, _=Depends(auth_dep)):
        """批准：queued → approved。items 带 text 的先落坐席改过的文案；不带 items = 批整队。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        account_id = str(body.get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        from src.companion.group_member_outreach import opener_block_reason
        items = body.get("items")
        pairs = None
        if isinstance(items, list):
            pairs = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                gid = str(it.get("group_id") or "").strip()
                uid = str(it.get("user_id") or "").strip()
                if not gid or not uid:
                    continue
                if "text" in it:
                    text = " ".join(str(it.get("text") or "").split())
                    if opener_block_reason(text):
                        _raise_outreach(request, "text", 400)
                    st.set_opener(gid, uid, text, "manual", variant="")
                pairs.append((gid, uid))
            if not pairs:
                raise HTTPException(400, tr(request, "err.gm.bad_request"))
        else:
            # 批整队：没文案的先拟一遍，免得批了却发不出去
            await _compose_for(request, st, account_id, force=False)
        now, _since = _outreach_clock()
        n = st.approve_queued(account_id, now, pairs=pairs)
        _audit(request, "tg_members_outreach_approve", account_id, "approved=%d" % n)
        return {"ok": True, "approved": n}

    @app.post("/api/tg-members/outreach/skip")
    async def outreach_skip(request: Request, _=Depends(auth_dep)):
        """坐席跳过这个人（queued/approved → skipped），今天不再碰。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        group_id = str(body.get("group_id") or "").strip()
        user_id = str(body.get("user_id") or "").strip()
        if not group_id or not user_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        ok = st.skip_outreach(group_id, user_id)
        if not ok:
            _raise_outreach(request, "state", 409)
        _audit(request, "tg_members_outreach_skip", str(body.get("account_id") or ""),
               "group=%s user=%s" % (group_id, user_id))
        return {"ok": True}

    @app.post("/api/tg-members/outreach/mode")
    async def outreach_mode(request: Request, _=Depends(auth_dep)):
        """这个号的开口方式：manual（逐条点发）/ approve（批准后调度器发）/ auto（全自动）。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        account_id = str(body.get("account_id") or "").strip()
        mode = str(body.get("mode") or "").strip()
        from src.companion.group_members_store import OUTREACH_MODE_AUTO, OUTREACH_MODES
        if not account_id or mode not in OUTREACH_MODES:
            raise HTTPException(400, tr(request, "err.gm.outreach_mode"))
        if mode == OUTREACH_MODE_AUTO:
            from src.companion.group_member_outreach import auto_mode_block_reason
            now, _since = _outreach_clock()
            gate = auto_mode_block_reason(
                st, account_id, now=now, age_days=_account_age_days(request, account_id, now),
                policy=_outreach_policy())
            if not gate.get("ok"):
                _audit(request, "tg_members_outreach_mode", account_id,
                       "mode=auto refused reason=%s" % gate.get("reason"))
                raise HTTPException(409, tr(request, "err.gm.outreach_auto_gate_%s"
                                            % gate.get("reason")))
        st.set_outreach_mode(account_id, mode)
        _audit(request, "tg_members_outreach_mode", account_id, "mode=%s" % mode)
        return {"ok": True, "account_id": account_id, "mode": mode}

    @app.post("/api/tg-members/outreach/settings")
    async def outreach_settings(request: Request, _=Depends(auth_dep)):
        """时段 / 每日条数写进 overlay（companion.group_members.*），即时生效。"""
        _require_enabled(request)
        _require_write(request)
        if not _settings_writable():
            raise HTTPException(503, tr(request, "err.gm.outreach_settings_na"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        from src.companion.group_member_outreach import clamp_outreach_cap
        changed: Dict[str, Any] = {}
        hours = body.get("hours")
        if hours is not None:
            try:
                s, e = int(hours[0]), int(hours[1])
            except Exception:
                raise HTTPException(400, tr(request, "err.gm.bad_request"))
            if not (0 <= s <= 23 and 1 <= e <= 24 and s < e):
                raise HTTPException(400, tr(request, "err.gm.bad_request"))
            changed["outreach_hours"] = [s, e]
        if body.get("daily_cap") is not None:
            try:
                cap = clamp_outreach_cap(int(body.get("daily_cap")))
            except Exception:
                raise HTTPException(400, tr(request, "err.gm.bad_request"))
            changed["outreach_daily_cap"] = cap
        if not changed:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        for k, v in changed.items():
            ok, note = config_manager.set_overlay_flag("companion.group_members.%s" % k, v)
            if not ok:
                logger.warning("[gm_outreach] overlay 写入失败 %s: %s", k, note)
                raise HTTPException(503, tr(request, "err.gm.outreach_settings_na"))
        _audit(request, "tg_members_outreach_settings", "",
               " ".join("%s=%s" % (k, v) for k, v in changed.items()))
        pol = _outreach_policy()
        return {"ok": True, "hours": [int(pol.hours_start), int(pol.hours_end)],
                "daily_cap": int(pol.cap)}

    @app.post("/api/tg-members/outreach/queue")
    async def outreach_queue(request: Request, _=Depends(auth_dep)):
        """生成今日队列（只标 queued，不发送）。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        from src.companion.group_member_outreach import enqueue_today
        now, since = _outreach_clock()
        result = enqueue_today(
            st, account_id, now=now, since_ts=since, policy=_outreach_policy(),
            age_days=_account_age_days(request, account_id, now),
            intent=_opener_ctx(request, account_id).get("intent"))
        if not result.get("ok"):
            _raise_outreach(request, str(result.get("kind") or ""), 409)
        _audit(request, "tg_members_outreach_queue", account_id,
               "queued=%s" % result.get("queued_now"))
        return result

    @app.post("/api/tg-members/outreach/release")
    async def outreach_release(request: Request, _=Depends(auth_dep)):
        """对队列里的一个人发一条。confirm 必须是 JSON true，没有默认发送。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        if body.get("confirm") is not True:
            raise HTTPException(400, tr(request, "err.gm.outreach_confirm"))
        account_id = str(body.get("account_id") or "").strip()
        group_id = str(body.get("group_id") or "").strip()
        user_id = str(body.get("user_id") or "").strip()
        text = str(body.get("text") or "")
        if not account_id or not group_id or not user_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        from src.companion.group_member_outreach import (
            classify_send_error,
            deliver_outreach,
            finalize_release,
            prepare_release,
        )
        from src.companion.group_members_store import OUTREACH_QUEUED, OUTREACH_SENDING
        now, since = _outreach_clock()
        # 坐席在卡片里改过的文案顺手落库（manual 永不被 AI 重拟覆盖）；原样发 AI 稿不改标签
        clean = " ".join(text.split())
        if clean:
            cur = st.get_member(group_id, user_id) or {}
            if clean != str(cur.get("opener_text") or ""):
                st.set_opener(group_id, user_id, clean, "manual", variant="")
        # 总发送闸门先于占坑：被拦时这个人原地留在队列，不翻状态
        gate_blocked, gate_reason = _outreach_gate(request, account_id, notify=True)
        if gate_blocked:
            _audit(request, "tg_members_outreach_send", account_id,
                   "group=%s user=%s kind=gate reason=%s" % (group_id, user_id, gate_reason))
            _raise_outreach(request, "gate", 409)
        prep = prepare_release(
            st, account_id=account_id, group_id=group_id, user_id=user_id,
            text=text, now=now, since_ts=since, policy=_outreach_policy(),
            age_days=_account_age_days(request, account_id, now),
        )
        if not prep.get("ok"):
            _raise_outreach(request, str(prep.get("kind") or ""), int(prep.get("http") or 409))
        pyro, loop = _live_pyro(request, account_id)
        if pyro is None:
            st.cas_outreach(
                group_id, user_id, expect_states=(OUTREACH_SENDING,),
                new_state=str(prep.get("prev_state") or OUTREACH_QUEUED),
                account_id=account_id, error="no_client", now=now,
            )
            raise HTTPException(503, tr(request, "err.gm.client_unavailable"))
        try:
            sent = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                deliver_outreach(
                    pyro, user_id=prep["user_id"], access_hash=prep["access_hash"],
                    text=prep["text"]),
                loop))
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "kind": classify_send_error(exc)}
        done = finalize_release(
            st, account_id=account_id, group_id=group_id, user_id=user_id,
            now=now, text=prep["text"],
            result=sent if isinstance(sent, dict) else {"ok": False, "kind": "retryable"},
        )
        if done.get("ok"):
            _count_toward_send_gate(account_id, now)
        _audit(request, "tg_members_outreach_send", account_id,
               "group=%s user=%s kind=%s" % (group_id, user_id, done.get("kind")))
        return {"ok": bool(done.get("ok")), "kind": done.get("kind")}

    @app.post("/api/tg-members/outreach/age")
    async def outreach_age(request: Request, _=Depends(auth_dep)):
        """申报这个 TG 号的真实号龄（天）：老号切到销售人设时不用再从每天 3 条爬。0 = 撤销。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "").strip()
        try:
            days = float((body or {}).get("days"))
        except (TypeError, ValueError):
            days = -1.0
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        if not (0.0 <= days <= 3650.0):
            raise HTTPException(400, tr(request, "err.gm.outreach_age"))
        now, _since = _outreach_clock()
        saved = st.set_declared_age(account_id, days, now)
        _audit(request, "tg_members_outreach_age", account_id, "days=%s" % saved)
        return {"ok": True, "account_id": account_id, "declared_days": saved,
                "age_days": _account_age_days(request, account_id, now)}

    # ── 群里接话（公开回复 TA 在群里那句；只逐条手动发）──────────────────────────

    def _gtouch_args(body: Any):
        b = body if isinstance(body, dict) else {}
        return (str(b.get("account_id") or "").strip(), str(b.get("group_id") or "").strip(),
                str(b.get("user_id") or "").strip())

    @app.get("/api/tg-members/gtouch/preview")
    async def gtouch_preview_route(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        account_id = str(request.query_params.get("account_id") or "").strip()
        if not account_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        from src.companion.group_member_gtouch import gtouch_preview
        from src.companion.group_member_opener import persona_public_ai
        now, since = _outreach_clock()
        ctx = _opener_ctx(request, account_id)
        out = gtouch_preview(st, account_id, now=now, since_ts=since, policy=_outreach_policy(),
                             intent=ctx.get("intent"))
        out["persona"] = {"id": ctx.get("persona_id") or "", "name": ctx.get("persona_name") or "",
                          "public_ai": persona_public_ai(ctx.get("persona"))}
        return out

    @app.post("/api/tg-members/gtouch/compose")
    async def gtouch_compose(request: Request, _=Depends(auth_dep)):
        """AI 给一个人拟群里公开回复并存成草稿。拟不出 → text 空，坐席手写。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id, group_id, user_id = _gtouch_args(body)
        if not account_id or not group_id or not user_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        row = st.get_member(group_id, user_id)
        if row is None or str(row.get("gtouch_state") or "") not in ("", "drafted"):
            _raise_outreach(request, "gtouch_state", 409)
        from src.companion.group_member_gtouch import compose_gtouch
        ctx = _opener_ctx(request, account_id)
        got = await compose_gtouch(getattr(request.app.state, "ai_client", None), member=row, ctx=ctx)
        if got.get("text"):
            st.set_gtouch_draft(group_id, user_id, got["text"])
        return {"ok": bool(got.get("text")), "text": got.get("text") or "",
                "reason": got.get("reason") or ""}

    @app.post("/api/tg-members/gtouch/skip")
    async def gtouch_skip(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id, group_id, user_id = _gtouch_args(body)
        if not group_id or not user_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        ok = st.skip_gtouch(group_id, user_id)
        _audit(request, "tg_members_gtouch_skip", account_id, "group=%s user=%s" % (group_id, user_id))
        return {"ok": ok}

    @app.post("/api/tg-members/gtouch/send")
    async def gtouch_send(request: Request, _=Depends(auth_dep)):
        """把一条公开回复发到群里（挂在 TA 那条下面）。逐条确认；闸门同开口。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if (body or {}).get("confirm") is not True:
            raise HTTPException(400, tr(request, "err.gm.outreach_confirm"))
        account_id, group_id, user_id = _gtouch_args(body)
        if not account_id or not group_id or not user_id:
            raise HTTPException(400, tr(request, "err.gm.bad_request"))
        text = str((body or {}).get("text") or "")
        from src.companion.group_member_gtouch import (
            classify_group_send_error,
            deliver_gtouch,
            finalize_gtouch,
            prepare_gtouch,
        )
        now, since = _outreach_clock()
        gate_blocked, gate_reason = _outreach_gate(request, account_id, notify=True)
        if gate_blocked:
            _audit(request, "tg_members_gtouch_send", account_id,
                   "group=%s user=%s kind=gate reason=%s" % (group_id, user_id, gate_reason))
            _raise_outreach(request, "gate", 409)
        prep = prepare_gtouch(st, account_id=account_id, group_id=group_id, user_id=user_id,
                              text=text, now=now, since_ts=since, policy=_outreach_policy())
        if not prep.get("ok"):
            _raise_outreach(request, str(prep.get("kind") or ""), int(prep.get("http") or 409))
        pyro, loop = _live_pyro(request, account_id)
        if pyro is None:
            st.finish_gtouch(group_id, user_id, state="drafted", error="no_client")
            raise HTTPException(503, tr(request, "err.gm.client_unavailable"))
        try:
            sent = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                deliver_gtouch(pyro, chat_id=prep["chat_id"], reply_to=prep["reply_to"],
                               text=prep["text"]), loop))
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "kind": classify_group_send_error(exc)}
        done = finalize_gtouch(
            st, account_id=account_id, group_id=group_id, user_id=user_id, now=now,
            text=prep["text"],
            result=sent if isinstance(sent, dict) else {"ok": False, "kind": "retryable"})
        if done.get("ok"):
            _count_toward_send_gate(account_id, now)
        _audit(request, "tg_members_gtouch_send", account_id,
               "group=%s user=%s kind=%s" % (group_id, user_id, done.get("kind")))
        return {"ok": bool(done.get("ok")), "kind": done.get("kind"),
                "requeued": int(done.get("requeued") or 0),
                "dm_after_hours": int(_outreach_policy().gtouch_dm_after_hours)}

    @app.post("/api/tg-members/outreach/stop")
    async def outreach_stop(request: Request, _=Depends(auth_dep)):
        """今天停止开口。account_id 空或 * = 全部号。不停提取。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = str((body or {}).get("account_id") or "").strip()
        from src.companion.group_members_store import OUTREACH_HOLD_ALL
        target = OUTREACH_HOLD_ALL if raw in ("", "*") else raw
        st.set_hold(target, paused=True, reason="operator_stop")
        _audit(request, "tg_members_outreach_stop", target)
        return {"ok": True, "account_id": target}

    @app.post("/api/tg-members/outreach/resume")
    async def outreach_resume(request: Request, _=Depends(auth_dep)):
        """解除人工急停。风控熔断不在这里清，等到点。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = str((body or {}).get("account_id") or "").strip()
        from src.companion.group_members_store import OUTREACH_HOLD_ALL
        if raw in ("", "*"):
            st.clear_outreach_pause("")
            st.clear_outreach_pause(OUTREACH_HOLD_ALL)
            target = OUTREACH_HOLD_ALL
        else:
            st.clear_outreach_pause(raw)
            target = raw
        _audit(request, "tg_members_outreach_resume", target)
        return {"ok": True, "account_id": target}

    # ── 管理台整页（独立自包含 HTML；号多选/群/过滤/配额 + 任务进度 + 成员表格/导出）──

    @app.get("/tools/tg-members", response_class=HTMLResponse)
    async def tg_members_page(request: Request, _=Depends(html_auth)):
        """群成员提取管理台（独立自包含页）。

        刻意不 gate enabled——让管理员总能打开页面（未开启时页内探测 quota 得 403 → 显示提示
        横幅）；写操作 API 各自受 enabled + viewer 门控。返回独立模板原文（无 Jinja 变量，
        HTMLResponse 直出）；CSRF 安全方法中间件顺带下发 csrf_token cookie 供页内 POST 复用。
        """
        from pathlib import Path as _Path
        tpl = _Path(__file__).resolve().parents[1] / "templates" / "tg_members.html"
        try:
            html = tpl.read_text(encoding="utf-8")
        except Exception:
            logger.warning("[group_members] 管理台模板读取失败", exc_info=True)
            raise HTTPException(500, tr(request, "err.gm.store_unavailable"))
        return HTMLResponse(content=html)
