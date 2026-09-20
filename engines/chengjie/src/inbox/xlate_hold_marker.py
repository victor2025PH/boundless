# -*- coding: utf-8 -*-
"""「出站翻译 HOLD」会话 note（Q-39 B #326 THBSHN，2026-09-15）。

背景：Telegram kj × Vince 03:56，草稿中文、``[lang-plan] reply=en``，``[xlate] provider=none
err=ai:empty`` → ``outbound_translate`` HOLD → ``autosend_worker`` ``translate_hold:ai:empty``。
纪律（不发原文）正确，但坐席侧只有一条审计行——状态带全程绿色，用户只会看到「AI 没回」。

本模块只让它**可见**：HOLD（同引擎重试 + 换引擎 + 重起草三步都不行之后）处写一条
``xlate_hold:<cid>={ts, reason, target, draft_id, attempts, text}`` → ``conv_state.compute`` 读它
→ 状态带红色「翻译引擎没回话 · 这条没发 · {hhmm}」+ 动作「重试翻译」
（``POST /api/unified-inbox/drafts/{draft_id}/retranslate``）；同会话下一次翻译**成功**即清。

存储：复用 ``InboxStore`` 通用 KV ``app_settings``（与 ``album_miss_marker`` / ``ai_fail_marker``
同款，不动 store.py / 不建表）。``outbound_translate`` 有 store 句柄直接传；拿不到 → 静默 no-op。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "xlate_hold:"
#: 超过这个时长的 note 视为陈旧，状态带不再显示（翻译链故障通常小时级恢复）
DEFAULT_TTL_SEC = 24 * 3600

#: 「引擎没回话」族（状态带文案「翻译引擎没回话」）；其余原因走「翻译结果不可用（{reason}）」
ENGINE_SILENT_TOKENS = ("empty", "timeout", "Timeout", "provider_unavailable", "translate_failed",
                        "translate_exception", "unavailable", "Connect", "Connection",
                        "ServerDisconnected")


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


def is_engine_silent(reason: str) -> bool:
    """``ai:empty`` / ``ai:TimeoutError: …`` / ``provider_unavailable`` → True（引擎没回话）。"""
    r = str(reason or "").strip()
    if not r:
        return False
    tail = r.split(":", 1)[1].strip() if ":" in r else r
    return any(tail.startswith(t) or r == t for t in ENGINE_SILENT_TOKENS)


def mark(cid: str, *, reason: str = "", target: str = "", draft_id: str = "",
         attempts: int = 0, store: Any = None, ts: Optional[float] = None,
         text: str = "") -> Optional[Dict[str, Any]]:
    """写 note（同会话覆盖：最新一次 HOLD 为准）。绝不抛；拿不到 store → None。"""
    cid = str(cid or "").strip()
    if not cid:
        return None
    st = store if store is not None else _default_store()
    if st is None or not hasattr(st, "set_app_setting"):
        return None
    now = float(ts or time.time())
    prev = get(cid, store=st, now=now, ttl_sec=6 * 3600)
    rec = {
        "ts": now,
        "first_ts": float((prev or {}).get("first_ts") or now),
        "n": int((prev or {}).get("n") or 0) + 1,
        "reason": str(reason or "hold")[:80],
        "target": str(target or "")[:16],
        "draft_id": str(draft_id or "")[:80],
        "attempts": int(attempts or 0),
        "text": str(text or "").replace("\n", " ").strip()[:120],
    }
    try:
        st.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False), updated_by="xlate_hold")
        return rec
    except Exception:
        logger.debug("[xlate_hold_marker] 写入失败（忽略）", exc_info=True)
        return None


def clear(cid: str, *, store: Any = None) -> bool:
    """翻译成功 / 重试成功后清 note（值空串 = 删键）。没有 note 时零写。绝不抛。"""
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
        logger.debug("[xlate_hold_marker] 清除失败（忽略）", exc_info=True)
        return False


def get(cid: str, *, store: Any = None, now: Optional[float] = None,
        ttl_sec: float = DEFAULT_TTL_SEC) -> Optional[Dict[str, Any]]:
    """读某会话 note；无 / 脏 / 超 ``ttl_sec`` → None（陈旧不删，只是不显示）。"""
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


__all__ = ["KEY_PREFIX", "DEFAULT_TTL_SEC", "mark", "clear", "get", "hhmm", "is_engine_silent"]
