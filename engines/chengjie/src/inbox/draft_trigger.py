# -*- coding: utf-8 -*-
"""拟稿触发源 reason 注册表（Q-3 #264 E，2026-09-10）。

``auto_generate_draft`` 的每一次触发都要能在日志里回答「这条稿为什么被（重新）起草」：
``new_inbound``（客户新入站，缺省）/ ``catchup_regen``（复班补觉重拟）/ ``risk_hold_regen``
（会话级风险持有把 L2 稿取消后按 L1 重拟）/ ``takeover_rearm`` / ``review_timeout`` /
``user_ignore``（预留：接回 / 审稿超时 / 坐席忽略后的重拟）。

drafts.py 是 Q-2 的文件，签名不动——触发方在**调用侧**先 ``note(conv, reason)``，
``autodraft_helpers`` 在调 ``auto_generate_draft`` 前 ``pop(conv)`` 取走并落一行
``[draft] trigger conv=… reason=…``。进程级小表（上限 2000、TTL 10 min），任何异常吞掉。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

REASONS = ("new_inbound", "catchup_regen", "risk_hold_regen",
           "takeover_rearm", "review_timeout", "user_ignore")
DEFAULT_REASON = "new_inbound"

_lock = threading.Lock()
_REG: Dict[str, Tuple[str, float]] = {}
_MAX = 2000
_TTL = 600.0


def note(conversation_id: str, reason: str) -> None:
    """登记本会话**下一次**拟稿的触发原因（被 pop 一次即消费）。"""
    k = str(conversation_id or "")
    r = str(reason or "").strip()
    if not k or not r:
        return
    now = time.time()
    with _lock:
        _REG[k] = (r, now)
        if len(_REG) > _MAX:
            cutoff = now - _TTL
            for kk in [x for x, (_, ts) in _REG.items() if ts < cutoff]:
                _REG.pop(kk, None)
            if len(_REG) > _MAX:
                for kk in sorted(_REG, key=lambda x: _REG[x][1])[: len(_REG) - _MAX]:
                    _REG.pop(kk, None)


def pop(conversation_id: str) -> str:
    """取走并清除；无 / 过期 → ``""``。绝不抛。"""
    try:
        with _lock:
            rec = _REG.pop(str(conversation_id or ""), None)
        if not rec or time.time() - rec[1] > _TTL:
            return ""
        return rec[0]
    except Exception:
        return ""


def peek(conversation_id: str) -> str:
    try:
        rec = _REG.get(str(conversation_id or ""))
        return rec[0] if rec else ""
    except Exception:
        return ""


def _reset_for_tests() -> None:
    with _lock:
        _REG.clear()


__all__ = ["REASONS", "DEFAULT_REASON", "note", "pop", "peek"]
