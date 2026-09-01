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

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_NOTIFY_TIMEOUT_SEC = 20.0

# 截图文件名白名单：服务端生成的形状是 shot_<序号>.<png|jpg>（见
# assistant_routes 附件落盘段）。用白名单而非黑名单做路径安全——不匹配即 404。
_SHOT_NAME_RE = re.compile(r"^shot_\d{1,4}\.(?:png|jpg|jpeg)$", re.I)


async def _send_group_text(app, row, text: str,
                           buttons=None) -> tuple[bool, str, int]:
    """把任意 HTML 文案发进工单所属群。返回 (ok, note, message_id)。

    发送身份（实施82 P0，2026-08-29 老板拍板）：**官方 bot 优先**（能带品牌身份
    与 inline 按钮；不与官网 webhook 冲突——只出站），bot 不可用（未配 token/
    未进该群/网络失败/开关关）→ **回落 pyrogram 用户账号**（旧链原样保留，
    客户部署零 bot 也不劣化）。message_id 仅 bot 链可得（pyro 回落记 0）。
    ``buttons``＝inline 键盘（bot 专属；回落链发不出按钮，文案里的文字口令
    引导就是为这个降级面准备的）。

    失败语义＝如实返回不抛（状态流转不被通知失败绑架）；回落链账号语义与
    tg-join 一致：**只**用工单归属账号的在线 worker client，绝不换号代发。
    """
    try:
        chat_id = int(str(row.get("chat_id") or "").strip())
    except (TypeError, ValueError):
        return False, "bad_chat_id", 0

    # ① bot 链（config 开关 bug_intake.bot_send 默认开）
    bot_note = ""
    try:
        from src.ops import bug_bot
        cfg = getattr(getattr(app.state, "config_manager", None),
                      "config", None) or {}
        if bug_bot.bot_send_enabled(cfg) and bug_bot.bot_token():
            ok, mid, note = await asyncio.to_thread(
                lambda: bug_bot.send_group_html(chat_id, text,
                                                buttons=buttons))
            if ok:
                return True, "bot", int(mid or 0)
            bot_note = f"bot_fail:{note}"
            logger.info("[bug_intake] bot 发送失败回落用户账号：%s", note)
    except Exception as exc:  # noqa: BLE001
        bot_note = f"bot_err:{type(exc).__name__}"
        logger.debug("[bug_intake] bot 链异常（回落用户账号）", exc_info=True)

    # ② pyrogram 用户账号回落（旧链）
    from src.web.routes.unified_inbox_tg_join_routes import _tg_join_pyro
    account_id = str(row.get("account_id") or "")
    pyro = _tg_join_pyro(app, account_id)
    loop = getattr(pyro, "loop", None)
    if pyro is None or loop is None or not loop.is_running():
        return False, (bot_note + ";worker_offline").lstrip(";"), 0

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
        return True, (bot_note + ";pyro").lstrip(";"), 0
    except (TimeoutError, asyncio.TimeoutError):
        return False, (bot_note + ";timeout").lstrip(";"), 0
    except Exception as exc:  # noqa: BLE001
        return False, (bot_note + ";" + type(exc).__name__).lstrip(";"), 0


async def _send_group_notify(app, row,
                             update_hint: str = "") -> tuple[bool, str, int]:
    """修复回访（文案=build_fix_notify_text；实施81 起可带「获取方式」段）。

    实施82 P2：bot 链自动挂验证键盘（✅ 修好了 / ❌ 还是不行）——回调经官网
    webhook 落盘、117 duty_callback_poll 拉回写工单；pyro 回落链无按钮，
    文案里的文字口令（好了/还是不行）继续兜底。
    """
    from src.ops.bug_intake import build_fix_notify_text
    buttons = None
    try:
        from src.ops.bug_bot import build_verify_keyboard
        buttons = build_verify_keyboard(row.get("id"), row.get("reporter_id"))
    except Exception:
        buttons = None
    return await _send_group_text(
        app, row, build_fix_notify_text(row, update_hint=update_hint),
        buttons=buttons)


def register_bug_intake_routes(app, *, api_auth, page_auth=None,
                               config_manager=None) -> None:
    """挂载报障群工单管理端点（管理员 API 鉴权，与 cases 面板同责任边界）。

    ``page_auth``/``config_manager`` 为实施81 P0-2 增量（处置页 + update_hint
    配置读取）——旧调用姿势（只传 api_auth）保持可用：页面路由不注册、
    update_hint 恒空，API 行为与 2026-08-18 版一致。
    """

    def _cfg() -> dict:
        return getattr(config_manager, "config", None) or {}

    @app.get("/api/admin/bug-intake")
    async def api_bug_intake_list(
        request: Request, status: str = "", limit: int = 100,
    ):
        api_auth(request)
        from src.ops import bug_intake
        # bot_guard（实施82）＝能力探测旗：本引擎已装载「官方 bot 自咬环守卫 +
        # bot 代发」。duty_known_issues --via bot 据此判断能否安全用 bot 发公示
        #（守卫未装载时 bot 发的公示会被登记链当新报障——CLI 见旗才放行）。
        return {
            "ok": True,
            "tickets": bug_intake.list_tickets(status=status, limit=limit),
            "stats": bug_intake.dump_stats(),
            "valid_statuses": list(bug_intake.VALID_STATUSES),
            "update_hint": bug_intake.resolve_update_hint(_cfg()),
            "bot_guard": True,
        }

    if page_auth is not None:
        # 处置页（实施74 §6.1 推迟的「客服工单处置页」，实施81 落地）：
        # 列表 + 详情 + 截图内嵌 + 状态流转 + 一键回访 + 群内回复。
        # templates 走 admin 单例（contacts_routes._render_ops_page 同款 lazy
        # import，规避 routes ↔ admin 循环依赖）。
        @app.get("/admin/bug-tickets")
        async def bug_tickets_page(request: Request, _=Depends(page_auth)):
            from src.web.admin import templates
            return templates.TemplateResponse(
                request, "bug_tickets.html", {"request": request})

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
        body = body if isinstance(body, dict) else {}
        status = str(body.get("status") or "").strip()
        from src.ops import bug_intake
        if status not in bug_intake.VALID_STATUSES:
            raise HTTPException(400, tr(
                request, "err.ws.field_required", field="status"))
        # fix_note（P1-4）：标 fixed 顺手写「本次改动」，回访文案引用；
        # 空值不动既有说明（重试流转不许抹掉已写内容）。
        fix_note = str(body.get("fix_note") or "").strip()
        if not bug_intake.set_ticket_status(int(ticket_id), status,
                                            fix_note=fix_note or None):
            raise HTTPException(404, tr(request, "err.case.not_found"))
        out = {"ok": True, "ticket_id": int(ticket_id), "status": status}
        # 修复回访（P2）：标 fixed 自动尝试一次群内 @报障人；失败不阻塞流转，
        # notify_note 记原因，可经 /notify 端点重试。
        if status == "fixed":
            row = bug_intake.get_ticket(int(ticket_id)) or {}
            hint = (str(body.get("update_hint") or "").strip()
                    or bug_intake.resolve_update_hint(_cfg()))
            ok, note, mid = await _send_group_notify(request.app, row,
                                                     update_hint=hint)
            bug_intake.mark_notified(int(ticket_id), ok, note, msg_id=mid)
            out["notified"] = ok
            if not ok:
                out["notify_note"] = note
        return out

    @app.post("/api/admin/bug-intake/{ticket_id}/notify")
    async def api_bug_intake_notify(ticket_id: int, request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        from src.ops import bug_intake
        row = bug_intake.get_ticket(int(ticket_id))
        if not row:
            raise HTTPException(404, tr(request, "err.case.not_found"))
        hint = (str(body.get("update_hint") or "").strip()
                or bug_intake.resolve_update_hint(_cfg()))
        ok, note, mid = await _send_group_notify(request.app, row,
                                                 update_hint=hint)
        bug_intake.mark_notified(int(ticket_id), ok, note, msg_id=mid)
        if not ok:
            raise HTTPException(502, tr(
                request, "err.inbox.send_not_delivered", msg=note or "notify"))
        return {"ok": True, "ticket_id": int(ticket_id), "notified": True}

    @app.post("/api/admin/bug-intake/{ticket_id}/reply")
    async def api_bug_intake_reply(ticket_id: int, request: Request):
        """处置台「群内回复」（实施81 P0-2）：@报障人 + 正文 + 工单号 footer，
        用工单归属账号发进原群；成功后回写工单 note（回复即台账，值守不再
        另记 markdown）。可选 ``status`` 顺手流转（如答疑完直接 confirmed）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, tr(
                request, "err.ws.field_required", field="text"))
        from src.ops import bug_intake
        row = bug_intake.get_ticket(int(ticket_id))
        if not row:
            raise HTTPException(404, tr(request, "err.case.not_found"))
        mention = bool(body.get("mention", True))
        payload = bug_intake.build_reply_text(row, text, mention=mention)
        ok, note, _mid = await _send_group_text(request.app, row, payload)
        if not ok:
            raise HTTPException(502, tr(
                request, "err.inbox.send_not_delivered", msg=note or "reply"))
        bug_intake.append_ticket_note(int(ticket_id), f"[值守回复] {text}")
        out = {"ok": True, "ticket_id": int(ticket_id), "sent": True}
        status = str(body.get("status") or "").strip()
        if status and status in bug_intake.VALID_STATUSES:
            bug_intake.set_ticket_status(
                int(ticket_id), status,
                fix_note=str(body.get("fix_note") or "").strip() or None)
            out["status"] = status
        return out

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
            hint = ""
            try:
                from src.ops.bug_intake import resolve_update_hint
                hint = resolve_update_hint(_cfg())
            except Exception:
                hint = ""
            ok, note, mid = await _send_group_notify(request.app, row,
                                                     update_hint=hint)
            bug_intake.mark_notified(int(row["id"]), ok, note, msg_id=mid)
            results.append({"ticket_id": int(row["id"]), "notified": ok,
                            "note": note})
            if ok and len(rows) > 1:
                await asyncio.sleep(1.5)
        return {"ok": True, "total": len(rows), "results": results}
