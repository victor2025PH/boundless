# -*- coding: utf-8 -*-
"""
Telegram 告警/通知投递目标解析。

支持：数字用户 ID、@公开用户名、超级群/频道 ID（-100…）。
不支持作为 chat_id 直接发送：t.me/+ 邀请链接（需先入群并取得 -100… 或 @公开群名）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_INVITE_LINK = re.compile(
    r"(?:https?://)?t\.me/\+[\w-]+|(?:https?://)?t\.me/joinchat/[\w-]+",
    re.IGNORECASE,
)


def _norm_key(s: str) -> str:
    x = str(s).strip()
    if not x:
        return ""
    low = x.lower()
    if low.startswith("@"):
        return low[1:]
    return low


def parse_line(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析单行目标。返回 (chat_id 传给 Bot API, 跳过原因)。
    跳过原因: invite_link / empty
    """
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None, "empty"
    if _INVITE_LINK.search(line):
        return None, "invite_link"
    # 去掉多余包裹引号
    if (line.startswith('"') and line.endswith('"')) or (line.startswith("'") and line.endswith("'")):
        line = line[1:-1].strip()
    return line, None


def lines_to_recipients(text: str) -> List[str]:
    """多行/逗号分隔 → 去重列表（保留首次出现顺序）。"""
    if not (text or "").strip():
        return []
    parts: List[str] = []
    for chunk in text.replace(",", "\n").split("\n"):
        for sub in chunk.split(";"):
            s = sub.strip()
            if s:
                parts.append(s)
    out: List[str] = []
    seen: Set[str] = set()
    for p in parts:
        tid, reason = parse_line(p)
        if tid is None:
            if reason == "invite_link":
                logger.warning(
                    "[Telegram] 跳过邀请链接（无法作为 chat_id 直接发送）: %s",
                    p[:80],
                )
            continue
        k = _norm_key(tid)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(tid)
    return out


def has_user_telegram_destination(telegram_block: Optional[Dict]) -> bool:
    """是否配置了主 chat_id 或 recipients（不含环境变量副本）。"""
    tg = telegram_block or {}
    if (str(tg.get("chat_id") or "").strip()) or (str(tg.get("channel_id") or "").strip()):
        return True
    rec = tg.get("recipients")
    if isinstance(rec, list):
        return any(str(x).strip() for x in rec)
    if isinstance(rec, str) and rec.strip():
        return bool(lines_to_recipients(rec))
    return False


def cc_telegram_chat_id_from_env() -> Optional[str]:
    """兼容 TELEGRAM_CC_CHAT_ID；未设置环境变量时默认副本 ID（与 studio_notifier 一致）。"""
    raw = os.getenv("TELEGRAM_CC_CHAT_ID")
    if raw is not None:
        r = raw.strip()
        return r if r else None
    return "6107037825"


def expand_telegram_notify_targets(telegram_block: Dict) -> List[str]:
    """
    合并主 chat_id、recipients 列表、环境变量副本（去重）。

    telegram_block 键:
      - chat_id / channel_id: 主目标
      - recipients: List[str] 额外目标（已由前端/API 拆成行）
    """
    out: List[str] = []
    seen: Set[str] = set()

    def push(raw: Optional[str]) -> None:
        if not raw:
            return
        tid, reason = parse_line(str(raw))
        if tid is None:
            return
        k = _norm_key(tid)
        if not k or k in seen:
            return
        seen.add(k)
        out.append(tid.strip())

    primary = (telegram_block or {}).get("chat_id") or (telegram_block or {}).get("channel_id")
    push(primary)

    rec = (telegram_block or {}).get("recipients")
    if isinstance(rec, list):
        for item in rec:
            if isinstance(item, str):
                push(item)
            elif item is not None:
                push(str(item))

    cc = cc_telegram_chat_id_from_env()
    push(cc)

    return out
