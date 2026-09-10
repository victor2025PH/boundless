# -*- coding: utf-8 -*-
"""会话级媒体承诺封锁 + 质问螺旋（Q-6 C/D · #263 #266 · 2026-09-10）。

P-3 已在投递层把 lie_caught 做成动作（重发 / 实话 / 连续第二条转人工）。本模块补
**会话级**状态，不重做那条动作链：

- 一次质问后 ``media_claim_blocked`` **10 轮**：任何照片承诺句一律改写 / 拦下；
- spiral 阈值 = **同会话 2 次**质问（不要求连续）→ 降级审核 + 需人工；
- 进程内表（``store.py`` 白名单列碰不得；10 轮对话通常活在同一进程）。

生成侧（``persona_reply``）读 ``is_blocked`` 跳过 goal-inject，并在 extra_hint
注入纠正指令。出站侧 ``outbound_promise_guard.apply_blocked_media_rewrite`` 剥承诺。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BLOCK_TURNS = 10
SPIRAL_THRESHOLD = 2

_LOCK = threading.Lock()
# conv → {turns_left, spiral_n, ts, last_kind}
_STATE: Dict[str, Dict[str, Any]] = {}

__all__ = [
    "BLOCK_TURNS", "SPIRAL_THRESHOLD",
    "note_lie_caught", "is_blocked", "spiral_count",
    "tick_outbound", "blocked_addendum", "reset_for_tests",
]


def _key(conv: Any) -> str:
    return str(conv or "").strip()


def reset_for_tests() -> None:
    """单测隔离：清空进程表。"""
    with _LOCK:
        _STATE.clear()


def note_lie_caught(conv: Any, *, kind: str = "lie_caught", now: Optional[float] = None) -> Dict[str, Any]:
    """入站质问一次：spiral +1，重置 10 轮封锁。返回当前记录（含 spiral_n）。"""
    ck = _key(conv)
    if not ck:
        return {"turns_left": 0, "spiral_n": 0, "ts": 0.0, "last_kind": ""}
    ts = float(now if now is not None else time.time())
    with _LOCK:
        rec = _STATE.get(ck) or {
            "turns_left": 0, "spiral_n": 0, "ts": 0.0, "last_kind": "",
        }
        rec["spiral_n"] = int(rec.get("spiral_n") or 0) + 1
        rec["turns_left"] = BLOCK_TURNS
        rec["ts"] = ts
        rec["last_kind"] = str(kind or "lie_caught")
        _STATE[ck] = rec
        out = dict(rec)
    logger.warning(
        "[media_claim_block] conv=%s kind=%s spiral=%s blocked=%s",
        ck, kind, out["spiral_n"], out["turns_left"],
    )
    return out


def is_blocked(conv: Any) -> bool:
    ck = _key(conv)
    if not ck:
        return False
    with _LOCK:
        rec = _STATE.get(ck) or {}
        return int(rec.get("turns_left") or 0) > 0


def spiral_count(conv: Any) -> int:
    ck = _key(conv)
    if not ck:
        return 0
    with _LOCK:
        return int((_STATE.get(ck) or {}).get("spiral_n") or 0)


def tick_outbound(conv: Any) -> bool:
    """每轮 AI 出站扣 1。返回扣完后是否仍封锁。"""
    ck = _key(conv)
    if not ck:
        return False
    with _LOCK:
        rec = _STATE.get(ck)
        if not rec:
            return False
        left = int(rec.get("turns_left") or 0)
        if left <= 0:
            return False
        rec["turns_left"] = left - 1
        still = rec["turns_left"] > 0
        if not still:
            rec["turns_left"] = 0
        return still


def blocked_addendum(conv: Any, *, lang: str = "zh") -> str:
    """封锁期内注入生成侧：不许再承诺发图、不许岔题。空 conv / 未封锁 → ""。"""
    if not is_blocked(conv):
        return ""
    n = spiral_count(conv)
    lg = str(lang or "zh").lower()
    if n >= SPIRAL_THRESHOLD:
        if lg.startswith("en"):
            return (
                "【media_claim_blocked】This chat is in photo-complaint escalation. "
                "Stay on this topic. Do not change the subject, do not promise a photo, "
                "do not say it was sent / still loading / try again / forgot. "
                "A human will take over."
            )
        return (
            "【media_claim_blocked】客户已两次质问没收到照片，会话已转人工。"
            "不要岔题、不要再承诺发图，禁止说已经发了/加载中/再试/忘了。"
        )
    if lg.startswith("en"):
        return (
            "【media_claim_blocked】The other person said they didn't get the photo. "
            "Admit it was not sent if nothing was delivered. "
            "Forbidden: still loading / try again / I forgot / I'll send later. "
            "Do not promise a new photo. Do not change the subject."
        )
    return (
        "【media_claim_blocked】客户说没收到照片。若系统从未真发出过，必须承认没发出去。"
        "禁止说加载慢/再试一次/忘了/稍后发。禁止承诺新照片。本轮不要岔题。"
    )
