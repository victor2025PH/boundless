# -*- coding: utf-8 -*-
"""Lightweight language detection for Messenger auto-reply.

启发式实现，零依赖。只区分 bot 实际服务的 4 类受众:
``ja`` / ``zh`` / ``en`` / ``it``。不确定时返回空串,让 caller 回退到
``persona_key`` 所声明的目标语言 —— 宁可保守也不误判,因为误判直接
会让 LLM 用错语种回复,伤用户体验远大于多一次回退。

典型用法::

    from src.ai.lang_detect import detect_language
    lang = detect_language(incoming_text)   # 'ja' / 'zh' / 'en' / 'it' / ''
"""
from __future__ import annotations

import re


_HIRAGANA = re.compile(r"[぀-ゟ]")
_KATAKANA = re.compile(r"[゠-ヿㇰ-ㇿ]")
_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")

_IT_DIACRITICS = re.compile(r"[àèéìòù]")
_IT_MARKERS = re.compile(
    r"(?<![a-zàèéìòù])(?:ciao|grazie|prego|mille|molto|"
    r"perch[eé]|citt[aà]|c'è|bene|benissimo|"
    r"sono|siamo|voglio|vorrei|buongiorno|buonasera|"
    r"amica|amore|bellissim[ao]|però|quindi|davvero|"
    r"parli|parlare|italiano|italiana)(?![a-zàèéìòù])",
    re.IGNORECASE,
)


def detect_language(text: str) -> str:
    """返回 incoming 文本的主导语言 ISO 码。

    优先级:kana > CJK > 拉丁 (带意大利语标志则 it,否则 en)。
    极短/纯符号/纯 emoji 返回空串。

    Returns:
        ``'ja'`` / ``'zh'`` / ``'en'`` / ``'it'`` / ``''``
    """
    if not text or not isinstance(text, str):
        return ""
    s = text.strip()
    if len(s) < 2:
        return ""
    if _HIRAGANA.search(s) or _KATAKANA.search(s):
        return "ja"
    if _CJK.search(s):
        return "zh"
    if _LATIN.search(s):
        if _IT_DIACRITICS.search(s) or _IT_MARKERS.search(s):
            return "it"
        return "en"
    return ""
