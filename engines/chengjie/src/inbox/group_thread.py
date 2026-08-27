# -*- coding: utf-8 -*-
"""群/频道会话的纯函数核心（2026-08-19 P0）。

把「会话类型归一 / 出站 kwargs 签名过滤 / 引用是否真上了线 / 发言人→@提及成员」
收成无 I/O 的可测函数，编排器、收件箱路由、Zalo 透传共用同一口径。

不变量：
- ``filter_send_kwargs`` **不**把 ``**kwargs`` 当成全收——worker 声明了哪个具名参
  才透哪个。否则假 worker / 官方 API 壳会把 ``reply_to`` 吞进黑洞，编排器还以为
  引用发出去了（173 实录「坐席见引用、客户端没有」的根因之一）。
- ``quote_applied`` 只认 worker 回执，绝不从「我们传了 reply_to」推断 True。
- Zalo thread id 不自描述：显式 ``chat_type`` 与 Node 群注册表是双重保险，
  本模块只负责从会话库查出类型并归一成 Node 认得的 ``group`` / ``private``。
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, Iterable, List, Mapping, Optional

# 工作台引用入口只对这两家打开：worker 有原生 quoted / reply_to_message_id，
# 且会回 ``quote_applied``。Messenger DOM 引用是 degrade-safe，不进白名单。
QUOTE_NATIVE_PLATFORMS = frozenset({"telegram", "whatsapp"})

_GROUP_TYPES = frozenset({
    "group", "supergroup", "gigagroup", "megagroup", "room",
    "group_thread", "community",
})
_PRIVATE_TYPES = frozenset({"private", "user", "direct", ""})


def normalize_chat_type(raw: Any) -> str:
    """归一会话类型：``private`` / ``group`` / ``channel``（空/未知 → private）。"""
    t = str(raw or "").strip().lower()
    if t in _GROUP_TYPES:
        return "group"
    if t == "channel":
        return "channel"
    if t in _PRIVATE_TYPES:
        return "private"
    return "private"


def is_one_to_one(raw: Any) -> bool:
    """该会话是否 1:1 私聊——「自动回复只面向单人」这类闸门的**白名单**判据。

    刻意不写成 ``chat_type != "group"``：上游 ingest 原样落的值可能是
    ``channel`` / ``supergroup`` / ``room``（LINE 多人房间），黑名单会全漏网——
    生产实测 zhiliao 有 10 个 TG 频道会话，黑名单口径下频道广播会被当客户私聊
    去自动回复。``store._contact_union_sql`` 早有同一课（那里用的就是白名单），
    本函数把该口径收成一处，谁都不必再各写一遍集合。
    """
    return normalize_chat_type(raw) == "private"


def quote_is_native(platform: str) -> bool:
    """该平台工作台是否允许「引用回复」入口（与 worker 回执白名单同集）。"""
    return str(platform or "").strip().lower() in QUOTE_NATIVE_PLATFORMS


def filter_send_kwargs(fn: Any, kwargs: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """按 worker ``send`` / ``send_media`` 的**具名**形参过滤 kwargs。

    ``*args`` / ``**kwargs`` 一律跳过：把 VAR_KEYWORD 当全收会把未声明的
    ``reply_to`` 悄悄塞进去，调用方无法从 TypeError 得知「其实没接」。
    值为 None 的键丢弃（与「没传」同语义）。
    """
    if not kwargs:
        return {}
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    accepted = set()
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        accepted.add(name)
    return {k: v for k, v in kwargs.items() if k in accepted and v is not None}


def annotate_quote_applied(res: Any) -> Dict[str, Any]:
    """保证发送结果是带 ``quote_applied: bool`` 的 dict。缺省 / 非 dict → False。"""
    out = dict(res) if isinstance(res, dict) else {"delivered": True}
    out["quote_applied"] = bool(out.get("quote_applied"))
    return out


def should_mirror_quote(reply_to: Any, result: Mapping[str, Any]) -> bool:
    """出站镜像是否写 ``source.reply_to``：必须有引用意图 **且** worker 回执为真。"""
    if not isinstance(reply_to, dict):
        return False
    if not (reply_to.get("id") or reply_to.get("text")):
        return False
    return bool(result.get("quote_applied"))


def lookup_chat_type(
    platform: str,
    account_id: str,
    chat_key: str,
    *,
    store: Any = None,
) -> str:
    """从会话库取归一后的 chat_type；库不可达 / 无行 → 空串（调用方不传该键）。"""
    plat = str(platform or "").strip().lower()
    ck = str(chat_key or "").strip()
    if not (plat and ck):
        return ""
    st = store
    if st is None:
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            st = get_inbox_store()
        except Exception:
            st = None
    if st is None or not hasattr(st, "get_conversation"):
        return ""
    try:
        from src.inbox.normalizer import conv_id
        row = st.get_conversation(conv_id(plat, str(account_id or ""), ck)) or {}
    except Exception:
        return ""
    raw = str(row.get("chat_type") or "").strip()
    if not raw:
        return ""
    return normalize_chat_type(raw)


def speakers_as_members(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """把 ``list_group_speakers`` 行转成 group-members 面板同款 ``{jid,number,name,admin}``。

    无号码的发言人（TG/LINE/Zalo 常见）``number`` 为空——前端插 ``@name``，
    不渲染 ``.mn-num``。去重键= jid 或 name。
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        sid = str(raw.get("sender_id") or "").strip()
        name = str(raw.get("sender_name") or "").strip() or sid
        key = sid or name
        if not key or key in seen:
            continue
        seen.add(key)
        number = _speaker_number(sid)
        out.append({
            "jid": sid,
            "number": number,
            "name": name or number or sid,
            "admin": "",
        })
    return out


def _speaker_number(sender_id: str) -> str:
    s = str(sender_id or "").strip()
    if not s:
        return ""
    local = s.split("@", 1)[0]
    digits = local[1:] if local.startswith("+") else local
    if digits.isdigit() and 5 <= len(digits) <= 20:
        return digits
    return ""
