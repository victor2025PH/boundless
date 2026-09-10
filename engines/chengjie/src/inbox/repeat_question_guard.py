# -*- coding: utf-8 -*-
"""出站重复提问守卫（Q-1 C · #264 #270 · 2026-09-10）。

事故：同一客户十小时被问「what do you do for work」8 次（T9KN8X），K24YJ2 到「20th time」——
目标引擎的决议修了（A/B 段），但回复主链本身没有任何「我 24 小时内问过同一件事」的闸：
模型只要想问，草稿就出问句。本模块是**起草层最后一道**：抽出本稿问句，与 24h 内我方已发
问句做规范化相似比对（词干 + 同义小表 + 槽位标签），相似 ≥ :data:`SIM_THRESHOLD` → 该句删
（其余句子保留）；整稿只剩这一句 → 改写为一句非问句应答（模板池）。

挂点：``outbound_humanize.apply_draft_humanize`` 里 ``claim_guard.check_claims`` **之前**
（P-1 A 起草即净化的同一入口——工作台 / L1 审核稿 / 关怀预览看到的就是过了闸的文本）。
history 由本模块自己按 conversation_id 从 InboxStore 取（``direction=out`` + ``ts`` 才能判 24h），
调用方也可显式传 ``own_questions``（测试 / 无 store 场景）。

规范化：小写 → 去标点 → 去停用词 → 英文简单词干（claim_guard 同源）/ 中日文 2 字片段 →
同义小表折叠（job / work / occupation / career → ``work``；city / town / located / based →
``city``；age / old / born → ``age``…）→ 若整句命中某画像槽的问法关键词
（``profile_slots.SLOT_ASK_KEYWORDS``）加标签 ``slot:<key>``。相似度 = 两问句**同槽标签 → 1.0**
（同一件事换个说法仍是同一件事），否则 Jaccard(tokens)。

保守：判不准就不拦（只删「明确重复」）；任何异常原文放行；日志
``[repeat-q] conv=… matched_ts=… sim=0.xx action=strip|rewrite q=…``。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

WINDOW_SEC = 24 * 3600.0
SIM_THRESHOLD = 0.8
_MAX_OWN = 40

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")

# 同义小表（规范化后 token → 代表词）
_SYN: Dict[str, str] = {
    "job": "work", "jobs": "work", "occupation": "work", "career": "work", "profession": "work",
    "living": "work", "gig": "work", "工作": "work", "职业": "work", "生意": "work", "上班": "work",
    "city": "city", "town": "city", "located": "city", "based": "city", "live": "city",
    "where": "where", "whereabouts": "where", "城市": "city", "哪里": "where", "哪儿": "where",
    "哪个": "where", "在哪": "where", "坐标": "city", "住": "city",
    "old": "age", "age": "age", "born": "age", "birthday": "age", "年龄": "age", "几岁": "age",
    "多大": "age", "岁": "age",
    "hobby": "hobby", "hobbies": "hobby", "fun": "hobby", "enjoy": "hobby", "interest": "hobby",
    "爱好": "hobby", "喜欢": "hobby", "兴趣": "hobby",
    "single": "single", "married": "single", "boyfriend": "single", "girlfriend": "single",
    "partner": "single", "seeing": "single", "单身": "single", "对象": "single", "结婚": "single",
    "kid": "family", "kids": "family", "children": "family", "family": "family", "parents": "family",
    "家里": "family", "孩子": "family", "家人": "family", "父母": "family",
    "name": "name", "call": "name", "名字": "name", "称呼": "name",
    "u": "you", "ya": "you", "ur": "your", "wat": "what", "whats": "what", "r": "are",
}
_STOP_EN = frozenset(
    "a an the and or but so to of in on at for with by from as is are am was were be been being do "
    "does did have has had will would can could should may might must shall i me my you your he "
    "she it we they them his her its our their this that these those there here then than too very "
    "just really also still yet again ever btw hey oh lol haha yeah ok okay well anyway by the way "
    "curious wondering wonder tell about kind sort like".split())
_STOP_ZH = frozenset(list("的了吗呢啊吧呀哦嗯那这个些还也都就是在有和跟给对说提讲聊过到起来去想要会能可以没不很太挺好像应该已经"
                          "你我他她它们那么什么怎么一下一般平时现在好奇顺便问问是不是有没有"))


def norm_lang(lang: Any, text: str) -> str:
    s = str(lang or "").strip().lower().replace("_", "-")
    if s and s != "unknown":
        return "zh" if s.startswith("zh") else s.split("-", 1)[0]
    if _KANA_RE.search(text):
        return "ja"
    if _CJK_RE.search(text):
        return "zh"
    return "en"


def split_sentences(text: str, lang: str) -> List[str]:
    t = str(text or "").strip()
    if not t:
        return []
    if lang in ("zh", "ja"):
        parts = re.split(r"(?<=[。！？!?；;\n])\s*", t)
    else:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Za-z0-9\"'(\[¿¡])|\n+", t)
    return [p for p in parts if p and p.strip()]


def is_question(sentence: str) -> bool:
    s = str(sentence or "").strip()
    if not s:
        return False
    if "?" in s or "？" in s:
        return True
    tail = s.rstrip("。.!！~～ ")[-1:] if s.rstrip("。.!！~～ ") else ""
    if tail in ("吗", "呢", "么", "呀"):
        return True
    # 英文无问号但以疑问词起头（模型偶尔漏问号）
    return bool(re.match(r"^(what|where|which|who|how|when|why|do|does|did|are|is|were|was|"
                         r"have|has|can|could|would|will)\b", s.lower())) and len(s.split()) >= 3


def _stem_en(w: str) -> str:
    w = w.lower().rstrip("'")
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _slot_tags(sentence: str) -> List[str]:
    """句子命中哪些画像槽的问法关键词 → ``slot:<key>``（profile_slots 单一词表；导入失败 → []）。"""
    try:
        from src.companion.goals.profile_slots import SLOT_ASK_KEYWORDS, _contains_term
    except Exception:
        return []
    low = str(sentence or "").lower()
    out: List[str] = []
    for k, kws in SLOT_ASK_KEYWORDS.items():
        if any(_contains_term(low, w) for w in kws):
            out.append(f"slot:{k}")
    return out


def normalize_question(sentence: str, lang: str = "") -> Tuple[frozenset, Tuple[str, ...]]:
    """问句 → ``(token 集合, 槽位标签)``。纯函数。"""
    s = str(sentence or "").strip()
    lg = norm_lang(lang, s)
    tags = tuple(_slot_tags(s))
    toks: List[str] = []
    if lg in ("zh", "ja"):
        body = re.sub(r"[^\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]+", " ", s)
        for chunk in body.split():
            if re.fullmatch(r"[A-Za-z0-9]+", chunk):
                w = _stem_en(chunk)
                if w and w not in _STOP_EN:
                    toks.append(_SYN.get(w, w))
                continue
            # 先整词同义（「工作」「哪里」），再 2 字片段
            for key, rep in _SYN.items():
                if _CJK_RE.search(key) and key in chunk:
                    toks.append(rep)
            c = "".join(ch for ch in chunk if ch not in _STOP_ZH)
            if len(c) == 1:
                continue
            if len(c) == 2:
                toks.append(c)
            else:
                toks.extend(c[i:i + 2] for i in range(len(c) - 1))
    else:
        for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", s.lower()):
            w = w.strip("'-")
            w = _SYN.get(w, w)
            if not w or w in _STOP_EN:
                continue
            st = _stem_en(w)
            toks.append(_SYN.get(st, st))
    return frozenset(t for t in toks if t), tags


def similarity(a: str, b: str, lang: str = "") -> float:
    """两问句相似度 0..1：同槽标签 → 1.0；否则 token Jaccard。"""
    ta, ga = normalize_question(a, lang)
    tb, gb = normalize_question(b, lang)
    if ga and gb and set(ga) & set(gb):
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return round(inter / union, 3) if union else 0.0


# 整稿只剩重复问句时的非问句应答（按语言；seed 取模稳定）
_FILLERS: Dict[str, List[str]] = {
    "en": ["haha fair enough", "gotcha, makes sense", "lol okay okay", "right right"],
    "zh": ["哈哈好", "懂了懂了", "行，我记住了", "哈哈是的"],
    "ja": ["なるほどね", "了解〜", "そっか、わかった", "うんうん"],
}


def filler_line(lang: str, seed: str = "") -> str:
    pool = _FILLERS.get(lang) or _FILLERS["en"]
    if not seed:
        return pool[0]
    h = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def own_questions_for(store: Any, conversation_id: str, *, now: Optional[float] = None,
                      window_sec: float = WINDOW_SEC, limit: int = 60) -> List[Dict[str, Any]]:
    """24h 内我方已发问句 ``[{text, ts}]``（按句拆）。任何失败 → []。"""
    cid = str(conversation_id or "").strip()
    if store is None or not cid or not hasattr(store, "list_recent_messages"):
        return []
    n = float(now if now is not None else time.time())
    try:
        rows = store.list_recent_messages(cid, limit=max(1, min(int(limit or 60), 200))) or []
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or str(r.get("direction") or "") != "out":
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0 or ts < n - float(window_sec) or ts > n + 5:
            continue
        txt = str(r.get("text") or r.get("content") or "").strip()
        if not txt:
            continue
        lg = norm_lang("", txt)
        for sent in split_sentences(txt, lg):
            if is_question(sent):
                out.append({"text": sent.strip(), "ts": ts})
    return out[-_MAX_OWN:]


def check_repeat_questions(
    text: str, *, conversation_id: str = "", lang: str = "",
    own_questions: Optional[Iterable[Any]] = None, store: Any = None,
    now: Optional[float] = None, threshold: float = SIM_THRESHOLD,
) -> Tuple[str, Dict[str, Any]]:
    """本稿问句 × 24h 内我方问句 → 相似 ≥threshold 的句子删；整稿只剩它 → 非问句应答。

    返回 ``(text, report)``；``report``: ``action ∈ clean|strip|rewrite``、``matched`` 列表
    ``[{q, matched, matched_ts, sim}]``、``stripped`` 句数。``own_questions`` 缺省时按
    ``conversation_id`` 从 ``store``（或 protocol_bridge 的 InboxStore）取。绝不抛。"""
    rep: Dict[str, Any] = {"action": "clean", "matched": [], "stripped": 0}
    src = str(text or "")
    if not src.strip():
        return src, rep
    try:
        lg = norm_lang(lang, src)
        own: List[Dict[str, Any]] = []
        if own_questions is not None:
            for q in own_questions:
                if isinstance(q, dict):
                    t = str(q.get("text") or q.get("q") or "").strip()
                    ts = float(q.get("ts") or 0)
                else:
                    t, ts = str(q or "").strip(), 0.0
                if t:
                    own.append({"text": t, "ts": ts})
        else:
            st = store
            if st is None:
                try:
                    from src.integrations.protocol_bridge import get_inbox_store
                    st = get_inbox_store()
                except Exception:
                    st = None
            own = own_questions_for(st, conversation_id, now=now)
        if not own:
            return src, rep
        sents = split_sentences(src, lg)
        if not sents:
            return src, rep
        keep: List[str] = []
        for s in sents:
            if not is_question(s):
                keep.append(s)
                continue
            best: Optional[Dict[str, Any]] = None
            for q in own:
                sim = similarity(s, q["text"], lg)
                if sim >= float(threshold) and (best is None or sim > best["sim"]):
                    best = {"q": s.strip()[:80], "matched": q["text"][:80],
                            "matched_ts": float(q.get("ts") or 0), "sim": sim}
            if best is None:
                keep.append(s)
            else:
                rep["matched"].append(best)
        if not rep["matched"]:
            return src, rep
        rep["stripped"] = len(rep["matched"])
        sep = "" if lg in ("zh", "ja") else " "
        out = sep.join(p.strip() for p in keep if p.strip()).strip()
        if out:
            rep["action"] = "strip"
        else:
            rep["action"] = "rewrite"
            out = filler_line(lg, conversation_id)
        for m in rep["matched"]:
            logger.info("[repeat-q] conv=%s matched_ts=%s sim=%.2f action=%s q=%r matched=%r",
                        conversation_id or "-",
                        time.strftime("%H:%M", time.localtime(m["matched_ts"])) if m["matched_ts"] else "-",
                        m["sim"], rep["action"], m["q"][:60], m["matched"][:60])
        return out, rep
    except Exception:
        logger.debug("[repeat-q] 异常（原文放行）", exc_info=True)
        return src, {"action": "clean", "matched": [], "stripped": 0}


__all__ = [
    "WINDOW_SEC", "SIM_THRESHOLD", "norm_lang", "split_sentences", "is_question",
    "normalize_question", "similarity", "filler_line", "own_questions_for",
    "check_repeat_questions",
]
