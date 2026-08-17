"""Telegram 群成员提取 —— 后台 API。

挂 ``/api/tg-members/*``。职责：建/查/停「提取任务」、列成员库、CSV 导出、每日配额水位。
提取本体是 async 协程，**调度到该号 pyrogram client 自己的事件循环上**（web loop 与
pyro loop 不同 → ``run_coroutine_threadsafe``），不阻塞 web 请求；进度写任务行，前端轮询。

只读提取（低危）；真正高危的「私聊触达」不在本模块（后续独立步骤 + 人工闸）。
写操作（建/停任务）viewer 角色 403；全链受 ``companion.group_members.enabled`` 总闸。
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

    def _cfg() -> Dict[str, Any]:
        try:
            full = (config_manager.config if config_manager is not None else {}) or {}
            return ((full.get("companion") or {}).get("group_members") or {})
        except Exception:
            return {}

    def _enabled() -> bool:
        return bool(_cfg().get("enabled", False))

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
