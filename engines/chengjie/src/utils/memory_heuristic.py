"""
Rule-based memory snippets from a single user message (no LLM).
"""

from __future__ import annotations

import re
from typing import List


# 自称/称呼误提取护栏：「我是X」「叫我X」里 X 若以这些动词/虚词起头，
# 多半不是名字而是句子片段（"我是说真的""叫我别走""我是来问问题的"）→ 不归为称呼。
# 保守：宁可漏（少记一个名字），不可误（把句子片段当名字写进长期记忆污染人设）。
_SELF_NAME_BLOCK = re.compile(
    r"^(说|想|要|来|去|在|会|不|没|刚|正|真|个|那|这|一|觉得|认为|知道|准备|"
    r"打算|可以|应该|从|把|被|跟|对|为|问|找|做|怕|先|别|等|走|过|看|听|"
    r"回|滚|闭|因为|如果|虽然|但|而|就|才|还|也|都|很|挺)"
)


def _looks_like_name(v: str) -> bool:
    """称呼候选是否像真名字/昵称（非句子片段）。"""
    v = (v or "").strip()
    if not v:
        return False
    return not _SELF_NAME_BLOCK.match(v)


def extract_heuristic_facts(text: str) -> List[str]:
    """Return 0+ short memory strings worth storing."""
    if not text or not isinstance(text, str):
        return []
    t = text.strip()
    if len(t) < 2:
        return []
    out: List[str] = []

    # Chinese: 叫我 / 称呼
    m = re.search(r"叫我\s*([^\s，。！？\n]{1,16})", t)
    if m and _looks_like_name(m.group(1).strip()):
        out.append(f"用户希望我称呼 TA：{m.group(1).strip()}")

    m = re.search(r"我是\s*([^\s，。！？\n]{1,20})", t)
    if m and _looks_like_name(m.group(1).strip()):
        out.append(f"用户自称：{m.group(1).strip()}")

    m = re.search(r"我不喜欢\s*([^。！？\n]{1,40})", t)
    if m:
        out.append(f"用户表示不喜欢：{m.group(1).strip()[:40]}")

    # 年龄（B50 2026-08-23：`_299` 实录「三次自报 38 岁仍被问年龄段」——年龄
    # 此前根本不被捕获，画像里没有 → LLM 只能问）。保守：只认第一人称现在时
    # 自报（我38岁/我今年38），排除追忆句（38岁的时候/那年）与第三人称。
    m = re.search(
        r"我(?:今年|现在|都)?\s*(\d{1,2})\s*岁(?!的时候|那年|之前|以前)", t)
    if not m:
        # 「我今年38」省略「岁」的常见口语——单位负预查排除 38万/38号 等非年龄
        m = re.search(
            r"我今年\s*(\d{1,2})(?!\d)"
            r"(?!\s*(?:年|月|号|日|点|块|元|斤|万|亿|个|天|小时|分|秒|[kKwW]))", t)
    if not m:
        m = re.search(r"(?i)\bI(?:'m|\s+am)\s+(\d{1,2})(?:\s+years?\s+old)?\b", t)
    if m:
        try:
            _age = int(m.group(1))
            if 10 <= _age <= 99:
                out.append(f"用户年龄：{_age}岁")
        except (TypeError, ValueError):
            pass

    m = re.search(r"记住[：:]\s*([^。！？\n]{2,80})", t)
    if m:
        out.append(f"用户请我记得：{m.group(1).strip()[:80]}")

    # English
    m = re.search(r"(?i)call me\s+([A-Za-z][A-Za-z\s'.-]{0,24})", t)
    if m:
        out.append(f"User asked to be called: {m.group(1).strip()}")

    m = re.search(r"(?i)my name is\s+([A-Za-z][A-Za-z\s'.-]{0,24})", t)
    if m:
        out.append(f"User's name (EN): {m.group(1).strip()}")

    # Dedupe while preserving order
    seen = set()
    uniq: List[str] = []
    for x in out:
        k = x[:120]
        if k not in seen:
            seen.add(k)
            uniq.append(x[:500])
    return uniq


def matches_forget_intent(text: str, phrases: List[str]) -> bool:
    """True if user is asking to clear bot-side memory."""
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    for p in phrases:
        p = (p or "").strip()
        if not p:
            continue
        if p.lower() in low or raw.startswith(p):
            return True
    # Regex fallbacks (Chinese / EN)
    if re.match(
        r"^(忘掉|忘记|清除|删除|清空)(你)?(记住|的)?(的)?(东西|内容|话|记忆)?",
        raw,
    ):
        return True
    if re.match(
        r"(?i)^(forget|clear)\s+(what|everything|my)",
        low,
    ):
        return True
    return False
