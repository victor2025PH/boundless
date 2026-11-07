"""WhatsApp 真机会话身份：只认号码。

会话 id 一律 ``whatsapp:{账号}:{E.164}``。显示名只是标签。
解析不出号码时返回空串，调用方不得用名字造一把可发送的键。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_DIGITS = re.compile(r"\d+")
_PHONE_IN_TEXT = re.compile(r"(?<!\d)(?:\+|00)?([1-9]\d{7,14})(?!\d)")


def normalize_e164(raw: str) -> str:
    """收成 ``+`` 加 8 到 15 位数字。不像号码就返回空串。

    去掉空格、横线、括号。``00`` 国际前缀换成 ``+``。
    纯显示名、过短的数字、以 0 开头的本地号（没有国家码）都不收。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return ""
    if text.startswith("00"):
        text = "+" + text[2:]
    digits = "".join(_DIGITS.findall(text))
    if text.startswith("+"):
        pass
    elif len(digits) < 8 or len(digits) > 15:
        return ""
    if not digits or len(digits) < 8 or len(digits) > 15:
        return ""
    if digits[0] == "0":
        return ""
    return "+" + digits


def conversation_id(account_id: str, phone: str) -> str:
    """电脑收件箱和 decide 共用的会话 id。号码必须已是 E.164。"""
    account = str(account_id or "").strip() or "default"
    e164 = normalize_e164(phone)
    if not e164:
        return ""
    return f"whatsapp:{account}:{e164}"


def phone_from_conversation_id(conversation_id: str) -> str:
    parts = str(conversation_id or "").split(":")
    if len(parts) < 3 or parts[0] != "whatsapp":
        return ""
    return normalize_e164(parts[-1])


def extract_phone(*chunks: str) -> str:
    """从通知标题、顶栏、备注里抽出第一个像 E.164 的号码。"""
    for chunk in chunks:
        text = str(chunk or "").strip()
        if re.fullmatch(r"\+?\d[\d\s().-]{6,}\d", text):
            found = normalize_e164(text)
            if found:
                return found
        for match in _PHONE_IN_TEXT.finditer(text):
            found = normalize_e164("+" + match.group(1))
            if found:
                return found
    return ""


def wa_me_url(phone: str, text: Optional[str] = None) -> str:
    """wa.me 深链。有文本时放进 ``text`` 参数，由 WhatsApp 预填输入框。

    号码用去掉加号的数字。文本做百分号编码，不经过 adb 的逐字输入。
    """
    from urllib.parse import quote

    e164 = normalize_e164(phone)
    if not e164:
        return ""
    digits = e164[1:]
    if text is None or str(text) == "":
        return f"https://wa.me/{digits}"
    return f"https://wa.me/{digits}?text={quote(str(text), safe='')}"


def view_intent_args(url: str) -> list:
    """打开深链的 adb 参数。测试只断言这条参数，不执行 adb。"""
    return [
        "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url,
    ]


def same_name(a: str, b: str) -> bool:
    left = re.sub(r"\s+", " ", str(a or "")).strip().casefold()
    right = re.sub(r"\s+", " ", str(b or "")).strip().casefold()
    return bool(left) and left == right


def header_matches(header: str, *, phone: str = "", sender: str = "") -> bool:
    """顶栏必须对上号码或显示名全称。对不上就不能入库、不能发。

    顶栏号码被省略号截断时，可见数字只要是完整号码的前缀或后缀也算对上。
    没有省略号时，残段至少 6 位，避免一位数字误认。中间一段不算。
    """
    shown = str(header or "").strip()
    if not shown:
        return False
    if phone and _truncated_phone_matches(shown, phone):
        return True
    if sender and same_name(shown, sender):
        return True
    return False


def _truncated_phone_matches(shown: str, phone: str) -> bool:
    want = normalize_e164(phone)
    if not want:
        return False
    digits = "".join(_DIGITS.findall(shown))
    full = want[1:]
    if not digits or not full:
        return False
    if digits == full:
        return True
    if len(digits) >= len(full):
        return False
    truncated = any(mark in shown for mark in ("…", "...", "..", "⋯"))
    minimum = 4 if truncated else 6
    if len(digits) < minimum:
        return False
    return full.startswith(digits) or full.endswith(digits)


def lookup_phone_by_name(store, account_id: str, name: str) -> str:
    """在本账号已经记下号码的会话里，按显示名找唯一号码。

    重名或找不到都返回空串，宁可不发，也不猜第一个。
    """
    label = str(name or "").strip()
    if store is None or not label or not hasattr(store, "list_conversations"):
        return ""
    try:
        rows = store.list_conversations(
            platform="whatsapp", account_id=str(account_id or ""), limit=500,
        )
    except Exception:
        return ""
    hits = []
    for row in rows or []:
        if not same_name(str(row.get("display_name") or ""), label):
            continue
        phone = normalize_e164(str(row.get("phone") or "")) or normalize_e164(
            str(row.get("chat_key") or "")
        )
        if phone:
            hits.append(phone)
    unique = set(hits)
    if len(unique) != 1:
        return ""
    return next(iter(unique))
