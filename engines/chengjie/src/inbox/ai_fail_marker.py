# -*- coding: utf-8 -*-
"""「AI 本轮未生成」会话灰标（Q-14 #262 B，2026-09-09）。

背景：AI 主链失败 → ``_fallback_reply`` 返回 None = 本轮不回（罐头兜底 08-15 已移除，
口径不变），此前对坐席**完全不可见**——工作台没有任何标记、日志只有一句「两次调用均失败」。
本模块只让失败**可见**：起草失败处写一条 ``last_ai_fail={ts, reason, …}``，工作台会话头
出一条灰标「AI 本轮未生成（超时 · 23:41）」+「重试起草」，下一次该会话 AI 成功即清。

存储：复用 ``InboxStore`` 既有通用 KV ``app_settings``（键 ``ai_last_fail:<cid>``，值 JSON），
**不动 store.py / 不建表**。读侧 ``list_app_settings(prefix)`` 一次取全（会话列表用）。

reason 口径（与 ``AIClient._classify_ai_error`` 同源）：
``timeout | connect | gateway_5xx | auth | no_key | empty | other | unknown``。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "ai_last_fail:"
REASONS = ("timeout", "connect", "gateway_5xx", "auth", "no_key", "empty", "other", "unknown")


def _key(cid: str) -> str:
    return KEY_PREFIX + str(cid or "").strip()


def mark(store: Any, cid: str, *, reason: str, ts: Optional[float] = None,
         latency_ms: int = 0, attempt: int = 0, draft_id: str = "",
         model: str = "", request_id: str = "") -> Optional[Dict[str, Any]]:
    """写灰标（覆盖旧值）。绝不抛；store 缺席 → None。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return None
    r = str(reason or "unknown").strip().lower() or "unknown"
    if r not in REASONS:
        r = "other"
    rec = {
        "ts": float(ts or time.time()),
        "reason": r,
        "latency_ms": int(latency_ms or 0),
        "attempt": int(attempt or 0),
        "draft_id": str(draft_id or ""),
        "model": str(model or ""),
        "request_id": str(request_id or ""),
    }
    try:
        store.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False), updated_by="ai_fail")
        return rec
    except Exception:
        logger.debug("[ai_fail_marker] 写入失败（忽略）", exc_info=True)
        return None


def clear(store: Any, cid: str) -> bool:
    """清灰标（值空串 = 删键）。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return False
    try:
        store.set_app_setting(_key(cid), "")
        return True
    except Exception:
        logger.debug("[ai_fail_marker] 清除失败（忽略）", exc_info=True)
        return False


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(str(raw or "") or "{}")
        if isinstance(got, dict) and got.get("reason"):
            return got
    except Exception:
        pass
    return None


def get(store: Any, cid: str) -> Optional[Dict[str, Any]]:
    """读某会话灰标；无 / 脏 → None。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return None
    try:
        return _parse(store.get_app_setting(_key(cid), ""))
    except Exception:
        return None


def all_marks(store: Any) -> Dict[str, Dict[str, Any]]:
    """全部灰标 ``{cid: rec}``（会话列表一次取全）。"""
    out: Dict[str, Dict[str, Any]] = {}
    if store is None or not hasattr(store, "list_app_settings"):
        return out
    try:
        for row in store.list_app_settings(KEY_PREFIX) or []:
            k = str(row.get("key") or "")
            if not k.startswith(KEY_PREFIX):
                continue
            rec = _parse(row.get("value"))
            if rec:
                out[k[len(KEY_PREFIX):]] = rec
    except Exception:
        logger.debug("[ai_fail_marker] 列举失败（忽略）", exc_info=True)
    return out


def consume_and_mark(ai_client: Any, store: Any, cid: str, *, draft_id: str = "",
                     stage: str = "autodraft") -> Optional[Dict[str, Any]]:
    """起草侧「接住点」：取走 ``AIClient.pop_last_fail(cid)`` 的失败记录 → 落一行
    ``[ai] fail conv=… reason=… stage=… draft_id=…`` 日志（ai_client 抛出点那一行带
    latency / attempt，本行带 draft_id / stage，各一行不重复）→ 写灰标。

    ai_client 没有该会话记录（失败发生在 AI 之外，如复读闸判失败）→ reason=unknown。"""
    rec: Optional[Dict[str, Any]] = None
    try:
        if ai_client is not None and hasattr(ai_client, "pop_last_fail"):
            rec = ai_client.pop_last_fail(cid)
    except Exception:
        rec = None
    reason = str((rec or {}).get("reason") or "unknown")
    logger.error("[ai] fail conv=%s reason=%s stage=%s draft_id=%s request_id=%s",
                 cid, reason, stage, draft_id or "-",
                 str((rec or {}).get("request_id") or "") or "n/a")
    return mark(store, cid, reason=reason,
                ts=float((rec or {}).get("ts") or time.time()),
                latency_ms=int((rec or {}).get("latency_ms") or 0),
                attempt=int((rec or {}).get("attempt") or 0),
                draft_id=draft_id, model=str((rec or {}).get("model") or ""),
                request_id=str((rec or {}).get("request_id") or ""))


def hhmm(ts: Any) -> str:
    """灰标文案里的时刻（本地钟 HH:MM）。"""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return "--:--"


__all__ = ["KEY_PREFIX", "REASONS", "mark", "clear", "get", "all_marks",
           "consume_and_mark", "hhmm"]
