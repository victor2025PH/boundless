# -*- coding: utf-8 -*-
"""引流联系方式解析 — 支持多通道字符串 / JSON / key:value 行。

供 Messenger 自动回复、``_ai_reply_and_send`` 等按 persona 首推渠道选模板。

支持格式示例::

    @mylineid
    line:@mylineid
    wa:+8190...
    {"line": "@x", "whatsapp": "+81..."}
    line:@a | wa:+81...
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_KNOWN = frozenset({"line", "instagram", "whatsapp", "telegram"})


def _norm_key(k: str) -> Optional[str]:
    k = (k or "").lower().strip()
    aliases = {
        "ig": "instagram",
        "wa": "whatsapp",
        "tg": "telegram",
        "tel": "telegram",
    }
    k = aliases.get(k, k)
    return k if k in _KNOWN else None


def parse_referral_channels(raw: str) -> Dict[str, str]:
    """把任意``referral_contact`` 配置解析成 ``{channel: value}``。

    无法识别结构时 → ``{"_default": 整段原文}``，供 ``pick_referral_for_persona`` 用首推渠道消费。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    # JSON object
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                out: Dict[str, str] = {}
                for k, v in d.items():
                    nk = _norm_key(str(k))
                    if nk and v:
                        out[nk] = str(v).strip()
                return out if out else {"_default": raw}
        except json.JSONDecodeError:
            pass
    # multi-line or | separated  key:value
    out = {}
    for chunk in re.split(r"[\n|]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            k, _, rest = chunk.partition(":")
            nk = _norm_key(k)
            if nk and rest.strip():
                out[nk] = rest.strip()
                continue
        low = chunk.lower()
        for prefix, ch in (
            ("line:", "line"),
            ("instagram:", "instagram"),
            ("whatsapp:", "whatsapp"),
            ("telegram:", "telegram"),
            ("wa:", "whatsapp"),
            ("ig:", "instagram"),
            ("tg:", "telegram"),
        ):
            if low.startswith(prefix):
                val = chunk[len(prefix) :].strip()
                if val:
                    out[ch] = val
                break
    if out:
        return out
    return {"_default": raw}


def pick_referral_for_persona(
    channels: Dict[str, str],
    persona_key: Optional[str] = None,
) -> Tuple[str, str]:
    """按 persona 的 ``referral_priority`` 选首推值。(value, channel)。

    ``channels`` 可含 ``_default``：在首推渠道无值时，用默认串 + 首推渠道名发 ``get_referral_snippet``。
    """
    try:
        from src.host.fb_target_personas import get_referral_priority

        pri: List[str] = get_referral_priority(persona_key) or ["whatsapp"]
    except Exception as e:
        logger.debug("[referral_contact] get_referral_priority 失败: %s", e)
        pri = ["whatsapp", "telegram", "instagram", "line"]

    default_blob = (channels.get("_default") or "").strip()
    for ch in pri:
        v = (channels.get(ch) or "").strip()
        if v:
            return v, ch
    if default_blob:
        return default_blob, (pri[0] if pri else "whatsapp")
    return "", ""


def format_contact_for_chat_brain(raw: str) -> str:
    """给 ChatBrain ``contact_info`` 用：保留可读的一行摘要。"""
    m = parse_referral_channels(raw)
    if not m:
        return ""
    if len(m) == 1 and "_default" in m:
        return m["_default"]
    parts = [f"{k}:{v}" for k, v in sorted(m.items()) if k != "_default" and v]
    if "_default" in m and m["_default"]:
        parts.append(m["_default"])
    return " | ".join(parts) if parts else raw.strip()
