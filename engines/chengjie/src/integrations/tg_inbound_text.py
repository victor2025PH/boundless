"""Telegram 入站文本语义化（A/B 线共用）。

B 线（protocol worker ``tg_message_payload``）与 A 线（``telegram_client``）
共用贴纸 emoji demojize 与纯文本 emoji 附注，使阶段 3 ``inbound_enrich`` 解析器
能稳定识别 ``[表情] …`` 并注入 AI 媒体块。
"""
from __future__ import annotations

from typing import Any


def demojize_one(raw_emoji: str) -> str:
    """单个 emoji → 中文语义（失败则原样）。"""
    raw = str(raw_emoji or "").strip()
    if not raw:
        return ""
    try:
        import emoji as _emoji
        demoj = _emoji.demojize(raw, language="zh", delimiters=("", ""))
        return demoj.strip() if demoj and demoj != raw else raw
    except Exception:
        return raw


def sticker_text_from_message(message: Any) -> str:
    """pyrogram Message 贴纸 → ``[表情] 语义``（零 Vision，与 A 线 emoji 路径同口径）。"""
    sticker = getattr(message, "sticker", None)
    if sticker is None:
        return "[表情]"
    emo_hint = demojize_one(str(getattr(sticker, "emoji", "") or ""))
    if emo_hint:
        return f"[表情] {emo_hint}"
    return "[表情]"


def annotate_inbound_emoji(text: str) -> str:
    """入站 Unicode emoji → 追加中文语义，帮 AI 读懂情绪。

    - 纯 emoji：``[表情] 笑哭了``
    - 混合文本：原文后 ``（表情：…）``
    - 无 emoji：原样
    """
    if not text:
        return text
    try:
        import emoji as _emoji
    except Exception:
        return text
    try:
        found = [e["emoji"] for e in _emoji.emoji_list(text)]
        if not found:
            return text
        stripped = _emoji.replace_emoji(text, replace="").strip()
        names = []
        for ch in found:
            nm = demojize_one(ch)
            if nm and nm != ch:
                names.append(nm)
            if len(names) >= 5:
                break
        if not names:
            return text
        uniq = list(dict.fromkeys(names))
        if not stripped:
            return f"[表情] {'、'.join(uniq)}"
        return f"{text}（表情：{'、'.join(uniq)}）"
    except Exception:
        return text


__all__ = ["demojize_one", "sticker_text_from_message", "annotate_inbound_emoji"]
