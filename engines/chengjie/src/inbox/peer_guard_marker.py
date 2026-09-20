# -*- coding: utf-8 -*-
"""peer_bot_guard「硬拦拟稿」会话标（Q-30 C #312 #314 可见化，2026-09-12）。

背景：``autodraft_helpers`` 的 B 线守卫 ``guard_auto_draft_action`` 判 ``colleague:<id>`` /
``ops_group:<id>``（Q-23 永不自动回复名单）或机器人对端时**直接 return 不拟稿**，此前只有一行
``[AutoDraft] peer_bot_guard 跳过拟稿`` 日志——界面零提示，状态带仍是绿色「AI 会自动回」。
jun #312 / #314 就是自测账号在名单里而以为「识图坏了」。

本模块只让跳过**可见**：跳过处写一条 ``peer_guard_skip:<cid>``＝``{ts, reason, soft}``，
``conv_state`` 读成 ``held``（reason_code 原样 ``colleague:<id>`` / ``ops_group:<id>`` / 其它
守卫码；文案键按前缀选）；该会话下一次守卫放行即清。

存储：复用 ``InboxStore`` 通用 KV ``app_settings``（与 ``ai_fail_marker`` 同模式，不动 store.py
/ 不建表）。全部 best-effort：任何异常只 debug，绝不影响拟稿主链。**不动守卫判定**（红线）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "peer_guard_skip:"
#: 原因码前缀 → 状态带文案键（conv_state 用；未列出的守卫码走 peer_guard 通用文案）
TEXT_KEYS = {
    "colleague": "inbox.cs.held.colleague",
    "ops_group": "inbox.cs.held.ops_group",
}
GENERIC_TEXT_KEY = "inbox.cs.held.peer_guard"


def _key(cid: str) -> str:
    return KEY_PREFIX + str(cid or "").strip()


def text_key_for(reason: str) -> str:
    """``colleague:123`` → ``inbox.cs.held.colleague``；``ops_group:-100…`` → ``…ops_group``；
    其它（tg_is_bot / repeat / instant / daily_budget …）→ 通用 ``…peer_guard``。"""
    r = str(reason or "").strip().lower()
    head = r.split(":", 1)[0]
    return TEXT_KEYS.get(head, GENERIC_TEXT_KEY)


def split_reason(reason: str) -> "tuple[str, str]":
    """``colleague:8852939166`` → ``("colleague", "8852939166")``；无冒号 → ``(code, "")``。"""
    r = str(reason or "").strip()
    if ":" in r:
        head, tail = r.split(":", 1)
        return head.strip().lower(), tail.strip()
    return r.lower(), ""


def mark(store: Any, cid: str, *, reason: str, soft: bool = False,
         ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """写跳过标（覆盖旧值）。绝不抛；store 缺席 / reason 空 → None。"""
    cid = str(cid or "").strip()
    r = str(reason or "").strip()
    if not cid or not r or store is None or not hasattr(store, "set_app_setting"):
        return None
    rec = {"ts": float(ts or time.time()), "reason": r[:96], "soft": bool(soft)}
    try:
        store.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False),
                              updated_by="peer_guard")
        return rec
    except TypeError:
        try:
            store.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False))
            return rec
        except Exception:
            logger.debug("[peer_guard_marker] 写入失败（忽略）", exc_info=True)
            return None
    except Exception:
        logger.debug("[peer_guard_marker] 写入失败（忽略）", exc_info=True)
        return None


def clear(store: Any, cid: str) -> bool:
    """守卫放行 → 清标（值空串 = 删键）。只在标在场时写，省一次无谓落盘。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return False
    try:
        if get(store, cid) is None:
            return False
        store.set_app_setting(_key(cid), "")
        return True
    except Exception:
        logger.debug("[peer_guard_marker] 清除失败（忽略）", exc_info=True)
        return False


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(str(raw or "") or "{}")
        if isinstance(got, dict) and str(got.get("reason") or ""):
            return got
    except Exception:
        pass
    return None


def get(store: Any, cid: str) -> Optional[Dict[str, Any]]:
    """读某会话跳过标；无 / 脏 → None。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return None
    try:
        return _parse(store.get_app_setting(_key(cid), ""))
    except Exception:
        return None


__all__ = ["KEY_PREFIX", "TEXT_KEYS", "GENERIC_TEXT_KEY", "mark", "clear", "get",
           "text_key_for", "split_reason"]
