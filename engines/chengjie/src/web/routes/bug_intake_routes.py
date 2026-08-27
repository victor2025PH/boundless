# -*- coding: utf-8 -*-
"""报障群工单管理路由（bug_intake，2026-08-18）。

- GET  /api/admin/bug-intake                 — 工单列表 + 值守统计
- GET  /api/admin/bug-intake/{ticket_id}/shots      — 截图附件清单（2026-08-27）
- GET  /api/admin/bug-intake/{ticket_id}/shot/{name} — 取单张截图原图
- POST /api/admin/bug-intake/{ticket_id}/status — 工单状态流转
  （new→confirmed→in_progress→fixed→verified→closed，见 bug_intake.VALID_STATUSES）
- POST /api/admin/bug-intake/{ticket_id}/notify — 修复回访重发（fixed 时自动
  尝试一次；账号离线等失败不阻塞状态流转，经此端点显式重试）

台账/统计逻辑全在 ``src.ops.bug_intake``（单一事实源），这里是薄路由；
群内发送复用 tg-join 同款「按账号取 worker pyro client + 跨 loop 调度」姿势。
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_NOTIFY_TIMEOUT_SEC = 20.0

# 截图文件名白名单：服务端生成的形状是 shot_<序号>.<png|jpg>（见
# assistant_routes 附件落盘段）。用白名单而非黑名单做路径安全——不匹配即 404。
_SHOT_NAME_RE = re.compile(r"^shot_\d{1,4}\.(?:png|jpg|jpeg)$", re.I)


async def _send_group_notify(app, row) -> tuple[bool, str]:
    """把修复回访文案发进工单所属群。返回 (ok, note)。

    失败语义＝如实返回不抛（状态流转不被通知失败绑架）；账号语义与 tg-join
    一致：**只**用工单归属账号的在线 worker client，绝不回落别的账号代发。
    """
    from src.ops.bug_intake import build_fix_notify_text
    from src.web.routes.unified_inbox_tg_join_routes import _tg_join_pyro
    account_id = str(row.get("account_id") or "")
    try:
        chat_id = int(str(row.get("chat_id") or "").strip())
    except (TypeError, ValueError):
        return False, "bad_chat_id"
    pyro = _tg_join_pyro(app, account_id)
    loop = getattr(pyro, "loop", None)
    if pyro is None or loop is None or not loop.is_running():
        return False, "worker_offline"
    text = build_fix_notify_text(row)

    async def _do():
        try:
            from pyrogram import enums
            return await pyro.send_message(
                chat_id, text, parse_mode=enums.ParseMode.HTML)
        except ImportError:
            return await pyro.send_message(chat_id, text)

    fut = asyncio.run_coroutine_threadsafe(_do(), loop)
    try:
        await asyncio.wait_for(
            asyncio.wrap_future(fut), timeout=_NOTIFY_TIMEOUT_SEC)
        return True, ""
    except (TimeoutError, asyncio.TimeoutError):
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def register_bug_intake_routes(app, *, api_auth) -> None:
    """挂载报障群工单管理端点（管理员 API 鉴权，与 cases 面板同责任边界）。"""

    @app.get("/api/admin/bug-intake")
    async def api_bug_intake_list(
        request: Request, status: str = "", limit: int = 100,
    ):
        api_auth(request)
        from src.ops import bug_intake
        return {
            "ok": True,
            "tickets": bug_intake.list_tickets(status=status, limit=limit),
            "stats": bug_intake.dump_stats(),
            "valid_statuses": list(bug_intake.VALID_STATUSES),
        }

    @app.get("/api/admin/bug-intake/{ticket_id}/shots")
    async def api_bug_intake_shots(ticket_id: int, request: Request):
        """列出该工单的截图附件（文件名 + 字节数 + 取图 URL）。

        补的是报障闭环里一个很实在的断点：小智报障的截图落在
        ``{config_dir}/assistant_reports/{ticket_id}/shot_N.{png|jpg}``，
        magic bytes 校验过、VLM 也可能读过，**但没有任何端点能把它取出来**
        ——客服收到工单只能看文字描述，要看截图得上服务器翻目录。而截图恰恰
        是报障里信息密度最高的东西（用户圈了红框、打了马赛克才发出来的）。
        """
        api_auth(request)
        from src.web.routes.assistant_routes import _reports_dir

        out = []
        try:
            d = _reports_dir() / str(int(ticket_id))
            if d.is_dir():
                for fp in sorted(d.glob("shot_*")):
                    if fp.is_file() and _SHOT_NAME_RE.match(fp.name):
                        out.append({
                            "name": fp.name,
                            "bytes": fp.stat().st_size,
                            "url": (f"/api/admin/bug-intake/{int(ticket_id)}"
                                    f"/shot/{fp.name}"),
                        })
        except Exception:
            logger.debug("[bug_intake] 截图列举失败（忽略）", exc_info=True)
        return {"ok": True, "ticket_id": int(ticket_id), "shots": out}

    @app.get("/api/admin/bug-intake/{ticket_id}/shot/{name}")
    async def api_bug_intake_shot(ticket_id: int, name: str, request: Request):
        """取单张截图原图。

        路径安全＝**白名单正则**而非黑名单转义：文件名由服务端生成
        （``shot_<n>.<ext>``），凡不匹配该形状一律 404，`..`/绝对路径/
        分隔符天然不可能通过；再叠一次 resolve 后的父目录归属校验兜底。
        """
        api_auth(request)
        from fastapi.responses import FileResponse

        from src.web.routes.assistant_routes import _reports_dir

        if not _SHOT_NAME_RE.match(str(name or "")):
            raise HTTPException(404, tr(request, "err.bug.shot_not_found"))
        base = (_reports_dir() / str(int(ticket_id))).resolve()
        fp = (base / name).resolve()
        if fp.parent != base or not fp.is_file():
            raise HTTPException(404, tr(request, "err.bug.shot_not_found"))
        media = "image/png" if fp.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(str(fp), media_type=media)

    @app.post("/api/admin/bug-intake/{ticket_id}/status")
    async def api_bug_intake_set_status(ticket_id: int, request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        status = str((body or {}).get("status") or "").strip()
        from src.ops import bug_intake
        if status not in bug_intake.VALID_STATUSES:
            raise HTTPException(400, tr(
                request, "err.ws.field_required", field="status"))
        if not bug_intake.set_ticket_status(int(ticket_id), status):
            raise HTTPException(404, tr(request, "err.case.not_found"))
        out = {"ok": True, "ticket_id": int(ticket_id), "status": status}
        # 修复回访（P2）：标 fixed 自动尝试一次群内 @报障人；失败不阻塞流转，
        # notify_note 记原因，可经 /notify 端点重试。
        if status == "fixed":
            row = bug_intake.get_ticket(int(ticket_id)) or {}
            ok, note = await _send_group_notify(request.app, row)
            bug_intake.mark_notified(int(ticket_id), ok, note)
            out["notified"] = ok
            if not ok:
                out["notify_note"] = note
        return out

    @app.post("/api/admin/bug-intake/{ticket_id}/notify")
    async def api_bug_intake_notify(ticket_id: int, request: Request):
        api_auth(request)
        from src.ops import bug_intake
        row = bug_intake.get_ticket(int(ticket_id))
        if not row:
            raise HTTPException(404, tr(request, "err.case.not_found"))
        ok, note = await _send_group_notify(request.app, row)
        bug_intake.mark_notified(int(ticket_id), ok, note)
        if not ok:
            raise HTTPException(502, tr(
                request, "err.inbox.send_not_delivered", msg=note or "notify"))
        return {"ok": True, "ticket_id": int(ticket_id), "notified": True}

    @app.post("/api/admin/bug-intake/notify-pending")
    async def api_bug_intake_notify_pending(request: Request):
        """回访积压批量冲刷：worker 离线期标 fixed 的单，账号回来后一键补发。

        逐单串行（条间 1.5s 防连发刷屏），单条失败不中止整批、如实回列表。
        """
        api_auth(request)
        from src.ops import bug_intake
        rows = bug_intake.list_pending_notify(limit=20)
        results = []
        for row in rows:
            ok, note = await _send_group_notify(request.app, row)
            bug_intake.mark_notified(int(row["id"]), ok, note)
            results.append({"ticket_id": int(row["id"]), "notified": ok,
                            "note": note})
            if ok and len(rows) > 1:
                await asyncio.sleep(1.5)
        return {"ok": True, "total": len(rows), "results": results}
