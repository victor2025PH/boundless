"""会话语言识别：他加禄 / 宿务 / Taglish 补强 + 会话级投票（2026-10-08 智语 · P1-5）。

173 实测：conversations.language 里 zh 178、unknown 187（46%）、en 41、tl 1。原因有三：
1. 全局检测器 ``translation_service.detect_language`` 的 tl 线索只有 5 个词
   （salamat / kumusta / magkano / paano / mahal kita），日常 Taglish
   「Pwede po ba ma-refund yung order ko?」一个都不中，被判成 en；
   而且线索是**子串**匹配，id 的 "saya" 会命中他加禄词 masaya、"halo" 会命中 halos。
2. 入站护栏 ``detect_inbound_language`` 把短的纯 ASCII 消息一律判 unknown（防「OK」翻
   中文会话），于是「Sige po」「Salamat po」这类明确的他加禄短句也被丢弃。
3. 会话语言只看**采集时末条**消息；末条是出站 / 表情 / 图片就一直 unknown，
   哪怕历史里早有清楚的入站证据。

本模块只做纯函数（零 IO）：
- ``refine_latin_language(text, base)``：基础结果是 en / unknown / id 时，按他加禄 /
  宿务功能词整词计分改判 tl；CJK 为主、夹少量英文词的消息判 zh。
- ``detect_message_language(text)``：入站单条消息的最终判定（normalizer 调用）。
- ``vote_session_language(texts)``：一组入站文本 → 会话语言（实时补写与历史回填共用）。

宿务语（Bisaya）不单列语言码：下游翻译 / 回复语种表没有 ceb，对宿务客户用 tl 回复
可以接受，``detail`` 里另带 ``variant="ceb"`` 供需要的地方（如 persona_guard 兜底句）使用。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

_WORD_RE = re.compile(r"[a-zñ]+(?:['’-][a-zñ]+)*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")

# 只在他加禄语里常见、在英 / 印尼 / 西 / 葡里几乎不会作为独立词出现的
_TL_UNAMBIGUOUS = frozenset({
    "po", "opo", "salamat", "kumusta", "kamusta", "magkano", "paano", "pano",
    "naman", "pwede", "puwede", "pede", "hindi", "yung", "iyong", "talaga",
    "sige", "diba", "nyo", "niyo", "ninyo", "natin", "namin", "kayo", "ikaw",
    "mga", "ng", "nang", "lang", "bakit", "ano", "anong", "saan", "kailan",
    "gusto", "ayaw", "sana", "muna", "pala", "daw", "raw", "kasi", "nga",
    "wala", "meron", "mayroon", "yun", "yan", "ito", "iyan", "dito", "doon",
    "sila", "siya", "tayo", "kami", "akin", "ako", "din", "rin", "ba",
    "mahal", "tapos", "bayad", "padala", "magandang", "umaga",
    "gabi", "hapon", "kuya", "lods", "petsa", "ngayon", "kanina", "mamaya",
    "masaya", "maganda", "sobrang", "naku", "pasensya", "pakisuyo", "paki",
})
# 短消息（≤3 词）里单独出现也能定案的：排除 hindi（英文 "Hindi" 语言名）/ ba / ako 等
_TL_SHORT_OK = frozenset({
    "po", "opo", "salamat", "kumusta", "kamusta", "magkano", "paano", "pano",
    "naman", "pwede", "puwede", "pede", "yung", "talaga", "sige", "diba", "nyo",
    "niyo", "ninyo", "kayo", "mga", "bakit", "anong", "saan", "kailan", "gusto",
    "sana", "muna", "pala", "kasi", "nga", "meron", "mayroon", "tayo", "kami",
    "padala", "bayad",
})
# 他加禄常见、但单独出现太容易撞别的语言的短词：只做加分，不单独定案
_TL_WEAK = frozenset({"ka", "ko", "mo", "sa", "na", "ang", "si", "ni", "at", "ay", "pa", "may"})
_CEB = frozenset({
    "unsa", "unsay", "nimo", "kaayo", "dili", "gyud", "jud", "lagi", "nako", "ako",
    "asa", "ngano", "kinsa", "pila", "palihug", "karon", "ug", "sab", "pud", "diay",
    "bitaw", "naa", "imong", "akong", "kana", "kini", "ayo", "salamat", "ganahan",
    "unsaon", "nganong", "wala", "ra", "man", "ta", "mi", "ka",
})
_CEB_STRONG = frozenset({
    "unsa", "unsay", "nimo", "kaayo", "dili", "gyud", "jud", "nako", "asa", "ngano",
    "kinsa", "pila", "palihug", "karon", "diay", "bitaw", "naa", "imong", "akong",
    "ganahan", "unsaon", "nganong",
})
# 印尼 / 马来独有高频词：同时出现就别改判 tl
_ID_MARKERS = frozenset({
    "saya", "tidak", "terima", "kasih", "bagaimana", "selamat", "apa", "ini", "itu",
    "bisa", "mau", "aku", "kamu", "yang", "dengan", "untuk", "sudah", "belum",
})

_LATIN_BASES = frozenset({"en", "unknown", "id", ""})


def _tokens(text: str) -> List[str]:
    return [w.lower().replace("’", "'") for w in _WORD_RE.findall(text or "")]


def tl_ceb_signal(text: str) -> Dict[str, int]:
    """他加禄 / 宿务 / 印尼线索计数（去重后的整词）。"""
    toks = _tokens(text)
    parts = set()
    for t in toks:
        parts.add(t)
        if "-" in t:  # mag-order / i-cancel / ma-refund：前缀拆出来也算
            parts.update(p for p in t.split("-") if p)
    return {
        "tokens": len(toks),
        "tl": len(parts & _TL_UNAMBIGUOUS),
        "tl_only": len((parts & _TL_UNAMBIGUOUS) - _CEB),
        "tl_short": len(parts & _TL_SHORT_OK),
        "tl_weak": len(parts & _TL_WEAK),
        "ceb": len(parts & _CEB),
        "ceb_strong": len(parts & _CEB_STRONG),
        "id": len(parts & _ID_MARKERS),
    }


def _tl_variant(sig: Dict[str, int]) -> Optional[str]:
    """按线索给出 'tl' / 'ceb' / None。"""
    tl, weak, ceb_s = sig["tl"], sig["tl_weak"], sig["ceb_strong"]
    if ceb_s >= 2 or (ceb_s >= 1 and sig["tl_only"] == 0):
        return "ceb"
    if sig["id"] >= 2 and sig["id"] >= tl:
        return None
    if tl >= 2 or (tl >= 1 and weak >= 2):
        return "tl"
    if sig["tl_short"] >= 1 and sig["tokens"] <= 3:
        return "tl"
    return None


def refine_latin_language(text: str, base: str) -> Tuple[str, str]:
    """在基础检测结果上补强，返回 ``(lang, variant)``；variant 仅 ``"ceb"`` 或 ``""``。

    - CJK 字 ≥2 且拉丁词 ≤3 个 → zh（「我想问price」「这个GCash可以吗」）；
    - 基础结果是 en / unknown / id 时，他加禄 / 宿务线索够 → tl（宿务带 variant=ceb）；
    - 其余原样返回。
    """
    t = str(text or "")
    b = str(base or "unknown")
    cjk = len(_CJK_RE.findall(t))
    if cjk >= 2 and len(_LATIN_WORD_RE.findall(t)) <= 3 and b in _LATIN_BASES | {"zh"}:
        return "zh", ""
    if b not in _LATIN_BASES:
        return b, ""
    v = _tl_variant(tl_ceb_signal(t))
    if v == "ceb":
        return "tl", "ceb"
    if v == "tl":
        return "tl", ""
    return b or "unknown", ""


def detect_message_language(text: str, base: Optional[str] = None) -> str:
    """单条入站消息的语言（``base`` 不给时内部调全局确定性检测器）。"""
    if base is None:
        from src.ai.translation_service import detect_language
        base = detect_language(text)
    return refine_latin_language(text, base)[0]


def vote_session_language(
    texts: Iterable[str],
    *,
    base_fn=None,
    min_weight: int = 4,
) -> Dict[str, object]:
    """一组**入站**文本 → 会话语言。

    每条按 ``detect_message_language`` 判定，按可见字符数加权（上限 200）；unknown 不投票。
    tl 与 en 同场时，tl 的字数占比或条数占比 ≥ 30% 即判 tl（Taglish 客户经常夹整句英文）。
    所有单条都判不出时，把短消息拼起来再判一次（多条「sige po」「ok po」「salamat」）。
    返回 ``{"lang", "variant", "votes", "evidence"}``；判不出 lang="unknown"。
    """
    if base_fn is None:
        from src.inbox.normalizer import detect_inbound_language as base_fn  # noqa: N806
    votes: Dict[str, int] = {}
    counts: Dict[str, int] = {}
    ceb_w = 0
    shorts: List[str] = []
    n = 0
    for raw in texts:
        t = str(raw or "").strip()
        if not t:
            continue
        n += 1
        lang = base_fn(t)
        lang, var = refine_latin_language(t, lang)
        if lang in ("unknown", ""):
            shorts.append(t)
            continue
        if lang == "tl" and not var and _tl_variant(tl_ceb_signal(t)) == "ceb":
            var = "ceb"  # base_fn 已改判 tl 时 refine 不再给 variant，这里补回
        w = min(len(t), 200)
        votes[lang] = votes.get(lang, 0) + w
        counts[lang] = counts.get(lang, 0) + 1
        if var == "ceb":
            ceb_w += w
    if not votes and shorts:
        joined = " ".join(shorts)
        lang, var = refine_latin_language(joined, "unknown")
        if lang != "unknown":
            votes[lang] = min(len(joined), 200)
            counts[lang] = 1
            if var == "ceb":
                ceb_w = votes[lang]
    total = sum(votes.values())
    if not votes or total < min_weight:
        return {"lang": "unknown", "variant": "", "votes": votes,
                "evidence": "no_inbound_text" if n == 0 else "insufficient"}
    latin = sum(v for k, v in votes.items() if k in ("en", "tl", "id"))
    latin_n = sum(v for k, v in counts.items() if k in ("en", "tl", "id"))
    tl_share = max(votes.get("tl", 0) / latin if latin else 0.0,
                   counts.get("tl", 0) / latin_n if latin_n else 0.0)
    if votes.get("tl", 0) and tl_share >= 0.3 and votes.get("zh", 0) < latin:
        win = "tl"
    else:
        win = max(votes.items(), key=lambda kv: kv[1])[0]
    variant = "ceb" if (win == "tl" and ceb_w * 2 >= votes.get("tl", 0)) else ""
    return {"lang": win, "variant": variant, "votes": votes, "evidence": "inbound_vote"}
