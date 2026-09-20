# -*- coding: utf-8 -*-
"""报障群官方 bot 发送通道（实施82 P0，2026-08-29 老板拍板）。

背景：群里官方消息此前由支持号（pyrogram **用户账号** 6834964252）发送——
用户账号发不出 inline 按钮（Bot API 专属），品牌/互动面全被锁死。老板 0829
决定：@tgzkw_bot 进群当发送方（已设管理员），**发送权交给 bot、观察/登记仍走
用户账号**（bug_intake observe 管线不动）。

铁律（改动前先读）：
1. **绝不消费更新**（长轮询拉取、回调地址注册/注销一律禁止——门禁按字面量
   扫描，此处刻意用描述而非那三个方法名）：该 bot 的更新流挂在官网
   ``bd2026.cc/api/telegram/webhook``（龙珠活动/订单绑定/客服核销/管理命令的
   活跃消费链），抢更新＝打崩官网 bot。本模块只做消息**出站**（与更新流不
   冲突）；按钮回调消费属官网侧集成（实施82 P2，另批）。
2. **token 不入库不入代码**：运行时读 ``notify_webhooks_store``（telegram 渠道，
   与告警链同源同热更）；测试经 ``set_store_path`` 落 tmp。
3. **自发现 bot 身份**：``bot_user_id()`` 懒调 getMe 并缓存——观察管线用它把
   「自己 bot 的消息」硬压制（否则 bot 发的周公示里满是 bug 词，会被自己的
   登记链当成新报障，实施82 施工前推演出的自咬环）。
4. bot 发送失败（未进群/网络/未配 token）→ 调用方回落 pyrogram 用户账号——
   客户部署没有 bot 进群，回落保证行为不劣于旧链。

门禁 tests/test_bug_bot.py。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_LOCK = threading.Lock()
# getMe 缓存：{"id": int|None, "ts": float}——成功缓存 24h，失败负缓存 10min
#（trigger 热路径会问 is_official_bot，不能每条消息一次 HTTP）。
_ME_CACHE: Dict[str, Any] = {"id": None, "ts": 0.0, "ok": False}
_ME_TTL_OK = 24 * 3600
_ME_TTL_FAIL = 600


def bot_token() -> str:
    """notify_webhooks 里第一个 telegram 渠道的 bot token（无则空串）。"""
    try:
        from src.integrations.notify_webhooks_store import load
        for ch in load() or []:
            if str(ch.get("format")) == "telegram" and ch.get("token"):
                return str(ch["token"])
    except Exception:
        logger.debug("[bug_bot] token 读取失败", exc_info=True)
    return ""


def _api_call(method: str, body: Dict[str, Any], *, token: str = "",
              timeout: int = 20) -> Tuple[bool, Dict[str, Any]]:
    tok = token or bot_token()
    if not tok:
        return False, {"error": "no_token"}
    req = urllib.request.Request(
        _API.format(token=tok, method=method),
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        return bool(resp.get("ok")), resp
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return False, {"error": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001
        return False, {"error": str(e)[:160]}


def bot_user_id(now: Optional[float] = None) -> Optional[int]:
    """本 bot 的 Telegram user id（getMe 懒取 + 缓存；拿不到返 None）。"""
    ts = float(now if now is not None else time.time())
    with _LOCK:
        age = ts - float(_ME_CACHE.get("ts") or 0)
        ttl = _ME_TTL_OK if _ME_CACHE.get("ok") else _ME_TTL_FAIL
        if _ME_CACHE.get("ts") and age < ttl:
            return _ME_CACHE.get("id")
    ok, resp = _api_call("getMe", {}, timeout=5)
    bid: Optional[int] = None
    if ok:
        try:
            bid = int((resp.get("result") or {}).get("id") or 0) or None
        except (TypeError, ValueError):
            bid = None
    with _LOCK:
        _ME_CACHE.update({"id": bid, "ts": ts, "ok": bool(bid)})
    return bid


def is_official_bot(sender_id: Any) -> bool:
    """该 sender 是否我们自己的官方 bot（观察管线的自咬环守卫）。

    判定失败（无 token/网络断）一律 False——宁可让 trigger 层的其他闸门兜底，
    也不能把「查不到」当「是官方」误压制真用户。
    """
    s = str(sender_id or "").strip()
    if not s.isdigit():
        return False
    bid = bot_user_id()
    return bid is not None and int(s) == bid


def reset_cache_for_tests() -> None:
    with _LOCK:
        _ME_CACHE.update({"id": None, "ts": 0.0, "ok": False})


def bot_send_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """``bug_intake.bot_send``（默认 **true**，2026-08-29 老板拍板 bot 代发；
    置 false 回旧链=纯用户账号发送）。异常按 true——回落链保证安全。"""
    try:
        raw = ((config or {}).get("bug_intake") or {}).get("bot_send", True)
        return bool(raw)
    except Exception:
        return True


def build_verify_keyboard(ticket_id: Any,
                          reporter_id: Any) -> Optional[Dict[str, Any]]:
    """修复回访的验证键盘（实施82 P2）。

    callback_data = ``btv:<ticket>:<y|n>:<reporterId>``——回调消费在官网
    webhook（权限校验也在那边：报障人本人/管理员才记账）。reporter_id 非数字
    （webuser 工单等）返 None＝不挂键盘，文字口令链兜底。
    """
    try:
        tid = int(ticket_id)
        rid = str(reporter_id or "").strip()
        if tid <= 0 or not rid.isdigit():
            return None
    except (TypeError, ValueError):
        return None
    return {"inline_keyboard": [[
        {"text": "✅ 修好了", "callback_data": f"btv:{tid}:y:{rid}"},
        {"text": "❌ 还是不行", "callback_data": f"btv:{tid}:n:{rid}"},
    ]]}


def send_group_html(chat_id: Any, html: str, *,
                    reply_to_message_id: int = 0,
                    buttons: Optional[Dict[str, Any]] = None,
                    token: str = "") -> Tuple[bool, int, str]:
    """bot 发 HTML 消息进群。返回 (ok, message_id, note)。

    reply_to 失效（原消息被删）自动降级为不带引用重发一次——引用是增强不是
    前提，不能因为引用失败把回访整条吞掉。``buttons``＝inline 键盘 dict
    （build_verify_keyboard 产物；bot 专属能力，pyro 回落链无此参）。
    """
    try:
        cid: Any = int(str(chat_id).strip())
    except (TypeError, ValueError):
        return False, 0, "bad_chat_id"
    body: Dict[str, Any] = {
        "chat_id": cid, "text": str(html or ""), "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        body["reply_markup"] = buttons
    if reply_to_message_id:
        body["reply_to_message_id"] = int(reply_to_message_id)
    ok, resp = _api_call("sendMessage", body, token=token)
    if not ok and reply_to_message_id and "reply" in str(resp)[:200].lower():
        body.pop("reply_to_message_id", None)
        ok, resp = _api_call("sendMessage", body, token=token)
    if not ok:
        note = str(resp.get("description") or resp.get("error") or resp)[:160]
        return False, 0, note
    mid = 0
    try:
        mid = int((resp.get("result") or {}).get("message_id") or 0)
    except (TypeError, ValueError):
        mid = 0
    return True, mid, "bot ok"


__all__ = [
    "bot_token", "bot_user_id", "is_official_bot", "bot_send_enabled",
    "build_verify_keyboard", "send_group_html", "reset_cache_for_tests",
]
