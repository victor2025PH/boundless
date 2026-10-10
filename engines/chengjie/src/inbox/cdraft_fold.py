# -*- coding: utf-8 -*-
"""Composer 草稿条默认只露一条原因。停联和要钱不折进「查看」。

风险、语言、知识库、记忆留在展开区。这里只给列表接口一个 ``sticky_alert``：
``stop`` / ``money`` / ``""``。``money_mention`` 是叙述降级，不算要钱。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, List

_STOP = {"stop_contact"}
_MONEY = {
    "money",
    "money_request",
    "request:money",
    "credential_or_payment_request",
    "commitment:money",
}


def _reasons(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                return [text]
            raw = parsed
        else:
            return [text]
    if isinstance(raw, dict):
        return [str(raw.get("reason") or raw.get("id") or "")]
    if isinstance(raw, Iterable):
        return [str(x or "") for x in raw]
    return [str(raw)]


def _is_stop(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    return r in _STOP or r.startswith("stop_contact:")


def _is_money(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    if r in _MONEY or r.startswith("request:money"):
        return True
    return False


def sticky_alert(risk_reasons: Any) -> str:
    """停联优先于要钱。都没有 → 空串。"""
    items = _reasons(risk_reasons)
    if any(_is_stop(x) for x in items):
        return "stop"
    if any(_is_money(x) for x in items):
        return "money"
    return ""
