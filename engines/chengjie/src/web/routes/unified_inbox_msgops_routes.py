"""统一收件箱——官方级消息管理路由域（2026-08-17 P0/P1）。

boss 需求「消息列表要有和官方一样的功能」的服务端半边：

- ``POST /api/unified-inbox/conversations/pin``：会话置顶/取消置顶（工作台全局
  语义，全坐席共见；服务端落库替代 localStorage——换电脑/清缓存不丢）。
- ``POST /api/unified-inbox/conversations/clear``：清空聊天记录（默认**仅工作台**，
  批量软删；客户设备上的对话不受影响）。supervisor+（与删除会话同闸）。
  ``both_sides: true``（2026-08-17 下）＝**连对方设备一起清空**——仅 Telegram
  私聊（唯一官方支持整段历史双向删除的平台，raw ``messages.DeleteHistory
  (revoke=True)`` 经编排器 ``delete_history``）；远端失败则本地也不动
  （半成功比失败更糟——坐席以为清了、对方那份还在）。能力经 meta
  ``clear_remote_platforms`` 探测；审计 kind=``conv_clear_remote`` 成败双记。
- ``POST /api/unified-inbox/messages/delete``：删除若干条消息（**仅工作台**软删，
  前台消失、后台留痕可审计）。全员可用（清理自己视图不是破坏性操作）。
- ``POST /api/unified-inbox/messages/restore``：撤销上一步（undo toast 的服务端
  退路——先真提交再真恢复，跨窗口/跨刷新一致，不靠前端定时器活着）。
- ``GET  /api/unified-inbox/message-ops/meta``：能力探测（feat 特性探测约定，
  与 reply-settings 守卫卡同模式）——模板热更新先于重启上线的中间态必须自洽：
  前端拿不到本端点（旧后端 404）就整套新 UI 不挂，绝不出现「菜单点了 404」。
  同时它是**平台撤回能力的单一事实源**（前端不各自硬编码平台清单）。

P2（2026-08-17 下半夜，审计可视化 + 回收站闭环）：

- ``GET /api/unified-inbox/messages/deleted``：回收站——会话内软删消息列表
  （supervisor+；配既有 restore 端点＝逐条/整体找回，误清空有了主管级后悔药）。
  前端入口 feat-gated 于 meta 新键 ``recycle_bin``（旧后端 meta 无此键→不渲染）。
- ``GET /api/admin/msg-ops-stats``：删除/撤回审计读数（ops_events 台账 7 天窗：
  各操作计数、撤回成功率与失败归因分布、最近操作流）→ ops-overview
  「消息管理审计」卡。失败归因分布是「要不要做 N 分钟内可撤预判」的判据。

双端撤回（对所有人删除）走既有 ``/api/platforms/{plat}/{acct}/message-op``
（unified_inbox_account_routes，2026-08-17 已扩 telegram/line + 成败双审计），
不在本模块。响应内 reason 一律 ASCII 机器码（前端 i18n 出人话），路由 0 CJK。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.inbox.normalizer import conv_id
from src.integrations.account_orchestrator import (
    ensure_builtin_workers,
    get_orchestrator,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# 单次批量删除/恢复的消息数上限（防误传全库 id 列表；清空走专用端点）
_MAX_BATCH_IDS = 500


def _role(request: Request) -> str:
    try:
        return str(request.session.get("role", "") or "")
    except Exception:
        return ""


def _actor(request: Request) -> str:
    try:
        return str(request.session.get("username", "") or "") or "api"
    except Exception:
        return "api"


def _can_destruct(request: Request) -> bool:
    """破坏性会话操作（清空/删除会话）的角色闸——与 conversations/delete 同口径：
    显式拒 agent/viewer；master/admin/supervisor/桌面壳 Bearer（无 session 角色）放行。"""
    return _role(request) not in ("agent", "viewer")


def _resolve_cid(body: Dict[str, Any], request: Request) -> str:
    cid = str((body or {}).get("conversation_id") or "").strip()
    if cid:
        return cid
    platform = str((body or {}).get("platform") or "").lower()
    account_id = str((body or {}).get("account_id") or "default")
    chat_key = str((body or {}).get("chat_key") or "").strip()
    if not platform or not chat_key:
        raise HTTPException(400, tr(request, "err.ws.field_required",
                                    field="conversation_id"))
    return conv_id(platform, account_id, chat_key)


def _publish(event_type: str, data: Dict[str, Any]) -> None:
    """SSE 广播（多窗口同步），best-effort 绝不影响主结果。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish(event_type, data)
    except Exception:
        logger.debug("[msgops] %s 事件发布失败（忽略）", event_type, exc_info=True)


def _parse_audit_detail(detail: str) -> Dict[str, str]:
    """把台账 detail（``cid=..;by=..;n=..;fail=..`` 键值串）解析成结构化字段。

    容错：非键值段忽略——审计卡渲染要结构化数据，别让前端各自拆串。
    """
    out: Dict[str, str] = {}
    for seg in str(detail or "").split(";"):
        if "=" in seg:
            k, _, v = seg.partition("=")
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


def _audit(kind: str, cid: str, actor: str, detail: str = "",
           *, reason: str = "ok") -> None:
    """运维台账留痕（90 天可追溯），best-effort。``reason``＝ok/失败机器码。"""
    try:
        from src.ops.ops_events import get_ops_event_store
        oes = get_ops_event_store()
        if oes is None:
            return
        parts = cid.split(":", 2)
        oes.record(
            platform=parts[0] if parts else "",
            account_id=parts[1] if len(parts) > 1 else "",
            kind=kind, reason=reason,
            detail=f"cid={cid};by={actor};{detail}")
    except Exception:
        logger.debug("[msgops] 审计落账失败（忽略）", exc_info=True)


def register_msgops_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载官方级消息管理端点（pin / clear / messages delete+restore / meta）。"""

    @app.get("/api/unified-inbox/message-ops/meta")
    async def api_msgops_meta(request: Request):
        """能力探测：前端按本响应决定挂哪些新 UI（旧后端 404 → 全部不挂）。

        ``revoke_platforms``＝各平台「双端撤回」能力的单一事实源；telegram/line
        为编排器受管账号能力（default 账号会话在调用时如实回 no_worker）。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        wa_ok = False
        try:
            from src.integrations.whatsapp_baileys_login import protocol_enabled
            wa_ok = bool(protocol_enabled(cfg))
        except Exception:
            wa_ok = False
        destruct = _can_destruct(request)
        return {
            "ok": True,
            "pin": True,
            "local_delete": True,
            "can_clear": destruct,
            "can_delete_conv": destruct,
            # P2：回收站入口（查看/恢复软删消息）。键缺失=旧后端 → 前端不渲染。
            "recycle_bin": destruct,
            "revoke_platforms": {
                "telegram": True,
                "whatsapp": wa_ok,
                "line": True,
                "messenger": False,
                "zalo": False,
                "instagram": False,
                # QQ 两端都能撤回自己发的消息（平台限约 2 分钟）：个人号走 Milky
                # recall_*_message，机器人走开放平台 DELETE /v2/.../messages/{id}
                "qq": True,
                "qqbot": True,
            },
            "edit_platforms": {"whatsapp": wa_ok},
            # 「清空时连对方设备一起删」能力（both_sides）：仅 Telegram 私聊
            # 官方支持整段历史双向删除；其余平台协议层就没有这个能力。
            "clear_remote_platforms": {"telegram": True},
        }

    @app.post("/api/unified-inbox/conversations/pin")
    async def api_conversation_pin(request: Request):
        """置顶/取消置顶（全员可用——置顶是工作偏好不是破坏操作）。

        body: ``{conversation_id | (platform, account_id, chat_key), pinned: bool}``
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = _resolve_cid(body or {}, request)
        want = bool((body or {}).get("pinned", True))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            if store.get_conversation(cid) is None:
                return {"ok": False, "reason": "not_found"}
            ts = store.set_conversation_pinned(cid, want)
        except Exception:
            logger.error("[msgops] 置顶写入失败 cid=%s", cid, exc_info=True)
            raise HTTPException(500, tr(request, "err.svc.inbox_not_ready"))
        _publish("conversation_pinned", {
            "conversation_id": cid, "pinned": bool(ts > 0), "pinned_at": ts,
            "by": _actor(request)})
        return {"ok": True, "conversation_id": cid,
                "pinned": bool(ts > 0), "pinned_at": ts}

    @app.post("/api/unified-inbox/messages/delete")
    async def api_messages_delete(request: Request):
        """删除若干条消息（仅工作台软删；全员可用；undo 经 restore 端点）。

        body: ``{conversation_id | 三元组, message_ids: [...]}``（≤500 条/次）
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = _resolve_cid(body or {}, request)
        ids_raw = (body or {}).get("message_ids")
        ids: List[str] = [str(i) for i in (ids_raw if isinstance(ids_raw, list) else [])
                          if str(i or "").strip()]
        if not ids:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="message_ids"))
        ids = ids[:_MAX_BATCH_IDS]
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        actor = _actor(request)
        try:
            n = store.delete_messages_local(cid, ids, deleted_by=actor)
        except Exception:
            logger.error("[msgops] 消息软删失败 cid=%s", cid, exc_info=True)
            raise HTTPException(500, tr(request, "err.svc.inbox_not_ready"))
        if n:
            _audit("msg_delete_local", cid, actor, f"n={n}")
            _publish("messages_deleted", {
                "conversation_id": cid, "op": "delete", "count": n})
        return {"ok": True, "conversation_id": cid, "deleted": int(n)}

    @app.post("/api/unified-inbox/messages/restore")
    async def api_messages_restore(request: Request):
        """撤销「仅工作台删除」（undo toast 的服务端退路）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = _resolve_cid(body or {}, request)
        ids_raw = (body or {}).get("message_ids")
        ids: List[str] = [str(i) for i in (ids_raw if isinstance(ids_raw, list) else [])
                          if str(i or "").strip()]
        if not ids:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="message_ids"))
        ids = ids[:_MAX_BATCH_IDS]
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            n = store.restore_messages_local(cid, ids)
        except Exception:
            logger.error("[msgops] 消息恢复失败 cid=%s", cid, exc_info=True)
            raise HTTPException(500, tr(request, "err.svc.inbox_not_ready"))
        if n:
            # P2：恢复同样留痕（回收站是主管动作，审计卡要能看到「找回了多少」）
            _audit("msg_restore", cid, _actor(request), f"n={n}")
            _publish("messages_deleted", {
                "conversation_id": cid, "op": "restore", "count": n})
        return {"ok": True, "conversation_id": cid, "restored": int(n)}

    @app.post("/api/unified-inbox/conversations/clear")
    async def api_conversation_clear(request: Request):
        """清空聊天记录（默认仅工作台，批量软删；会话行/标签/认领/AI 记忆保留）。

        supervisor+（与删除会话同闸——都属「整段记录蒸发」级操作）；
        刻意**无 undo**（量大 + 已过确认弹窗；误清由 supervisor 走 ops 台账追溯）。

        ``both_sides: true``＝连对方设备一起清空（仅 Telegram 私聊，经编排器
        ``delete_history`` → raw ``messages.DeleteHistory(revoke=True)``）。
        顺序不变量：**先远端后本地**，远端失败（no_worker / unsupported_chat_type
        / 平台拒绝）则本地一条不动、如实回 reason——「工作台清了、对方那份还在」
        的半成功是坐席无法察觉的撒谎，比整体失败更糟。审计 ``conv_clear_remote``
        成败双记（与 msg_revoke 同口径）。
        """
        api_auth(request)
        if not _can_destruct(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = _resolve_cid(body or {}, request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        actor = _actor(request)
        both = bool((body or {}).get("both_sides", False))
        remote_deleted = 0
        if both:
            parts = cid.split(":", 2)
            plat = parts[0] if parts else ""
            acct = parts[1] if len(parts) > 1 else "default"
            ck = parts[2] if len(parts) > 2 else ""
            if plat != "telegram" or not ck:
                return {"ok": False, "conversation_id": cid,
                        "reason": "remote_unsupported_platform"}
            cfg = (config_manager.config if config_manager is not None else {}) or {}
            try:
                ensure_builtin_workers(cfg)
                res = await get_orchestrator(cfg).delete_history(
                    plat, acct, ck, revoke=True)
            except Exception:
                logger.error("[msgops] 双向清空调度失败 cid=%s", cid, exc_info=True)
                res = {"ok": False, "reason": "orchestrator_error"}
            if not (isinstance(res, dict) and res.get("ok")):
                reason = str((res or {}).get("reason") or "remote_failed")[:80]
                _audit("conv_clear_remote", cid, actor, "n=0;fail=1",
                       reason=reason or "remote_failed")
                return {"ok": False, "conversation_id": cid, "reason": reason}
            remote_deleted = int(res.get("deleted") or 0)
            _audit("conv_clear_remote", cid, actor,
                   f"n={remote_deleted};fail=0")
        try:
            n = store.clear_conversation_messages(cid, deleted_by=actor)
        except Exception:
            logger.error("[msgops] 会话清空失败 cid=%s", cid, exc_info=True)
            raise HTTPException(500, tr(request, "err.svc.inbox_not_ready"))
        logger.info("[msgops] 会话已清空 cid=%s by=%s n=%s both=%s remote=%s ts=%s",
                    cid, actor, n, both, remote_deleted, time.time())
        if n:
            _audit("conv_clear", cid, actor, f"n={n}")
        if n or remote_deleted:
            _publish("messages_deleted", {
                "conversation_id": cid, "op": "clear", "count": n})
        return {"ok": True, "conversation_id": cid, "cleared": int(n),
                "both_sides": both, "remote_deleted": remote_deleted}

    @app.get("/api/unified-inbox/messages/deleted")
    async def api_messages_deleted_list(request: Request):
        """回收站（P2）：列出会话内「仅工作台删除」的消息（supervisor+）。

        query: ``conversation_id``（或 platform+account_id+chat_key 三元组）、
        ``limit``（默认 200，封顶 500）。配 ``POST /messages/restore``＝找回。
        """
        api_auth(request)
        if not _can_destruct(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        qp = request.query_params
        body = {"conversation_id": qp.get("conversation_id"),
                "platform": qp.get("platform"),
                "account_id": qp.get("account_id"),
                "chat_key": qp.get("chat_key")}
        cid = _resolve_cid(body, request)
        try:
            limit = int(qp.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            items = store.list_deleted_messages(cid, limit=limit)
        except Exception:
            logger.error("[msgops] 回收站读取失败 cid=%s", cid, exc_info=True)
            raise HTTPException(500, tr(request, "err.svc.inbox_not_ready"))
        return {"ok": True, "conversation_id": cid, "items": items}

    @app.get("/api/admin/msg-ops-stats")
    async def api_msg_ops_stats(request: Request):
        """删除/撤回审计读数（P2）：ops-overview「消息管理审计」卡的数据源。

        ops_events 台账 N 天窗（默认 7，封顶 30）：各操作计数、撤回成功率与
        失败归因分布（``by_reason``）、按日成败、最近操作流（detail 已结构化）。
        台账缺席（建库失败）→ 空计数如实返回（卡按零流量隐藏，不摆错误现场）。
        """
        api_auth(request)
        if not _can_destruct(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            days = max(1, min(int(request.query_params.get("days") or 7), 30))
        except (TypeError, ValueError):
            days = 7
        kinds = ["msg_revoke", "msg_delete_local", "conv_clear",
                 "conv_clear_remote", "conv_delete", "msg_restore"]
        counts: Dict[str, Any] = {}
        daily_revoke: List[Dict[str, Any]] = []
        recent: List[Dict[str, Any]] = []
        try:
            from src.ops.ops_events import get_ops_event_store
            oes = get_ops_event_store()
            if oes is not None:
                counts = oes.reason_summary(kinds, days=days)
                daily_revoke = oes.daily_kinds(["msg_revoke"], days=days)
                for ev in oes.recent_kinds(kinds, limit=12):
                    d = _parse_audit_detail(ev.get("detail") or "")
                    recent.append({
                        "ts": float(ev.get("ts") or 0),
                        "kind": str(ev.get("kind") or ""),
                        "platform": str(ev.get("platform") or ""),
                        "account_id": str(ev.get("account_id") or ""),
                        "reason": str(ev.get("reason") or ""),
                        "by": d.get("by", ""),
                        "cid": d.get("cid", ""),
                        "n": d.get("n", ""),
                        "fail": d.get("fail", ""),
                    })
        except Exception:
            logger.debug("[msgops] 审计读数聚合失败（回空）", exc_info=True)
        return {"ok": True, "days": days, "counts": counts,
                "daily_revoke": daily_revoke, "recent": recent}
