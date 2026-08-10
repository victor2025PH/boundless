"""主动开场「变体守卫」纯函数（P0，2026-07-29）。

实锤根因：gentle_checkin 固定指令 + 同一 LLM → 生产上 42 条主动问候里
「好久没联系啦，你最近过得怎么样呀？」级别的近重复句式 x3/x2/x2；且最近
上下文只拼纯文本（无方向/无时间），LLM 反把上一条未回的问候当风格参考，
越发越像复读机。本模块提供三件事，全部零 IO 可单测：

- **相似度判定**：归一化（去标点/空白/表情，仅留中英日韩文数字）后
  difflib 比率，抓「同一句问候换个语气词」的近重复；
- **未回连发尾**：从最近消息里抽出「末尾连续出站且没有得到回应」的文案，
  作为生成侧的「禁止相似」负样本；
- **结构化聊天上下文**：带方向（你/TA）+ 相对时间（3小时前/昨天）的
  上下文行，让 LLM 分得清谁说的、隔了多久。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# 仅保留字母数字 + CJK（含日文假名/韩文），其余（标点/空白/emoji/波浪号）全剥掉
_WORD_RE = re.compile(
    r"[^0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af]+")
# 中文语气/结构助词——问候句换个语气词就算「新文案」是生产实锤的伪多样性
# （「好久没联系啦，最近过得怎么样呀～」vs「嘿，好久没联系了，你最近还好吗？」
# 字符比率仅 0.57，剥掉助词后问候壳子裸露 → 0.76+ 抓得住；而真正换了切入点的
# 开场剥不剥都远低于阈值，零误伤）。
_TONE_RE = re.compile(r"[嘿嗨哈啊呀哇呢吧吗嘛哦噢喔嗯哟唷咯呗啦咧咦诶欸了的地得]")

# 阈值按生产实锤语料校准（2026-07-29）：42 条真实主动问候里的近重复对
# 归一化比率 0.667~1.0，真正换切入点的开场 ≤0.242——0.60 落在干净分隔带中，
# 抓全复读且离误伤区有 2.5 倍裕量。
DEFAULT_SIMILARITY_THRESHOLD = 0.60


def normalize_for_similarity(text: str) -> str:
    """相似度用归一化：小写 + 去标点/emoji + 剥中文语气助词（语气词不算差异）。"""
    return _TONE_RE.sub("", _WORD_RE.sub("", str(text or "").lower()))


def similarity(a: str, b: str) -> float:
    """两句开场的相似度 0..1（归一化后 difflib ratio；任一为空 → 0）。"""
    na, nb = normalize_for_similarity(a), normalize_for_similarity(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def most_similar(
    text: str,
    previous: Optional[List[str]],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> Optional[str]:
    """``text`` 与历史开场里相似度 ≥ threshold 的最像一条；都不像 → None。"""
    best: Optional[str] = None
    best_score = float(threshold)
    for p in previous or []:
        s = similarity(text, p)
        if s >= best_score:
            best, best_score = str(p), s
    return best


def trailing_unanswered_texts(
    messages: List[Dict[str, Any]], *, max_texts: int = 3,
) -> List[str]:
    """末尾「连续出站未获回应」的文案（时间升序输入，返回时间升序）。

    这些就是「我发了、TA 没回」的最强负样本——新开场绝不能与它们雷同。
    媒体占位（如 ``[图片] xx``）保留原文（配文同样不该被复读）。
    """
    tail: List[str] = []
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("direction") or "") != "out":
            break  # 一遇到入站 = 对方回过话，连发尾结束
        t = str(m.get("text") or "").strip()
        if t:
            tail.append(t)
    tail.reverse()
    return tail[-max(0, int(max_texts)):]


def trailing_unanswered_inbound(
    messages: List[Dict[str, Any]], *, max_texts: int = 2,
) -> List[str]:
    """末尾「连续入站未获回应」的文案（时间升序输入，返回时间升序）。

    这些是「TA 说了、我一直没回」的悬空话头（2026-08-05 实锤：客户 22:27
    撩了一句「以后给你介绍做你老公」整晚没人接，次日 07:10 只等来一条与
    话头完全脱节的通用晨安）——晨安/回访开场必须先接住它，否则「装没看见 +
    标准问候」当场穿帮。媒体占位（``[图片]``）保留原文（能被自然指涉）。
    """
    tail: List[str] = []
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("direction") or "") != "in":
            break  # 一遇到出站 = 我方回过话，悬空尾结束
        t = str(m.get("text") or "").strip()
        if t:
            tail.append(t)
    tail.reverse()
    return tail[-max(0, int(max_texts)):]


def rel_age_label(age_sec: float) -> str:
    """相对时间标签：刚刚 / N分钟前 / N小时前 / 昨天 / N天前。"""
    try:
        s = max(0.0, float(age_sec or 0.0))
    except (TypeError, ValueError):
        s = 0.0
    if s < 90:
        return "刚刚"
    if s < 3600:
        return f"{int(s // 60)}分钟前"
    if s < 86400:
        return f"{int(s // 3600)}小时前"
    if s < 2 * 86400:
        return "昨天"
    return f"{int(s // 86400)}天前"


def format_recent_context(
    messages: List[Dict[str, Any]],
    *,
    now: float,
    max_lines: int = 8,
    max_line_chars: int = 60,
    max_total_chars: int = 700,
) -> str:
    """把最近消息拼成带方向+相对时间的上下文块（时间升序，保最近）。

    形如：``TA（3小时前）：好呀`` / ``你（昨天）：刚下班～``。LLM 由此能
    分清谁说的、隔了多久——「上一条是我自己发的问候且没被回」不再被误读成
    对话素材。
    """
    lines: List[str] = []
    for m in (messages or [])[-max(1, int(max_lines)):]:
        if not isinstance(m, dict):
            continue
        t = str(m.get("text") or "").strip().replace("\n", " ")
        if not t:
            continue
        if len(t) > max_line_chars:
            t = t[: max_line_chars - 1] + "…"
        who = "你" if str(m.get("direction") or "") == "out" else "TA"
        try:
            age = rel_age_label(float(now) - float(m.get("ts") or 0.0))
        except (TypeError, ValueError):
            age = ""
        lines.append(f"{who}（{age}）：{t}" if age else f"{who}：{t}")
    out = "\n".join(lines)
    if len(out) > max_total_chars:
        out = out[-max_total_chars:]
        nl = out.find("\n")
        if 0 <= nl < 80:  # 掐掉被截断的半行
            out = out[nl + 1:]
    return out


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "normalize_for_similarity",
    "similarity",
    "most_similar",
    "trailing_unanswered_texts",
    "trailing_unanswered_inbound",
    "rel_age_label",
    "format_recent_context",
]
