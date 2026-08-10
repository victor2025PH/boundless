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
    """入站 Unicode emoji → 语义化（仅限**纯 emoji** 消息）。

    - 纯 emoji：``[表情] 笑哭了``——方括号形态，媒体块解析（inbound_enrich）与
      语言证据剥离（lang_policy 的媒体描述行剥离）都认识它。
    - 混合文本：**原样返回，绝不改写客户原话**。
      P0-198（2026-08-03 实锤，tg 7331682689）：旧行为在原文后追加
      ``（表情：中文语义）``，这段系统注入的中文被全链当成「客户在说中文」的
      语言证据——``Haha 🤣`` 落库成 ``Haha 🤣（表情：笑得满地打滚）`` → 草稿
      reply_lang 判成 zh → 中文稿原样发给英文客户 → 下一轮还按「切换提示」反咬
      客户 "suddenly switching to English?"。emoji 本身就留在正文里，模型读得懂；
      语义加注若将来要回归，走 prompt 组装期的结构化字段（media_desc 族），
      不许再写进存储层的消息正文。
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
        if stripped:
            return text  # 混合文本：客户原话不可改写（见 docstring P0-198）
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
        return f"[表情] {'、'.join(uniq)}"
    except Exception:
        return text


__all__ = ["demojize_one", "sticker_text_from_message", "annotate_inbound_emoji"]
