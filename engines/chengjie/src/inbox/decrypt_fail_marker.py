# -*- coding: utf-8 -*-
"""WhatsApp 解密失败（Bad MAC）会话琥珀 note（#279 P2-2）。

边车已会删 Signal 会话重建；此前 CIPHERTEXT stub 无 ``message`` 体，
``pushWaMessage`` 直接 return，收件箱看不到客户发过东西。本模块只让坐席看见
「有一条消息解不开，会话在重建」——不改状态、不触发自动回复。

存储：InboxStore ``app_settings``，键 ``decrypt_fail:<cid>``。明文入站后清。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "decrypt_fail:"
DEFAULT_TTL_SEC = 24 * 3600
_REPEAT_WINDOW_SEC = 6 * 3600
#: 线程气泡正文（与边车 DECRYPT_FAIL_PLACEHOLDER 对齐）
PLACEHOLDER_TEXT = "[无法解密的消息 · 会话将自动重建]"
#: 会话列表预览——系统口径，不看起来像客户原话
LIST_PREVIEW = "一条消息无法解密"


def _key(cid: str) -> str:
    return KEY_PREFIX + str(cid or "").strip()


def _default_store() -> Any:
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(str(raw or "") or "{}")
        if isinstance(got, dict) and got.get("ts"):
            return got
    except Exception:
        pass
    return None


def mark(cid: str, *, store: Any = None, ts: Optional[float] = None,
         streak: int = 0) -> Optional[Dict[str, Any]]:
    cid = str(cid or "").strip()
    if not cid:
        return None
    st = store if store is not None else _default_store()
    if st is None or not hasattr(st, "set_app_setting"):
        return None
    now = float(ts or time.time())
    prev = get(cid, store=st, now=now, ttl_sec=_REPEAT_WINDOW_SEC)
    rec = {
        "ts": now,
        "first_ts": float((prev or {}).get("first_ts") or now),
        "n": int((prev or {}).get("n") or 0) + 1,
        "streak": int(streak or 0),
    }
    try:
        st.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False),
                           updated_by="decrypt_fail")
        return rec
    except Exception:
        logger.debug("[decrypt_fail_marker] 写入失败（忽略）", exc_info=True)
        return None


def clear(cid: str, *, store: Any = None) -> bool:
    cid = str(cid or "").strip()
    if not cid:
        return False
    st = store if store is not None else _default_store()
    if st is None or not hasattr(st, "set_app_setting") or not hasattr(st, "get_app_setting"):
        return False
    try:
        if not str(st.get_app_setting(_key(cid), "") or ""):
            return False
        st.set_app_setting(_key(cid), "")
        return True
    except Exception:
        logger.debug("[decrypt_fail_marker] 清除失败（忽略）", exc_info=True)
        return False


def get(cid: str, *, store: Any = None, now: Optional[float] = None,
        ttl_sec: float = DEFAULT_TTL_SEC) -> Optional[Dict[str, Any]]:
    cid = str(cid or "").strip()
    st = store if store is not None else _default_store()
    if not cid or st is None or not hasattr(st, "get_app_setting"):
        return None
    try:
        rec = _parse(st.get_app_setting(_key(cid), ""))
    except Exception:
        return None
    if not rec:
        return None
    try:
        age = float(now if now is not None else time.time()) - float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ttl_sec and age > float(ttl_sec):
        return None
    rec["age_sec"] = round(max(0.0, age), 0)
    return rec


def hhmm(ts: Any) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return "--:--"


__all__ = ["KEY_PREFIX", "DEFAULT_TTL_SEC", "mark", "clear", "get", "hhmm"]
