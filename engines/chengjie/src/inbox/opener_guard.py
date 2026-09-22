# -*- coding: utf-8 -*-
"""出站开场词守卫（P0-4，2026-09-21，Bug #340「连着几条都是 Ha, 开头」）。

``reply_variety`` 在 prompt 侧劝模型别复读开头，但那是**软约束**——模型不听就穿帮，
而且 ``ai.reply_variety`` 默认关。本模块是出站侧的**硬兜底**，纯函数、零 IO：

  - ``inspect``：本稿开场词（首个感叹/笑词 token，或首词 / 前 4 个汉字）在本会话
    最近 ``window`` 条出站里已出现 ``min_repeat`` 次以上 → 命中；
  - ``strip_opener``：命中且开场是「Ha, / Haha! / 哈哈，/ Hey～」这类可直接摘掉的
    感叹 token → 摘掉后首字母大写返回；不是感叹 token（如每条都「I think」开头）→
    不动文本，只让调用方计数留痕（此处不冒险重写译文）。

跑在**译后文本**上：客户看到的是译文，历史出站存的也是译文，同一口径比对。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_WINDOW = 6
DEFAULT_MIN_REPEAT = 2
DEFAULT_ALERT_AFTER = 10

_INTERJ_RE = re.compile(
    r"^\s*(?P<tok>(?:h+a+(?:h+a+)*|he+h+e+|he+y+|hi+|hello+|oh+|ooh+|aw+|hm+|well|wow+|yay|lol|omg"
    r"|ah+|ugh|oof|honestly|okay|ok|hehe|haha)"
    r"|[哈嘿呵嘻嗯哎啊欸哇呀唉哦噢喔嘤]+)"
    r"(?P<sep>[\s,，.。!！~～:：…—\-]+)",
    re.I)
_LATIN_WORD_RE = re.compile(r"^[a-z][a-z']*(?:\s+[a-z][a-z']*)?", re.I)
_LEAD_STRIP_RE = re.compile(r"^[\W_]+", re.UNICODE)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\uFE0F\u200d]+")


def _norm_tok(tok: str) -> str:
    t = str(tok or "").lower()
    if re.fullmatch(r"(?:h+a+)+|haha|hehe|he+h+e+|lol", t):
        return "ha"
    if t and all(ch in "哈呵嘻嘿" for ch in t):
        return "哈"
    return re.sub(r"(.)\1+", r"\1", t)


def opener_key(text: str) -> str:
    """开场词归一键：感叹 token（笑词家族折成 ha/哈）优先；否则首词 / 前 4 个汉字。空＝无。"""
    s = _EMOJI_RE.sub("", str(text or "")).strip()
    if not s:
        return ""
    m = _INTERJ_RE.match(s)
    if m:
        return _norm_tok(m.group("tok"))
    s = _LEAD_STRIP_RE.sub("", s)
    if len(s) < 2:
        return ""
    w = _LATIN_WORD_RE.match(s)
    if w:
        parts = w.group(0).lower().split()
        return parts[0] if len(parts[0]) >= 2 else " ".join(parts)
    return s[:4]


def strip_opener(text: str) -> str:
    """摘掉开头的感叹 token（含其后的标点/空白）；不是感叹开场或摘后过短 → 返回空串（不可摘）。"""
    s = str(text or "")
    m = _INTERJ_RE.match(_EMOJI_RE.sub("", s).lstrip())
    if not m:
        return ""
    rest = _EMOJI_RE.sub("", s).lstrip()[m.end():].lstrip()
    if len(rest) < 2:
        return ""
    if rest[0].isascii() and rest[0].isalpha():
        rest = rest[0].upper() + rest[1:]
    return rest


def recent_out_texts(rows: Optional[Iterable[Dict[str, Any]]], *, window: int = DEFAULT_WINDOW) -> List[str]:
    """最近 ``window`` 条真正发出去的出站文本（新→旧）。失败 / 重发状态的行不算。"""
    out: List[Dict[str, Any]] = []
    for r in list(rows or []):
        if not isinstance(r, dict) or str(r.get("direction") or "") != "out":
            continue
        if str(r.get("status") or "") in ("failed", "resent"):
            continue
        if not str(r.get("text") or "").strip():
            continue
        out.append(r)

    def _ts(r: Dict[str, Any]) -> float:
        try:
            return float(r.get("ts") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    out.sort(key=_ts, reverse=True)
    return [str(r.get("text") or "") for r in out[:max(1, int(window or DEFAULT_WINDOW))]]


def inspect(text: str, recent_out: Optional[Iterable[str]], *,
            min_repeat: int = DEFAULT_MIN_REPEAT) -> Optional[Dict[str, Any]]:
    """本稿开场词是否在最近出站里复读。返回 ``None`` 或 ``{opener, count, stripped}``
    （``stripped`` 为摘掉开场后的文本，不可摘时为空串）。绝不抛。"""
    try:
        key = opener_key(text)
        if not key:
            return None
        count = sum(1 for t in (recent_out or []) if opener_key(t) == key)
        if count < max(1, int(min_repeat or DEFAULT_MIN_REPEAT)):
            return None
        return {"opener": key, "count": count, "stripped": strip_opener(text)}
    except Exception:
        return None


def resolve_cfg(root_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``inbox.opener_guard``（默认开：只摘可摘的感叹开场，零副作用；``enabled:false`` 关）。"""
    try:
        blk = dict(((root_config or {}).get("inbox") or {}).get("opener_guard") or {})
    except Exception:
        blk = {}
    return {
        "enabled": bool(blk.get("enabled", True)),
        "window": max(1, min(20, int(blk.get("window", DEFAULT_WINDOW) or DEFAULT_WINDOW))),
        "min_repeat": max(1, min(20, int(blk.get("min_repeat", DEFAULT_MIN_REPEAT) or DEFAULT_MIN_REPEAT))),
        # 同一开场键累计命中达此数 → ops_alert 一次（≤0 关）；见 AutosendWorker._opener_monitor
        "alert_after": max(0, min(10000, int(blk.get("alert_after", DEFAULT_ALERT_AFTER)))),
    }


__all__ = ["DEFAULT_WINDOW", "DEFAULT_MIN_REPEAT", "DEFAULT_ALERT_AFTER", "opener_key", "strip_opener",
           "recent_out_texts", "inspect", "resolve_cfg"]
