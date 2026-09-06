# -*- coding: utf-8 -*-
"""对端撤回内容的「记得但不主动提」账本（#32② / #145⑤，2026-09-04）。

产品口径（老板拍板）：工作台同步删（显得专业）；AI **可以有记忆**，但不得主动
引用已删内容。#146 曾把关联情景记忆物理删掉、A 线历史整条剔除——上下文会因
凭空少一条而错位，且与「AI 可以有记忆」相反。本模块把那条改回来：

- 账本记下被撤原文（进程内；测试可 ``reset_ledger``）；
- A 线历史**保留槽位**，正文换成占位（轮次不塌）；
- 注入 hint：知道但不得主动提起；
- 出站 ``sanitize_outbound``：稿里主动提起撤回话术则按句剥离（当前入站已含
  同样字眼＝对方自己又提了，不剥）。

工作台移除走既有 ``deleted_by=peer`` + ``messages_deleted`` SSE，不改 store.py /
unified_inbox.html。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

PLACEHOLDER = "（对方已撤回一条消息，不得主动引用其内容）"
# M-1 C #219（2026-09-06）：**己方**在手机端删掉的消息——客户侧已不存在，AI 不得再接着
# 这个话头聊、不得把它当自己说过的事引用（「你刚才问她在不在宿务」→ 客户根本没看到）。
OWN_PLACEHOLDER = "（你已撤回一条消息，不得再提其内容或接着这个话头聊）"

_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SENT_RE = re.compile(r".+?(?:[。！？!?\n]|$)")
# 太泛的字不进命中面（「我」「去」单独出现不能当「提了日本」）
_STOP = set("的了吗呢啊哦呀吧嘛嘛我你他她它是在有和就都也与或去到着过上下不没")
# 英文同理（M-1 C #219 起账本里会有英文出站原文）：功能词 / 高频泛词不算「提到了那句话」，
# 且拉丁词命中要**同句 ≥2 个不同词**或整句包含才算——否则任何含 how/good/life 的回复都会被剥空。
_EN_STOP = {
    "the", "and", "for", "you", "your", "are", "was", "were", "have", "has", "had", "how",
    "what", "when", "where", "why", "who", "with", "this", "that", "there", "here", "then",
    "than", "too", "also", "just", "not", "but", "can", "could", "will", "would", "should",
    "did", "does", "doing", "been", "being", "get", "got", "let", "lets", "like", "much",
    "very", "really", "good", "great", "nice", "okay", "yes", "yeah", "hey", "hello", "hi",
    "day", "today", "tonight", "tomorrow", "now", "later", "time", "life", "thing", "things",
    "about", "into", "from", "over", "out", "some", "any", "all", "one", "two", "our", "its",
    "his", "her", "him", "she", "they", "them", "their", "still", "well", "back", "take",
    "make", "come", "know", "think", "feel", "want", "need", "going", "gonna", "wanna", "way",
    "there", "food", "same", "more", "most", "many", "long", "see", "say", "said", "tell",
    "sure", "maybe", "again", "ever", "never", "always", "off", "yet", "please", "thanks",
    "thank", "sorry", "love", "miss", "care", "night", "morning", "evening", "hope", "wish",
}

_LEDGER: Dict[str, List[str]] = {}
_OWN_LEDGER: Dict[str, List[str]] = {}


def reset_ledger() -> None:
    _LEDGER.clear()
    _OWN_LEDGER.clear()


def normalize_quote(text: Any) -> str:
    s = _WS_RE.sub(" ", str(text or "")).strip()
    return s[:200]


def record_withdrawn(conversation_id: str, quote: str, *, own: bool = False) -> bool:
    """记下一条被撤原文。过短 / 空 cid → False。幂等。``own=True``＝己方撤回（独立账本）。"""
    cid = str(conversation_id or "").strip()
    q = normalize_quote(quote)
    if not cid or len(q) < 3:
        return False
    ledger = _OWN_LEDGER if own else _LEDGER
    bucket = ledger.setdefault(cid, [])
    if any(normalize_quote(x) == q for x in bucket):
        return False
    bucket.append(q)
    if len(bucket) > 40:
        del bucket[:-40]
    return True


def _lookup(ledger: Dict[str, List[str]], key: str) -> List[str]:
    key = str(key or "").strip()
    if not key:
        return []
    if key in ledger:
        return list(ledger[key])
    out: List[str] = []
    seen = set()
    for cid, qs in ledger.items():
        if cid == key or cid.endswith(":" + key) or cid.rsplit(":", 1)[-1] == key:
            for q in qs:
                k = normalize_quote(q)
                if k and k not in seen:
                    seen.add(k)
                    out.append(q)
    return out


def quotes_for(key: str) -> List[str]:
    """按完整 conversation_id 或 chat_key 后缀取**对端**撤回原文。"""
    return _lookup(_LEDGER, key)


def own_quotes_for(key: str) -> List[str]:
    """按完整 conversation_id 或 chat_key 后缀取**己方**撤回原文（M-1 C #219）。"""
    return _lookup(_OWN_LEDGER, key)


def distinctive_terms(quote: str) -> List[str]:
    """从撤回原文抽可识别片段（整词优先，再 CJK 二字）。空/过短 → []。"""
    q = normalize_quote(quote)
    if len(q) < 3:
        return []
    terms: List[str] = []
    seen = set()

    def _add(s: str) -> None:
        s = str(s or "").strip()
        if len(s) < 2 or s in _STOP:
            return
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        terms.append(s)

    _add(q)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(chunk) >= 2:
            _add(chunk)
            for i in range(len(chunk) - 1):
                _add(chunk[i:i + 2])
    for w in re.findall(r"[A-Za-z]{3,}", q):
        if w.lower() in _EN_STOP:
            continue
        _add(w)
    terms.sort(key=len, reverse=True)
    return terms


def _inbound_covers(inbound: str, term: str) -> bool:
    inn = _WS_RE.sub("", str(inbound or "")).lower()
    t = _WS_RE.sub("", str(term or "")).lower()
    return bool(t) and t in inn


_LATIN_TERM_RE = re.compile(r"^[A-Za-z]+$")


def mentions_withdrawn(
    text: str, quotes: Iterable[str], *, inbound: str = "",
) -> List[str]:
    """正文是否主动提起撤回内容（入站已含该片段则不算主动）。

    命中口径：CJK 片段任一命中即算；拉丁词须**同一条撤回原文里 ≥2 个不同词**命中，
    或整句被包含（防「how / good / life」这类泛词让任何英文回复被剥空）。
    """
    hay = _WS_RE.sub("", str(text or "")).lower()
    if not hay:
        return []
    hits: List[str] = []
    seen = set()
    for q in quotes or []:
        q_hits: List[str] = []
        latin_n = 0
        for term in distinctive_terms(q):
            if len(term) < 2:
                continue
            if _inbound_covers(inbound, term):
                continue
            nt = _WS_RE.sub("", term).lower()
            if nt and nt in hay:
                q_hits.append(term)
                if _LATIN_TERM_RE.match(term):
                    latin_n += 1
        whole = _WS_RE.sub("", normalize_quote(q)).lower()
        whole_hit = bool(whole) and whole in hay
        cjk_hit = any(not _LATIN_TERM_RE.match(t) for t in q_hits)
        if not (whole_hit or cjk_hit or latin_n >= 2):
            continue
        for term in q_hits:
            nt = _WS_RE.sub("", term).lower()
            if nt not in seen:
                seen.add(nt)
                hits.append(term)
    return hits


def _split_sentences(text: str) -> List[str]:
    parts = [m.group(0) for m in _SENT_RE.finditer(text) if m.group(0)]
    return parts or ([text] if text else [])


def sanitize_outbound(
    text: str, quotes: Iterable[str], *, inbound: str = "",
) -> Tuple[str, List[str]]:
    """按句剥掉主动引用撤回内容的句子。剥空 → 中性短句，不回退原文。"""
    raw = str(text or "")
    if not raw:
        return raw, []
    qs = [normalize_quote(q) for q in (quotes or []) if normalize_quote(q)]
    hits = mentions_withdrawn(raw, qs, inbound=inbound)
    if not hits:
        return raw, []
    kept = []
    for sent in _split_sentences(raw):
        if mentions_withdrawn(sent, qs, inbound=inbound):
            continue
        kept.append(sent)
    cleaned = "".join(kept).strip()
    if not cleaned:
        cleaned = "嗯。" if _CJK_RE.search(raw) else "Yeah."
    return cleaned, hits


def apply_to_reply(
    text: str, conversation_id: str = "", *, inbound: str = "",
) -> Tuple[str, List[str]]:
    """发送链入口：按会话账本剥主动引用（对端撤回 + 己方撤回两本都算——己方删掉的话
    AI 也不该再原样复述）。无账本 / 异常由调用方自己兜。"""
    qs = quotes_for(conversation_id) + own_quotes_for(conversation_id)
    if not qs:
        return str(text or ""), []
    return sanitize_outbound(text, qs, inbound=inbound)


def redact_history(
    history: Optional[List[Dict[str, Any]]], quotes: Iterable[str],
    own_quotes: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """匹配的 user 条（对端撤回）/ assistant 条（己方撤回，M-1 C）换成占位，
    **不删槽位**（防上下文错位）。"""
    qs = [normalize_quote(q) for q in (quotes or []) if normalize_quote(q)]
    oqs = [normalize_quote(q) for q in (own_quotes or []) if normalize_quote(q)]
    if not history or not (qs or oqs):
        return list(history or [])

    def _hit(body: str, pool: List[str]) -> bool:
        return bool(body) and any(body == q or q in body or body in q for q in pool)

    out: List[Dict[str, Any]] = []
    for m in history:
        if not isinstance(m, dict):
            out.append(m)
            continue
        mm = dict(m)
        role = str(mm.get("role") or "")
        if role == "user" and qs and _hit(normalize_quote(mm.get("content")), qs):
            mm["content"] = PLACEHOLDER
            mm["_withdrawn"] = True
        elif role == "assistant" and oqs and _hit(normalize_quote(mm.get("content")), oqs):
            mm["content"] = OWN_PLACEHOLDER
            mm["_withdrawn_own"] = True
        out.append(mm)
    return out


def build_withdrawn_hint(quotes: Iterable[str], *, inbound: str = "",
                         own_quotes: Iterable[str] = ()) -> str:
    """注入：知道但不得主动提。对方本轮又说了同一件事 → 不注入。
    ``own_quotes``（M-1 C #219）＝己方撤回：不得再提、不得追问对方看没看到、不接着聊。"""
    qs = [normalize_quote(q) for q in (quotes or []) if len(normalize_quote(q)) >= 3]
    active = []
    for q in qs:
        terms = distinctive_terms(q)
        if terms and all(_inbound_covers(inbound, t) for t in terms[:3]):
            continue
        active.append(q)
    parts: List[str] = []
    if active:
        sample = "；".join(active[:3])
        parts.append(
            f"【已撤回·不得主动引用】对方删过这些话（{sample}）。"
            "你心里可以记得，但不要主动提起、不要追问、不要复述其中的具体内容。"
            "若对方这轮自己又说到同一件事，按新话回应即可。"
        )
    oqs = [normalize_quote(q) for q in (own_quotes or []) if len(normalize_quote(q)) >= 3]
    if oqs:
        sample = "；".join(oqs[:3])
        parts.append(
            f"【你已撤回·不得再提】你自己删掉了这些话（{sample}），对方那边已经看不到。"
            "不要再提起、不要问对方看到没有、不要接着这个话头往下聊；"
            "对方若主动说起，再自然接。"
        )
    return "\n".join(parts)


__all__ = [
    "OWN_PLACEHOLDER",
    "PLACEHOLDER",
    "build_withdrawn_hint",
    "distinctive_terms",
    "mentions_withdrawn",
    "normalize_quote",
    "own_quotes_for",
    "quotes_for",
    "record_withdrawn",
    "apply_to_reply",
    "redact_history",
    "reset_ledger",
    "sanitize_outbound",
]
