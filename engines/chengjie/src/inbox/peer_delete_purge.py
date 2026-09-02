# -*- coding: utf-8 -*-
"""对端删消息 → 关联记忆清理（#146，2026-09-02）。

钧实锤：手机上删了消息，工作台没跟着删——这不只是显示不同步，残留的错误内容会进
AI 记忆、影响后面每一轮回复。B87（实施68）已把镜像软删接到 A 线 client；本模块补
「删了的消息不能再影响 AI」的三条腿：

1. **情景记忆**（``episodic_memory``）：按 ``source_quote``（五件套溯源列＝抽取自
   哪句原话）精确反查——被删消息的正文归一后与 quote 前 200 字相等 → 删该事实。
   只删 ``hits == 1``（复发事实＝客户在别处又说过一遍，一条删了事实仍在）。
   早期无溯源的条目（quote 为空）匹配不到，如实接受。
2. **A 线对话上下文**（``ContextStore._conversation_history`` / ``last_message``）：
   剔掉 role=user 且正文匹配的条目；``last_message`` 命中时清空——否则下一轮
   「上一轮补录进历史」机制会把它再塞回去。只碰**已存在**的上下文（不因清理
   而凭空建 ctx）。
3. **B 线历史**：不在本模块——``InboxStore.list_recent_messages`` 已在默认口径下剔
   ``deleted_by='peer'`` 行（单点收口，十几处 LLM 历史消费方零改动）。

全部 best-effort：任何一步失败只记 debug、不影响软删本身与 SSE。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger("PeerDeletePurge")

# 与 ``EpisodicMemoryStore.add_fact`` 的 quote 口径一致：原话截 200 字
QUOTE_MAX = 200
# 太短的正文（「好」「嗯」）在记忆里没有唯一性，反查只会误删
MIN_TEXT_LEN = 3

_WS_RE = re.compile(r"\s+")


def normalize_quote(text: Any) -> str:
    """被删消息正文 → 与 ``source_quote`` 可比对的归一形态。

    与抽取端同序：先剥 ``[图片内容]…`` 识图描述（抽取只吃客户自己的话），再
    strip + 折叠空白 + 截 200。``strip_media_desc`` 不可用时按原文归一。
    """
    s = str(text or "")
    try:
        from src.inbox.media_enrich import strip_media_desc
        s = strip_media_desc(s)
    except Exception:
        pass
    s = _WS_RE.sub(" ", s).strip()
    return s[:QUOTE_MAX]


def quotes_match(a: str, b: str) -> bool:
    """两条归一文本是否指同一句原话（前 200 字相等；空/过短不算）。"""
    a = _WS_RE.sub(" ", str(a or "")).strip()[:QUOTE_MAX]
    b = _WS_RE.sub(" ", str(b or "")).strip()[:QUOTE_MAX]
    if len(a) < MIN_TEXT_LEN or len(b) < MIN_TEXT_LEN:
        return False
    return a == b


def split_conversation_id(conversation_id: str) -> Tuple[str, str, str]:
    """``platform:account_id:chat_key`` → 三元组（chat_key 自身可含冒号，只切两刀）。"""
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) != 3:
        return "", "", ""
    return parts[0], parts[1], parts[2]


def key_has_component(memory_key: str, chat_key: str) -> bool:
    """记忆键是否**以组件形式**含该 chat_key（``telegram:acct:123`` / ``acct:123`` /
    ``123`` / ``123_456`` 群键都认；纯子串「1234」∋「123」不认）。"""
    ck = str(chat_key or "").strip().lower()
    mk = str(memory_key or "").strip().lower()
    if not ck or not mk:
        return False
    if mk == ck:
        return True
    return ck in re.split(r"[:_]", mk)


def inbound_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只留客户侧（direction=in）且正文够长的被删行——AI 自己发的被删了不进记忆。"""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("direction") or "in") != "in":
            continue
        q = normalize_quote(r.get("text"))
        if len(q) < MIN_TEXT_LEN:
            continue
        rr = dict(r)
        rr["_quote"] = q
        out.append(rr)
    return out


def purge_episodic(episodic_store: Any, rows: Iterable[Dict[str, Any]]) -> int:
    """情景记忆清理：按 (chat_key, quote) 反查删除。返回删除条数。"""
    if episodic_store is None or not hasattr(episodic_store, "delete_by_source_quotes"):
        return 0
    by_chat: Dict[str, List[str]] = {}
    for r in inbound_rows(rows):
        _plat, _acct, ck = split_conversation_id(str(r.get("conversation_id") or ""))
        if not ck:
            continue
        by_chat.setdefault(ck, []).append(r["_quote"])
    n = 0
    for ck, quotes in by_chat.items():
        try:
            n += int(episodic_store.delete_by_source_quotes(ck, quotes) or 0)
        except Exception:
            logger.debug("[peer-delete] episodic purge failed chat=%s", ck, exc_info=True)
    return n


def purge_context_history(context_store: Any, rows: Iterable[Dict[str, Any]]) -> int:
    """A 线对话上下文清理：剔历史里匹配的 user 条 + 清命中的 last_message。返回改动数。"""
    if context_store is None or not hasattr(context_store, "peek"):
        return 0
    changed = 0
    try:
        from src.utils.context_store import make_context_key
    except Exception:
        return 0
    for r in inbound_rows(rows):
        _plat, acct, ck = split_conversation_id(str(r.get("conversation_id") or ""))
        if not ck:
            continue
        keys: List[str] = []
        for k in (make_context_key(ck, acct), ck):
            if k and k not in keys:
                keys.append(k)
        for key in keys:
            try:
                ctx = context_store.peek(key)
            except Exception:
                ctx = None
            if not isinstance(ctx, dict):
                continue
            touched = False
            hist = ctx.get("_conversation_history")
            if isinstance(hist, list):
                kept = [m for m in hist
                        if not (isinstance(m, dict)
                                and str(m.get("role") or "") == "user"
                                and quotes_match(m.get("content"), r["_quote"]))]
                if len(kept) != len(hist):
                    ctx["_conversation_history"] = kept
                    touched = True
            if quotes_match(ctx.get("last_message"), r["_quote"]):
                ctx["last_message"] = ""
                touched = True
            if touched:
                changed += 1
                try:
                    context_store.mark_dirty(key)
                    context_store.flush(key)
                except Exception:
                    logger.debug("[peer-delete] ctx flush failed key=%s", key, exc_info=True)
    return changed


def purge_for_deleted_rows(skill_manager: Any, rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """编排入口（``protocol_bridge`` 经注册钩子调用）：返回 ``{"memory": n, "context": m}``。"""
    rows = list(rows or [])
    out = {"memory": 0, "context": 0}
    if skill_manager is None or not rows:
        return out
    try:
        out["memory"] = purge_episodic(getattr(skill_manager, "_episodic_store", None), rows)
    except Exception:
        logger.debug("[peer-delete] episodic purge crashed", exc_info=True)
    try:
        out["context"] = purge_context_history(
            getattr(skill_manager, "_context_store", None), rows)
    except Exception:
        logger.debug("[peer-delete] context purge crashed", exc_info=True)
    return out


__all__ = [
    "MIN_TEXT_LEN", "QUOTE_MAX",
    "inbound_rows", "key_has_component", "normalize_quote",
    "purge_context_history", "purge_episodic", "purge_for_deleted_rows",
    "quotes_match", "split_conversation_id",
]
