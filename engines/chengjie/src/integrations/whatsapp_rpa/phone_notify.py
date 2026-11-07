"""从 dumpsys notification 文本里抽出 WhatsApp 的发送者和正文。

只解析字符串，不调用 adb。点进哪个人由号码和顶栏核对决定，
这里不负责点击聊天列表第一行。
"""

from __future__ import annotations

import re
from typing import Dict, List

_BLOCK_RE = re.compile(
    r"NotificationRecord[^\n]*pkg=com\.whatsapp[^\n]*\n"
    r"((?:(?!\s*NotificationRecord).+\n?)*)",
    re.MULTILINE,
)
_TITLE_RE = re.compile(
    r"android\.title\s*=\s*(?:"
    r"String\s*\(([^)\n]+)\)"
    r"|String\s*\(\d+\)\s*[\"']?([^\"'\n]+)[\"']?"
    r")",
    re.IGNORECASE,
)
_TEXTLINES_RE = re.compile(
    r"android\.textLines\s*=\s*CharSequence\[\][^\n]*\n"
    r"((?:\s+\[\d+\][^\n]+\n?)+)",
    re.MULTILINE,
)
_LINEITEM_RE = re.compile(r"\[\d+\]\s*(.+)")
_TEXT_RE = re.compile(
    r"android\.text\s*=\s*(?:"
    r"String\s*\(([^)\n]+)\)"
    r"|String\s*\(\d+\)\s*[\"']?([^\"'\n]+)[\"']?"
    r")",
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(r"^\d+\s*条\s*新\s*消息")
_SKIP_SENDERS = {"WhatsApp", "WhatsApp Business", ""}


def parse_wa_notifications(dump: str) -> List[Dict[str, str]]:
    text = str(dump or "")
    if not text:
        return []
    found: List[Dict[str, str]] = []
    seen = set()
    for block_match in _BLOCK_RE.finditer(text):
        block = block_match.group(1)
        title_match = _TITLE_RE.search(block)
        sender = ""
        if title_match:
            sender = (title_match.group(1) or title_match.group(2) or "").strip().rstrip("\"' ")
        if sender in _SKIP_SENDERS:
            continue
        body = ""
        lines_match = _TEXTLINES_RE.search(block)
        if lines_match:
            items = _LINEITEM_RE.findall(lines_match.group(1))
            if items:
                body = items[-1].strip()
        if not body:
            text_match = _TEXT_RE.search(block)
            if text_match:
                body = (text_match.group(1) or text_match.group(2) or "").strip().rstrip("\"' ")
        if not body or _SUMMARY_RE.match(body):
            continue
        key = (sender, body)
        if key in seen:
            continue
        seen.add(key)
        found.append({"sender": sender, "text": body})
    return found
