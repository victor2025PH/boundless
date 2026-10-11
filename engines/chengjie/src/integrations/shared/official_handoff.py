# -*- coding: utf-8 -*-
"""官方通道「进待人工」统一入口（WhatsApp Cloud / Telegram Bot 自答路径共用，2026-10-08）。

主线「需人工」（``protocol_autoreply.tag_needs_human``）只挂在主管道 / 草稿链；官方 webhook
的自答路径（``use_pipeline=False``）原本：AI 异常 → 只打日志；空回复 → 静默；发送失败 →
只打日志；客户点名要真人 → AI 照答。坐席台看不到这些「客户在等」的会话。

本模块：
- ``human_request_hits(text)``：客户明确要真人（「转人工」「真人」「talk to a human」…）。
- ``tag_official_handoff(platform, account_id, chat_key, reason, ...)``：经 inbox store 打
  「需人工」标（同一入口、同一元数据），store 不可用时只计数 + 日志（不抛）。
- ``handoff_snapshot(platform)``：按原因计数，供健康面板。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: 官方自答路径的转人工原因（与 ``protocol_autoreply.HANDOFF_REASONS`` 同名的复用同名）。
OFFICIAL_HANDOFF_REASONS = frozenset({
    "generate_error", "empty_reply", "send_error", "human_request", "media_inbound",
    "window_expired", "delivery_failed",
})

_HUMAN_REQUEST_RE = re.compile(
    r"转人工|人工客服|找人工|要人工|接人工|真人客服|找真人|要真人|是真人吗|有没有人工"
    r"|(?<![A-Za-z])(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?:real\s+)?(?:human|person|agent|operator|representative)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:real|live)\s+(?:human|person|agent)(?![A-Za-z])"
    r"|(?<![A-Za-z])human\s+agent(?![A-Za-z])",
    re.IGNORECASE,
)

_lock = threading.Lock()
_counts: Dict[str, Dict[str, Any]] = {}


def human_request_hits(text: Any) -> List[str]:
    s = str(text or "")
    if not s.strip():
        return []
    return [m.group(0)[:40] for m in _HUMAN_REQUEST_RE.finditer(s)][:3]


def _bump(platform: str, reason: str, tagged: bool) -> None:
    try:
        with _lock:
            row = _counts.setdefault(platform, {"total": 0, "tagged": 0, "by_reason": {},
                                                "last_ts": 0.0, "last_reason": ""})
            row["total"] += 1
            if tagged:
                row["tagged"] += 1
            row["by_reason"][reason] = int(row["by_reason"].get(reason) or 0) + 1
            row["last_ts"] = time.time()
            row["last_reason"] = reason
    except Exception:
        logger.debug("[official-handoff] 计数失败（忽略）", exc_info=True)


def handoff_snapshot(platform: str = "") -> Dict[str, Any]:
    with _lock:
        if platform:
            row = _counts.get(platform) or {"total": 0, "tagged": 0, "by_reason": {},
                                            "last_ts": 0.0, "last_reason": ""}
            out = dict(row)
            out["by_reason"] = dict(row.get("by_reason") or {})
            return out
        return {k: dict(v, by_reason=dict(v.get("by_reason") or {})) for k, v in _counts.items()}


def reset_for_tests() -> None:
    with _lock:
        _counts.clear()


def tag_official_handoff(platform: str, account_id: str, chat_key: str, reason: str, *,
                         hits: Optional[Iterable[str]] = None, store: Any = None) -> bool:
    """打「需人工」标（best-effort，绝不抛）。返回是否真的写进了 store。"""
    plat = str(platform or "").lower()
    rsn = str(reason or "").strip() or "generate_error"
    tagged = False
    try:
        st = store
        if st is None:
            from src.integrations.protocol_bridge import get_inbox_store
            st = get_inbox_store()
        if st is not None and chat_key:
            from src.integrations.protocol_autoreply import tag_needs_human
            tagged = bool(tag_needs_human(
                st, {"platform": plat, "account_id": str(account_id or "default"),
                     "chat_key": str(chat_key)},
                reason=rsn, source="system", hits=list(hits or [])))
    except Exception:
        logger.debug("[official-handoff] tag_needs_human 失败", exc_info=True)
    _bump(plat, rsn, tagged)
    logger.info("[official-handoff] platform=%s account=%s reason=%s tagged=%s",
                plat, account_id, rsn, tagged)
    return tagged


__all__ = ["OFFICIAL_HANDOFF_REASONS", "human_request_hits", "tag_official_handoff",
           "handoff_snapshot", "reset_for_tests"]
