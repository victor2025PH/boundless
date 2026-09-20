# -*- coding: utf-8 -*-
"""要照片门禁（Q-6 F · #263 · 2026-09-10）。

发版门禁「10 条要照片只发图或拒绝」：出站正文在**没有真发媒体**时不得声称
已发 / 正在发 / 稍后发。Q-2 F ``commitment_gate`` 未开工，本文件自建同口径
纯函数，R79 可并入。

不发送、不改稿——只判定。改写仍走 P-3 ``autosend_helpers`` / Q-6 blocked rewrite。
"""
from __future__ import annotations

from typing import Any

__all__ = ["outbound_ok_for_photo_ask"]


def outbound_ok_for_photo_ask(text: Any, *, media_sent: bool) -> bool:
    """``media_sent=True``（本轮已真发）→ 放行；否则承诺/断言/假声明任一命中 → False。"""
    if media_sent:
        return True
    t = str(text or "").strip()
    if not t:
        return True
    try:
        from src.ai.outbound_promise_guard import (
            detect_media_claim,
            detect_media_promise,
            detect_sent_claim,
        )
    except Exception:
        return True
    if detect_media_promise(t):
        return False
    if detect_media_claim(t, media_context=True):
        return False
    if detect_sent_claim(t):
        return False
    return True
