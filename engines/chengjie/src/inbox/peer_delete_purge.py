# -*- coding: utf-8 -*-
"""对端删消息 → 工作台软删后的记忆侧处置（#32② / #145⑤）。

#146（2026-09-02）曾把关联情景记忆物理删掉、A 线历史整条剔除。2026-09-04 老板
拍板改回：「同步删除（显得专业）；AI 可以有记忆，但不要主动提删掉的信息。」
凭空少一条会让后续回复接不上——所以记忆**保留**，历史**留槽位**换成占位。

本模块三条腿（工作台移除仍走 store 既有 ``deleted_by=peer``，本文件不改 store）：
1. **账本** ``withdrawn_cite.record_withdrawn``：记下被撤原文；
2. **情景记忆不删**（``purge_episodic`` 现只记账，不再 ``delete_by_source_quotes``）；
3. **A 线上下文**把匹配的 user 条换成占位，不删轮次。
B 线 ``list_recent_messages`` 默认仍剔 peer 软删行（属 store.py，本条不能改）——
注入层用账本 hint 补「知道但不提」。
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
    """记忆侧：记下撤回原文，**不删**情景事实。返回新记账条数。

    ``episodic_store`` 保留形参以兼容旧调用方；现网不再调 ``delete_by_source_quotes``。
    """
    n = 0
    try:
        from src.inbox.withdrawn_cite import record_withdrawn
    except Exception:
        return 0
    for r in inbound_rows(rows):
        cid = str(r.get("conversation_id") or "").strip()
        q = str(r.get("_quote") or r.get("text") or "")
        try:
            if record_withdrawn(cid, q):
                n += 1
        except Exception:
            logger.debug("[peer-delete] withdrawn ledger failed cid=%s", cid, exc_info=True)
    return n


def purge_context_history(context_store: Any, rows: Iterable[Dict[str, Any]]) -> int:
    """A 线对话上下文：匹配的 user 条换成占位（不删轮次）。返回改动数。"""
    if context_store is None or not hasattr(context_store, "peek"):
        return 0
    try:
        from src.inbox.withdrawn_cite import PLACEHOLDER
    except Exception:
        PLACEHOLDER = "（对方已撤回一条消息，不得主动引用其内容）"

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
                new_hist = []
                for m in hist:
                    if (isinstance(m, dict)
                            and str(m.get("role") or "") == "user"
                            and quotes_match(m.get("content"), r["_quote"])):
                        mm = dict(m)
                        mm["content"] = PLACEHOLDER
                        mm["_withdrawn"] = True
                        new_hist.append(mm)
                        touched = True
                    else:
                        new_hist.append(m)
                if touched:
                    ctx["_conversation_history"] = new_hist
            if quotes_match(ctx.get("last_message"), r["_quote"]):
                ctx["last_message"] = PLACEHOLDER
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
