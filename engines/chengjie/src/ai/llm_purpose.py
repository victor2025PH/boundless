# -*- coding: utf-8 -*-
"""LLM 调用用途（B2 / 成本归因）——从 llm_cost 抽出的零依赖事实源。

09-08 成本对账线把 purpose_scope 写进了 ``llm_cost.py``，与 cost_ledger / 币种
改造同文件未提交。B1/B2 计量出口必须读同一套 ContextVar，但不能把整份成本线
绑上 HEAD。本模块是那一套用途 API 的单一实现；``llm_cost`` 再 re-export，
两边读到的是同一 ``_PURPOSE_VAR``。
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Dict, Optional, Tuple

PURPOSE_CUSTOMER_REPLY = "customer_reply"
PURPOSE_DRILL = "drill"
PURPOSE_MEMORY_EXTRACT = "memory_extract"
PURPOSE_TRANSLATE = "translate"
PURPOSE_ASSISTANT = "assistant"
PURPOSE_KB = "kb"
PURPOSE_VISION = "vision"
PURPOSE_PROBE = "probe"
PURPOSE_EVAL = "eval"
PURPOSE_TOOL = "tool"
PURPOSE_COLLOQUIAL = "colloquial"
PURPOSE_PERSONA = "persona"
PURPOSE_UNKNOWN = "unknown"

KNOWN_PURPOSES = frozenset({
    PURPOSE_CUSTOMER_REPLY, PURPOSE_DRILL, PURPOSE_MEMORY_EXTRACT, PURPOSE_TRANSLATE,
    PURPOSE_ASSISTANT, PURPOSE_KB, PURPOSE_VISION, PURPOSE_PROBE, PURPOSE_EVAL,
    PURPOSE_TOOL, PURPOSE_COLLOQUIAL, PURPOSE_PERSONA, PURPOSE_UNKNOWN,
})

# 与 utils.case_center.DRILL_UID_RANGES 同源；零依赖复刻。
_DRILL_RANGES: Tuple[Tuple[int, int], ...] = ((990_001_000, 990_001_999),)

_PURPOSE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("llm_purpose", default="")


def is_drill_id(value: Any) -> bool:
    """chat_id / conv_id / 三段式 id 的尾段落在演练号段 → True。"""
    tail = str(value or "").rsplit(":", 1)[-1].strip()
    if not tail.isdigit():
        return False
    n = int(tail)
    return any(lo <= n <= hi for lo, hi in _DRILL_RANGES)


@contextlib.contextmanager
def purpose_scope(purpose: str):
    """``with purpose_scope("translate"): await ai_client.chat(...)`` → 这段里的记账都归该用途。"""
    token = _PURPOSE_VAR.set(str(purpose or ""))
    try:
        yield
    finally:
        _PURPOSE_VAR.reset(token)


def purpose_for_reply(context: Optional[Dict[str, Any]]) -> str:
    """出稿链用途：context[_llm_purpose] > purpose_scope > 演练号 → drill > customer_reply。"""
    ctx = context or {}
    explicit = str(ctx.get("_llm_purpose") or "").strip()
    if explicit in KNOWN_PURPOSES:
        return explicit
    scoped = _PURPOSE_VAR.get()
    if scoped in KNOWN_PURPOSES:
        return scoped
    for k in ("chat_id", "conversation_id", "user_id", "peer_id"):
        if is_drill_id(ctx.get(k)):
            return PURPOSE_DRILL
    return PURPOSE_CUSTOMER_REPLY


__all__ = [
    "KNOWN_PURPOSES", "PURPOSE_ASSISTANT", "PURPOSE_COLLOQUIAL",
    "PURPOSE_CUSTOMER_REPLY", "PURPOSE_DRILL", "PURPOSE_EVAL", "PURPOSE_KB",
    "PURPOSE_MEMORY_EXTRACT", "PURPOSE_PERSONA", "PURPOSE_PROBE", "PURPOSE_TOOL",
    "PURPOSE_TRANSLATE", "PURPOSE_UNKNOWN", "PURPOSE_VISION",
    "_PURPOSE_VAR", "is_drill_id", "purpose_for_reply", "purpose_scope",
]
