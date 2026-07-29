"""主动触达 opt-out 意图识别（P1，2026-07-29，纯函数）。

用户明说「别再发了/别烦我/stop messaging me」时，系统此前听不懂，只能靠对方
拉黑（伤账号）或忍耐。本模块做**保守**的退订意图识别——词表只收「不想再被
主动打扰」的高置信表述，宁可漏判（漏了还有未回退避兜底降频）也不误判
（「别烦」出现在转述/开玩笑里误静默 30 天=丢客户）。

命中处置在编排器：写静默注册表（默认 30 天不再主动），**只拦主动触达**，
对方自己再来消息照常回复；对方开口即提前解除（自愿回来了）。
"""

from __future__ import annotations

import re
from typing import List, Optional

# 高置信退订表述（子串命中；中文不分词，直接子串最稳）。
# 刻意不收：单独「滚」「闭嘴」「烦死了」（情绪宣泄/打情骂俏高频误伤），
# 「不要发」（可能指别发某类内容）。
_PHRASES_SUBSTR = (
    # zh：明确针对「再发消息」的拒绝
    "别再发了", "别再发消息", "不要再发了", "不要再发消息", "别发了别发了",
    "别再联系我", "不要再联系我", "别联系我了", "别来烦我", "别再烦我",
    "不要来烦我", "别打扰我", "不要打扰我", "别再打扰", "别给我发消息",
    "不要给我发消息", "再发我就拉黑", "再发就拉黑", "我要拉黑你", "取关了",
    "退订", "别骚扰我", "不要骚扰", "别再骚扰",
    # ja / ko（保守几条明确表述）
    "もう送らないで", "連絡しないで", "メッセージしないで",
    "그만 보내", "연락하지 마", "메시지 보내지 마",
)

# en：词边界正则（子串会误伤 "unstoppable" 之类）
_PATTERNS_EN = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bstop\s+(messaging|texting|contacting)\s*(me)?\b",
    r"\bdon'?t\s+(message|text|contact)\s+me\b",
    r"\bdo\s+not\s+(message|text|contact)\s+me\b",
    r"\bstop\s+sending\s+(me\s+)?messages\b",
    r"\bleave\s+me\s+alone\b",
    r"\bunsubscribe\b",
    r"\bstop\s+bothering\s+me\b",
))


def detect_optout(texts: List[str]) -> str:
    """在（通常是最近几条入站）文本里找退订表述；返回命中的原句片段，无 → ""。

    输入按时间升序或降序皆可（逐条独立判定）；None/空条目安全跳过。
    """
    for t in texts or []:
        s = str(t or "").strip()
        if not s:
            continue
        low = s.lower()
        for p in _PHRASES_SUBSTR:
            if p in s or p in low:
                return s[:80]
        for rx in _PATTERNS_EN:
            if rx.search(s):
                return s[:80]
    return ""


def optout_active(
    entry: Optional[dict], *, now: float, last_in_ts: float = 0.0,
) -> bool:
    """静默条目当前是否仍生效（纯函数）。

    - 无条目 / 已过期（now ≥ until）→ False；
    - 对方在 opt-out **之后又主动开口**（last_in_ts > 记录时刻）→ False
      （自愿回来了，静默自动解除——比死等 30 天更像真人）。
    """
    if not isinstance(entry, dict):
        return False
    try:
        ts = float(entry.get("ts") or 0.0)
        until = float(entry.get("until") or 0.0)
    except (TypeError, ValueError):
        return False
    if until <= 0 or float(now) >= until:
        return False
    try:
        li = float(last_in_ts or 0.0)
    except (TypeError, ValueError):
        li = 0.0
    if li > ts:
        return False
    return True


__all__ = ["detect_optout", "optout_active"]
