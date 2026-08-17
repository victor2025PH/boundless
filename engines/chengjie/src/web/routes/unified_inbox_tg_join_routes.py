"""统一收件箱——Telegram「加入群组/频道」动作路由（2026-08-12）。

场景：客户在会话里发来 ``t.me`` 群链接（收件箱气泡已 linkify 可点，但「点开」只是
浏览器看预览页——真正入群需要**会话绑定的那个 Telegram 账号**执行 join）。本端点
把「加入」做成坐席显式动作：气泡工具行「加入群组」按钮 → 确认 → 此处真入群。

设计要点：
- **账号语义是硬约束**：``account_id=default`` 用进程主 A 线 client；受管多开账号
  **只**用其正在运行的受管 worker client，worker 不在线一律 409——绝不回落主
  client（读路径的优雅降级在写动作上等于「换了个账号入群」，比失败更糟）。
- pyrogram client 活在自己的事件循环 → ``run_coroutine_threadsafe`` 跨 loop 调度，
  ``await wrap_future`` 等待（与 fetch-media / resolve-peer 同姿势，不冻结 web loop）。
- 链接解析 ``parse_tg_join_target`` 纯函数（独立单测）：公开用户名 → username；
  ``+hash`` / ``joinchat/hash`` 邀请链 → 完整链接（pyrogram ``join_chat`` 两种都吃）；
  share/proxy/addstickers/c/… 等不可入群的深链拒绝。
- 错误按坐席可操作性分流：已在群=ok(already)；需管理员批准=ok(pending)；链接失效/
  私有/超上限=400；FloodWait=429 带秒数；client 不在线=409；超时=504。文案全走
  ``tr()`` 请求级 i18n（响应 0 硬编码中文，棘轮门禁口径）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT_SEC = 30.0

# t.me 第一段里「不是可入群目标」的保留路径（share 分享、代理配置、贴纸包、
# 语言包、私有频道消息深链 c/<id>/<msg>、预览 iv 等）。s/<name> 是频道预览
# 例外——第二段才是频道名，在解析器里单独处理。
_RESERVED_PATHS = frozenset({
    "share", "proxy", "socks", "iv", "login", "confirmphone", "invoice",
    "addstickers", "addemoji", "addtheme", "setlanguage", "boost",
    "giftcode", "contact", "c", "m", "k",
})

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_HOSTS = ("t.me", "telegram.me", "telegram.dog")


def parse_tg_join_target(link: str) -> Tuple[str, str]:
    """把用户发来的 Telegram 链接解析成 join_chat 可用目标（纯函数）。

    返回 ``(kind, target)``：
    - ``("username", "<name>")``——公开群/频道用户名（``t.me/name``、``t.me/s/name``、
      ``t.me/name/123`` 消息链、``@name``）；
    - ``("invite", "https://t.me/+<hash>")``——私有邀请链（``+hash`` / ``joinchat/hash``）；
    - ``("", "")``——认不出 / 明确不可入群（share/proxy/贴纸包/c 深链/裸文本…）。
    """
    s = str(link or "").strip()
    if not s:
        return "", ""
    if s.startswith("@"):
        name = s[1:]
        return ("username", name) if _USERNAME_RE.match(name) else ("", "")
    # 掐掉 scheme / www / query / fragment，只认 t.me 系主机
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^www\.", "", s, flags=re.IGNORECASE)
    s = s.split("?", 1)[0].split("#", 1)[0].strip()
    host, _, path = s.partition("/")
    if host.lower() not in _HOSTS:
        return "", ""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "", ""
    head = parts[0]
    # 邀请链：t.me/+hash 或 t.me/joinchat/hash → 保留完整链接形态给 pyrogram
    if head.startswith("+"):
        h = head[1:]
        return ("invite", f"https://t.me/+{h}") if _INVITE_HASH_RE.match(h) else ("", "")
    if head.lower() == "joinchat":
        if len(parts) >= 2 and _INVITE_HASH_RE.match(parts[1]):
            return "invite", f"https://t.me/joinchat/{parts[1]}"
        return "", ""
    # 预览深链 t.me/s/<name> → 第二段才是频道名
    if head.lower() == "s":
        if len(parts) >= 2 and _USERNAME_RE.match(parts[1]):
            return "username", parts[1]
        return "", ""
    if head.lower() in _RESERVED_PATHS:
        return "", ""
    # 公开用户名（后面还挂消息 id 之类的段不影响：t.me/name/123 仍指向 name）
    return ("username", head) if _USERNAME_RE.match(head) else ("", "")


def _tg_join_pyro(app: Any, account_id: str) -> Any:
    """按账号取「有资格执行 join」的 pyrogram client。

    与读路径 ``_get_tg_pyro_for_account`` 的关键差异：**受管账号绝不回落主 client**
    ——join 是写动作，回落等于换了另一个账号入群。default（A 线主账号）直取
    ``app.state.telegram_client``；其余账号仅取其正在运行的受管 worker client。
    """
    from src.web.routes.unified_inbox_account_routes import _extract_pyro
    if not account_id or account_id == "default":
        return _extract_pyro(
            getattr(getattr(app, "state", None), "telegram_client", None))
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is not None:
            worker = orch.worker_for("telegram", account_id)
            return _extract_pyro(getattr(worker, "client", None))
    except Exception:
        logger.debug("[tg-join] 取账号 worker client 失败 acct=%s",
                     account_id, exc_info=True)
    return None


def _chat_summary(chat: Any) -> Optional[Dict[str, Any]]:
    """把 pyrogram Chat 收敛成前端要的最小字段（防御式取值）。"""
    if chat is None:
        return None
    try:
        ctype = getattr(chat, "type", "")
        ctype = getattr(ctype, "value", None) or str(ctype or "")
        return {
            "id": getattr(chat, "id", None),
            "title": getattr(chat, "title", None)
                     or getattr(chat, "first_name", None) or "",
            "username": getattr(chat, "username", None) or "",
            "type": str(ctype),
        }
    except Exception:
        return None


def register_tg_join_routes(app, *, page_auth) -> None:
    """挂载 Telegram 入群动作端点。"""

    @app.post("/api/unified-inbox/tg-join-chat")
    async def api_unified_inbox_tg_join_chat(
        request: Request, _=Depends(page_auth),
    ):
        """用会话绑定账号加入 t.me 群组/频道。Body: ``{account_id, link}``。

        坐席显式动作（与手动发送同责任边界），不做任何自动入群。
        """
        body = await request.json()
        account_id = str(body.get("account_id") or "default")
        link = str(body.get("link") or "").strip()
        kind, target = parse_tg_join_target(link)
        if not kind:
            raise HTTPException(400, tr(request, "err.inbox.join_bad_link"))

        pyro = _tg_join_pyro(request.app, account_id)
        loop = getattr(pyro, "loop", None)
        if pyro is None or loop is None or not loop.is_running():
            raise HTTPException(
                409, tr(request, "err.inbox.join_client_unavailable"))

        fut = asyncio.run_coroutine_threadsafe(pyro.join_chat(target), loop)
        try:
            chat = await asyncio.wait_for(
                asyncio.wrap_future(fut), timeout=_JOIN_TIMEOUT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(504, tr(request, "err.inbox.join_timeout"))
        except Exception as exc:
            # 按异常类名分流（不 import pyrogram.errors 全家桶：跨版本类集不稳，
            # 且保持本模块在无 pyrogram 环境可导入、纯函数可单测）。
            name = type(exc).__name__
            if name == "UserAlreadyParticipant":
                logger.info("[tg-join] 已在群 acct=%s target=%s", account_id, target)
                return {"ok": True, "already": True, "chat": None}
            if name == "InviteRequestSent":
                logger.info("[tg-join] 入群申请已提交 acct=%s target=%s",
                            account_id, target)
                return {"ok": True, "pending": True, "chat": None}
            if name == "FloodWait":
                wait = int(getattr(exc, "value", 0) or 0)
                raise HTTPException(
                    429, tr(request, "err.inbox.join_flood", sec=wait))
            if name in ("InviteHashExpired", "InviteHashInvalid"):
                raise HTTPException(400, tr(request, "err.inbox.join_expired"))
            if name in ("ChannelPrivate", "ChatAdminRequired", "UserBannedInChannel",
                        "ChatRestricted", "ChatGuestSendForbidden"):
                raise HTTPException(400, tr(request, "err.inbox.join_private"))
            if name == "ChannelsTooMuch":
                raise HTTPException(400, tr(request, "err.inbox.join_too_many"))
            if name in ("UsernameNotOccupied", "UsernameInvalid", "PeerIdInvalid",
                        "BadRequest"):
                raise HTTPException(400, tr(request, "err.inbox.join_bad_link"))
            logger.warning("[tg-join] 加入失败 acct=%s target=%s err=%s: %s",
                           account_id, target, name, exc)
            raise HTTPException(502, tr(request, "err.inbox.join_failed"))

        info = _chat_summary(chat)
        logger.info("[tg-join] 已加入 acct=%s kind=%s target=%s chat=%s",
                    account_id, kind, target, (info or {}).get("id"))
        return {"ok": True, "chat": info}
