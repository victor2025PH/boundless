# -*- coding: utf-8 -*-
"""引用锚点守卫（P-1 B · #259 #254，2026-09-08）。

证据 ZH3ZQ5③：机器当天 clean 重装、记忆 0 条、人设 0 个，开场白却写
「I was just thinking about that hiking trail you mentioned a while back」——「你之前提过」
没有任何依据，与 RRYKF6「被工作埋了」同类编造。O-1 E 管的是「编造迟回理由」，本模块管
「编造共同回忆」：出稿里凡是**引用对方说过的话**（you mentioned / you said / last time you /
你之前说 / 上次你说 / 前に言ってた…），就把被引用的实体 / 关键词拿到「最近会话文本 + 该客户
记忆」里找锚点；找不到 → 整句换成中性开场（how have you been? / 最近怎么样？）；找到 → 放行。

结构照 O-1 C ``persona_guard.rewrite_service_tone``：纯函数、确定性、改写一次、绝不抛、
绝不返回空。挂点在起草层净化（``outbound_humanize.apply_draft_humanize``）**之前**——先去谎
再去标点。verbatim / 人工手发不经本模块。

误伤保护：
  * 引用短语出现在**引号里**（出稿转述客户原话「你说的 "you mentioned"…」）不动；
  * 出稿整句与历史里某条**逐字相同**（复述客户原话）不动；
  * 抽不出实体的裸引用（"like you said"）只在**零历史**时才改——有历史时对方总说过点什么。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── 引用短语三语表 ──────────────────────────────────────────────────────────
_CLAIM_EN = re.compile(
    r"(?:"
    r"\b(?:as|like|just\s+like)\s+you\s+(?:said|mentioned|told\s+me|put\s+it)\b|"
    r"\byou\s+(?:once\s+|already\s+|previously\s+|earlier\s+)?(?:mentioned|said|told\s+me|"
    r"talked\s+about|brought\s+up|were\s+(?:saying|telling\s+me)|kept\s+(?:saying|telling\s+me)|"
    r"used\s+to\s+(?:say|tell\s+me))\b|"
    r"\byou'?ve\s+(?:mentioned|said|told\s+me|talked\s+about|brought\s+up)\b|"
    r"\byou\s+(?:had\s+)?(?:mentioned|said|told\s+me)\s+(?:before|earlier|once|the\s+other\s+day)\b|"
    r"\b(?:the\s+)?last\s+time\s+(?:you|we)\b|"
    r"\byou\b[^.!?\n]{0,40}?\ba\s+while\s+(?:back|ago)\b|"
    r"\bthe\s+other\s+day\s+you\b|"
    r"\bremember\s+(?:when|how|that|the|what|you)\b|"
    r"\bdo\s+you\s+(?:still\s+)?remember\b|"
    r"\byou\s+always\s+(?:say|said|talk\s+about)\b|"
    r"\bback\s+when\s+you\b|"
    r"\bthat\s+(?:time|thing)\s+you\s+(?:mentioned|said|told\s+me|talked\s+about)\b"
    r")",
    re.IGNORECASE,
)
_CLAIM_ZH = re.compile(
    r"(?:"
    r"你(?:之前|以前|上次|上回|前几天|前段时间|那天|那次|早前|先前|曾经)(?:跟我|和我|给我|对我)?"
    r"(?:说|提|讲|聊)(?:过|到|起|的)?|"
    r"你(?:跟我|和我|给我|对我)?(?:说过|提过|提到过|讲过|聊过|说起过|提起过|说起|提到|讲到|聊到)|"
    r"你不是说|"
    r"(?:之前|以前|上次|上回|前几天|前段时间|那天|那次)你(?:跟我|和我|给我|对我)?(?:说|提|讲|聊)|"
    r"(?:还|你还)?记得(?:你|咱们|我们|那次|那个|上次|之前)|"
    r"像你说的|正如你说|如你所说|你常说|你老说|你总说|你一直说|你不是说过?"
    r")"
)
_CLAIM_JA = re.compile(
    r"(?:"
    r"(?:前に|以前|前回|この前|あの時|先日|いつか)?(?:言って(?:た|いた|ました)|話して(?:た|いた|ました)|"
    r"教えてくれた|言ってくれた|話してくれた)(?:よね|でしょ|じゃん|っけ|ね)?|"
    r"覚えて(?:る|いる|ます)(?:？|\?|か)?|"
    r"言ってた通り|言った通り|あなたが言った|君が言った|前に話した"
    r")"
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_QUOTE_SPAN_RE = re.compile(r"[\"“”„«»『「](.+?)[\"“”„«»』」]")

# 中性改写模板（按语言；hash 取一条，同会话稳定）
_NEUTRAL: Dict[str, List[str]] = {
    "en": ["how have you been?", "what have you been up to?", "how's your week going?"],
    "zh": ["最近怎么样？", "这几天都在忙什么？", "最近过得好吗？"],
    "ja": ["最近どう？", "元気にしてた？", "最近何してた？"],
    "th": ["ช่วงนี้เป็นยังไงบ้าง", "ช่วงนี้ทำอะไรอยู่บ้าง"],
    "es": ["¿cómo has estado?", "¿qué has hecho últimamente?"],
    "pt": ["como você tem estado?", "o que tem feito ultimamente?"],
    "fr": ["comment tu vas ces derniers temps ?", "tu fais quoi de beau en ce moment ?"],
    "de": ["wie geht's dir so?", "was machst du gerade so?"],
    "ko": ["요즘 어때?", "요즘 뭐 하고 지내?"],
}

# 英文停用词（抽实体时剔掉）：代词 / 冠词 / 介词 / 引用短语本身的词
_STOP_EN = frozenset("""
a an the and or but so if then that this those these there here it its it's i me my mine you your
yours we us our he she they them his her their be been being am is are was were do does did done
have has had having will would shall should can could may might must to of in on at by for from
with about into over after before again further once up down out off under between through during
just only also very really too quite still even ever never always often sometimes back ago while
time last other day earlier once when how what which who whom where why remember said say mentioned
told tell talked talk brought bring saying telling kept keep used put like as thinking think thought
was were going go went get got did do ever end up thing things something anything nothing everything
""".split())
_STOP_ZH = frozenset(list("你我他她它们的了吗呢啊吧呀哦嗯那这个些还也都就是在有和跟给对说提讲聊过到起来去做想要会能可以没不很太挺好像应该已经之前以前上次上回前几天前段时间那天那次记得咱们我们像正如所说常老总一直不是") + [
    "之前", "以前", "上次", "上回", "前几天", "前段时间", "那天", "那次", "记得", "咱们", "我们", "还是", "怎么", "什么", "时候", "现在", "今天", "明天", "昨天",
])
_STOP_JA = frozenset(list("のはがをにでとへもやかなねよだですますしたていてるいるあるこのそのあのこれそれあれ私君あなた僕俺前以前前回先日いつか時言話教覚通") + [
    "前に", "以前", "前回", "この前", "あの時", "先日", "言って", "話して", "教えて", "覚えて", "通り",
])

_MEMORY_PROVIDER: Optional[Callable[[str, str], List[str]]] = None
_mem_lock = threading.Lock()
_mem_conns: Dict[str, sqlite3.Connection] = {}


def set_memory_facts_provider(fn: Optional[Callable[[str, str], List[str]]]) -> None:
    """装配层可注册 ``(chat_key, account_id) -> [fact_text, ...]``；未注册走 bot.db 只读回落。"""
    global _MEMORY_PROVIDER
    _MEMORY_PROVIDER = fn


def _norm_lang(lang: Any, text: str) -> str:
    s = str(lang or "").strip().lower().replace("_", "-")
    if s and s != "unknown":
        return "zh" if s.startswith("zh") else s.split("-", 1)[0]
    if _KANA_RE.search(text):
        return "ja"
    if _CJK_RE.search(text):
        return "zh"
    return "en"


def _claim_regex(lang: str) -> "re.Pattern[str]":
    if lang == "zh":
        return _CLAIM_ZH
    if lang == "ja":
        return _CLAIM_JA
    return _CLAIM_EN


def _split_sentences(text: str, lang: str) -> List[str]:
    t = str(text or "").strip()
    if not t:
        return []
    if lang in ("zh", "ja"):
        parts = re.split(r"(?<=[。！？!?；;\n])\s*", t)
    else:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[¿¡])|\n+", t)
    return [p for p in parts if p and p.strip()]


def _join(parts: Sequence[str], lang: str) -> str:
    sep = "" if lang in ("zh", "ja") else " "
    return sep.join(p.strip() for p in parts if p and p.strip())


def _stem_en(w: str) -> str:
    w = w.lower().rstrip("'")
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def extract_keywords(sentence: str, lang: str) -> List[str]:
    """被引用句里的实体 / 关键词（去掉引用短语本身与停用词）。英文按词干；中日文按 2 字片段。"""
    s = str(sentence or "")
    s = _claim_regex(lang).sub(" ", s)
    if lang in ("zh", "ja"):
        stop = _STOP_ZH if lang == "zh" else _STOP_JA
        for w in sorted(stop, key=len, reverse=True):
            if len(w) > 1:
                s = s.replace(w, " ")
        chunks = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\u30a0-\u30ffA-Za-z0-9]+", s)
        out: List[str] = []
        for c in chunks:
            c = "".join(ch for ch in c if ch not in stop) if not re.match(r"^[A-Za-z0-9]+$", c) else c.lower()
            if not c:
                continue
            if re.match(r"^[a-z0-9]+$", c):
                if len(c) >= 2 and c not in _STOP_EN:
                    out.append(c)
                continue
            if len(c) == 1:
                continue
            if len(c) == 2:
                out.append(c)
            else:
                out.extend(c[i:i + 2] for i in range(len(c) - 1))
        seen: List[str] = []
        for k in out:
            if k not in seen:
                seen.append(k)
        return seen
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{1,}", s)
    out_en: List[str] = []
    for w in words:
        lw = w.lower().strip("'-")
        if not lw or lw in _STOP_EN or len(lw) < 3:
            continue
        st = _stem_en(lw)
        if st and st not in out_en:
            out_en.append(st)
    return out_en


def _corpus(history_texts: Optional[Iterable[Any]], memory_facts: Optional[Iterable[Any]]) -> Tuple[str, str]:
    def _flat(xs: Optional[Iterable[Any]]) -> str:
        if not xs:
            return ""
        buf: List[str] = []
        for x in xs:
            if isinstance(x, dict):
                x = x.get("text") or x.get("content") or ""
            s = str(x or "").strip()
            if s:
                buf.append(s)
        return "\n".join(buf).lower()
    return _flat(history_texts), _flat(memory_facts)


def _anchored_in(keywords: Sequence[str], corpus: str, lang: str) -> bool:
    if not corpus:
        return False
    if lang in ("zh", "ja"):
        return any(k and k in corpus for k in keywords)
    for k in keywords:
        if not k:
            continue
        # 词干前缀匹配：hik → hiking / hike / hikes；要求词边界起始
        if re.search(r"\b" + re.escape(k), corpus):
            return True
    return False


def _in_quotes(sentence: str, phrase_span: Tuple[int, int]) -> bool:
    for m in _QUOTE_SPAN_RE.finditer(sentence):
        if m.start() <= phrase_span[0] and phrase_span[1] <= m.end():
            return True
    return False


def _is_media_accept(sentence: str) -> bool:
    """Q-36：处置句「发来看看 / send it over」（共享词表 commitment_guard.is_media_accept_prompt；缺席 → False）。"""
    try:
        from src.inbox.commitment_guard import is_media_accept_prompt
        return bool(is_media_accept_prompt(sentence))
    except Exception:
        return False


def neutral_line(lang: str, seed: str = "") -> str:
    pool = _NEUTRAL.get(lang) or _NEUTRAL["en"]
    if not seed:
        return pool[0]
    h = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def find_claims(text: str, lang: str = "") -> List[str]:
    """出稿里的引用短语列表（纯检测，供日志 / 测试）。"""
    try:
        lg = _norm_lang(lang, text)
        return [m.group(0) for m in _claim_regex(lg).finditer(str(text or ""))]
    except Exception:
        return []


def check_claims(
    text: str, *, history_texts: Optional[Iterable[Any]] = None,
    memory_facts: Optional[Iterable[Any]] = None, lang: str = "", seed: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """引用锚点守卫主入口。返回 ``(text, report)``。

    ``report``: ``phrases`` 命中短语 / ``keywords`` 抽出的实体 / ``anchored`` 是否找到锚点 /
    ``anchor`` ``history`` | ``memory`` | ``""`` / ``action`` ``clean`` | ``pass`` | ``rewrite`` /
    ``rewritten`` 被改写的句子数。绝不抛、绝不返回空。
    """
    src = str(text or "")
    rep: Dict[str, Any] = {"phrases": [], "keywords": [], "anchored": None, "anchor": "",
                           "action": "clean", "rewritten": 0}
    if not src.strip():
        return src, rep
    try:
        lg = _norm_lang(lang, src)
        rx = _claim_regex(lg)
        if not rx.search(src):
            return src, rep
        hist, mem = _corpus(history_texts, memory_facts)
        has_history = bool(hist.strip())
        sents = _split_sentences(src, lg)
        out: List[str] = []
        any_pass = False
        for s in sents:
            hits = list(rx.finditer(s))
            if not hits:
                out.append(s)
                continue
            phrases = [m.group(0) for m in hits]
            rep["phrases"].extend(p for p in phrases if p not in rep["phrases"])
            # 误伤保护 ①：引用短语在引号里（转述客户原话）
            if all(_in_quotes(s, (m.start(), m.end())) for m in hits):
                out.append(s)
                any_pass = True
                continue
            # 误伤保护 ②：整句是历史里某条的逐字复述
            s_norm = re.sub(r"\s+", " ", s.strip().lower())
            if s_norm and hist and s_norm in hist:
                out.append(s)
                any_pass = True
                continue
            # 误伤保护 ③（Q-36 #313）：对客户「提议发图」的处置句（「你刚说的那几个菜，发来看看」
            # / "like you said, send it over"）——接受 + 期待不是编造共同回忆，无锚点也不改写。
            if _is_media_accept(s):
                out.append(s)
                any_pass = True
                continue
            kws = extract_keywords(s, lg)
            for k in kws:
                if k not in rep["keywords"]:
                    rep["keywords"].append(k)
            anchor = ""
            if kws:
                if _anchored_in(kws, hist, lg):
                    anchor = "history"
                elif _anchored_in(kws, mem, lg):
                    anchor = "memory"
            elif has_history:
                anchor = "history"           # 裸引用（like you said）：有历史即放行
            if anchor:
                rep["anchor"] = rep["anchor"] or anchor
                out.append(s)
                any_pass = True
                continue
            rep["rewritten"] += 1
            out.append("")                   # 先占位，下面决定换中性句还是删
        if rep["rewritten"] == 0:
            rep["anchored"] = True
            rep["action"] = "pass" if any_pass else "clean"
            return src, rep
        rep["anchored"] = False
        rep["action"] = "rewrite"
        kept = [p for p in out if p]
        neutral = neutral_line(lg, seed)
        if not kept:
            return neutral, rep
        # 其余句子已经在向对方提问 → 直接删；否则用中性句补一问
        tail = kept[-1].strip()
        if tail.endswith(("?", "？")):
            return _join(kept, lg), rep
        if lg in ("zh", "ja"):
            return _join(kept + [neutral], lg), rep
        return _join(kept + [neutral[:1].upper() + neutral[1:]], lg), rep
    except Exception:
        logger.debug("[claim-guard] 处理异常（原文放行）", exc_info=True)
        rep["action"] = "clean"
        return src, rep


def log_report(rep: Dict[str, Any], *, conversation_id: str = "", stage: str = "") -> None:
    """``[claim-guard] conv=… stage=… phrase=… anchored=1|0 anchor=… action=pass|rewrite``（clean 不记）。"""
    try:
        if str(rep.get("action") or "clean") == "clean":
            return
        logger.info(
            "[claim-guard] conv=%s stage=%s phrase=%s keywords=%s anchored=%d anchor=%s action=%s",
            conversation_id or "-", stage or "-",
            "|".join(str(p) for p in (rep.get("phrases") or [])[:3]) or "-",
            "|".join(str(k) for k in (rep.get("keywords") or [])[:6]) or "-",
            1 if rep.get("anchored") else 0, rep.get("anchor") or "-", rep.get("action"))
    except Exception:
        pass


# ── 记忆事实回落读取（bot.db 只读）────────────────────────────────────────────
_BOT_DB_NAME = "bot.db"


def _bot_db_path(store: Any) -> Optional[Path]:
    try:
        from src.compliance.runtime import runtime_config
        p = ((runtime_config() or {}).get("memory") or {}).get("db_path")
        if p:
            pp = Path(str(p))
            if pp.exists():
                return pp
    except Exception:
        pass
    base = getattr(store, "_db_path", None)
    if not base:
        return None
    try:
        db = Path(base).parent / _BOT_DB_NAME
        return db if db.exists() else None
    except Exception:
        return None


def memory_facts_for(chat_key: str, account_id: str = "", *, store: Any = None, limit: int = 300) -> List[str]:
    """该客户的记忆事实文本（provider 优先；否则 ``bot.db`` ``episodic_memory`` 只读反查，
    键匹配 ``chat_key`` / ``acct:chat_key`` / ``*:chat_key``）。任何失败 → 空列表。"""
    ck = str(chat_key or "").strip()
    if not ck:
        return []
    if _MEMORY_PROVIDER is not None:
        try:
            return [str(x) for x in (_MEMORY_PROVIDER(ck, str(account_id or "")) or []) if str(x).strip()]
        except Exception:
            logger.debug("[claim-guard] memory provider 异常", exc_info=True)
            return []
    db = _bot_db_path(store)
    if db is None:
        return []
    key = str(db)
    try:
        with _mem_lock:
            conn = _mem_conns.get(key)
            if conn is None:
                conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                                       timeout=2, check_same_thread=False)
                _mem_conns[key] = conn
            acct = str(account_id or "").strip()
            rows = conn.execute(
                "SELECT content FROM episodic_memory WHERE COALESCE(status,'active')='active' AND "
                "(user_id=? OR user_id LIKE ? OR user_id=?) ORDER BY id DESC LIMIT ?",
                (ck, f"%:{ck}", f"{acct}:{ck}" if acct else ck, max(1, min(int(limit or 300), 2000))),
            ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        logger.debug("[claim-guard] bot.db 反查失败（按无记忆）", exc_info=True)
        with _mem_lock:
            _mem_conns.pop(key, None)
        return []


def history_texts_for(store: Any, conversation_id: str, limit: int = 200) -> List[str]:
    """最近 N 条会话文本（双向；软删 / 撤回行由 store 自己剔除）。任何失败 → 空列表。"""
    if store is None or not conversation_id or not hasattr(store, "list_recent_messages"):
        return []
    try:
        rows = store.list_recent_messages(str(conversation_id), limit=max(1, min(int(limit or 200), 500))) or []
    except Exception:
        return []
    out: List[str] = []
    for r in rows:
        try:
            t = str((r or {}).get("text") or "").strip()
        except Exception:
            t = ""
        if t:
            out.append(t)
    return out


def _reset_for_tests() -> None:
    global _MEMORY_PROVIDER
    _MEMORY_PROVIDER = None
    with _mem_lock:
        for c in _mem_conns.values():
            try:
                c.close()
            except Exception:
                pass
        _mem_conns.clear()


__all__ = [
    "check_claims", "find_claims", "extract_keywords", "neutral_line", "log_report",
    "memory_facts_for", "history_texts_for", "set_memory_facts_provider",
]
