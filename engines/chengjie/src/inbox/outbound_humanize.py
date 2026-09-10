# -*- coding: utf-8 -*-
"""出站确定性「去 AI 标点 / 句式」后处理（O-1 B · #253 #254 · D-O2，2026-09-08）。

证据 RYE8Y8 / 78W8DN：一天 5 条 em dash 跨 3 个 WhatsApp 账号（同一人设）；「复述 → 破折号
引申 → 人生感悟式总结 / isn't it 反问尾」固定套路；出站文本不落日志、无法统计全量。提示词
禁令模型遵守率低——**确定性处理才可验收**（200 样例 0 em dash）。

本模块两层：

1. :func:`humanize`（**纯函数**，零 I/O）：``(text, lang, *, cfg) -> (text, stats)``
   - U+2014/U+2013/U+2015/U+2012 破折号 → 逗号（数字区间 → 连字符；句尾 → 删）；中文
     「——」按语言表 → 「，」；日文 → 「、」
   - 弯引号 “ ” „ → 直引号 "（CJK 语种不动：那是中文正体引号）；’ ‘ → '（各语种）
   - 分号 ; → 句号并大写下一字（表情 ;) 不动）；； → 。
   - … → ...（连串折叠）
   - °C / °F → 「度 / degrees / grados…」；% → 「percent / por ciento…」（拉丁语种表；CJK 保留）
   - 冒号列表 / 项目符号 / 编号 / markdown 粗体标题 → 散文（≥2 行列表才动）
   - 连续 ≥2 个 ! → 1 个；emoji 串 ≥3 → 留 2
   - 句尾反问尾巴 isn't it / aren't they / right? / 对吧 / 是吧 → 去掉
   - 结尾总结 / 感悟句（句式表可配 ``summary_patterns``）→ 删（只删末句、且至少保留 1 句）
   - 默认 ≤2 句：超长**精简不切分**（与 L-1 A「不拆条」一致）——保留前 N-1 句 + 末句若是
     向客户提问则保留，否则取前 N 句
2. :func:`apply_outbound_humanize`：带配置门（``inbox.l2_autosend.humanize``，默认开、热更）
   与**绕过**（``origin ∈ {manual, verbatim, human}``；deferred 队列里 ``care:verbatim`` /
   ``extra.verbatim`` 的原文行经 :func:`deferred_verbatim_pending` 只读反查识别）+ 每条出站
   一行日志 ``[outbound] conv=… origin=… lang=… len=… punct_fix=n style_fix=n trimmed=n``
   （78W8DN 要的出站文本日志；文本只留 40 字预览 + 指纹，不落全文）。

挂点：``outbound_translate.translate_outbound_text`` 出口（所有 AI 出站——autosend 自动链 /
关怀润色 / 目标冲刺 / 问候 / 协议直发 / deferred——都先过它；HOLD=None 不动）。
**verbatim 原文直发与人工手发一律不经本模块**——用户写的破折号是用户的。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 配置 ─────────────────────────────────────────────────────────────────
DEFAULT_MAX_SENTENCES = 2
_CFG_KEY = "humanize"

# 人工 / 原文来源：一律不经后处理
BYPASS_ORIGINS = frozenset({"manual", "verbatim", "human", "identity"})


def resolve_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 ``inbox.l2_autosend.humanize``（缺省全开、≤2 句）。``humanize: false`` 布尔简写。"""
    raw: Any = None
    try:
        if config is None:
            from src.compliance.runtime import runtime_config
            config = runtime_config() or {}
        raw = (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get(_CFG_KEY)
    except Exception:
        raw = None
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    if not isinstance(raw, dict):
        raw = {}
    try:
        max_s = int(raw.get("max_sentences", DEFAULT_MAX_SENTENCES) or 0)
    except (TypeError, ValueError):
        max_s = DEFAULT_MAX_SENTENCES
    pats = raw.get("summary_patterns")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_sentences": max_s,                       # 0 = 不限
        "strip_summary": bool(raw.get("strip_summary", True)),
        "strip_tag_questions": bool(raw.get("strip_tag_questions", True)),
        "flatten_lists": bool(raw.get("flatten_lists", True)),
        "units": bool(raw.get("units", True)),
        # O-1 C：陪伴域客服腔句级剥离（persona_guard.rewrite_service_tone），先于标点处理
        "service_tone": bool(raw.get("service_tone", True)),
        "summary_patterns": [str(p) for p in pats] if isinstance(pats, (list, tuple)) else [],
        "skip_langs": {str(x).lower() for x in (raw.get("skip_langs") or [])
                       if isinstance(raw.get("skip_langs"), (list, tuple))},
    }


def _service_tone_pass(text: str, cfg: Dict[str, Any]) -> Tuple[str, str, int]:
    """陪伴域客服腔守卫（O-1 C）：返回 ``(text, action, hits)``。非陪伴域 / 关 → 原文 clean。

    出口这里只做「改写一次」；剥完为空（整段客服腔）没有人审出口可退 → 原文放行并记
    ``review_needed``（B 线拟稿链在 enrich_draft 已于更早处转人审，这里兜的是 deferred /
    主动触达 / 协议链漏网）。绝不抛。
    """
    src = str(text or "")
    if not src.strip() or not cfg.get("service_tone", True):
        return src, "clean", 0
    try:
        from src.utils.persona_guard import companion_tone_guard_active, rewrite_service_tone
        if not companion_tone_guard_active():
            return src, "clean", 0
        out, rep = rewrite_service_tone(src, record_stats=False)
        act = str(rep.get("action") or "clean")
        n = len(rep.get("hits") or []) + int(bool(rep.get("three_part"))) + int(bool(rep.get("conditional_close")))
        if act == "rewrite":
            return out, act, n
        if act == "review":
            return src, "review_needed", n
        return src, "clean", 0
    except Exception:
        logger.debug("[outbound_humanize] 客服腔守卫异常（原文放行）", exc_info=True)
        return src, "clean", 0


# ── 语言分类 ─────────────────────────────────────────────────────────────
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf\uf900-\ufaff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _norm_lang(lang: Any, text: str = "") -> str:
    s = str(lang or "").strip().lower().replace("_", "-")
    if s and s != "unknown":
        if s.startswith("zh"):
            return "zh"
        return s.split("-", 1)[0]
    # 文本文字系统回落
    if _CJK_RE.search(text):
        return "ja" if re.search(r"[\u3040-\u30ff]", text) else "zh"
    if _HANGUL_RE.search(text):
        return "ko"
    return "en"


def _is_cjk(lang: str) -> bool:
    return lang in ("zh", "ja")


# ── 标点表 ───────────────────────────────────────────────────────────────
_DASH_CHARS = "\u2014\u2013\u2015\u2012"          # — – ― ‒
_DASH_RE = re.compile(r"[ \t]*[\u2014\u2013\u2015\u2012]+[ \t]*")
_DQ_RE = re.compile(r"[\u201c\u201d\u201e\u201f]")   # “ ” „ ‟
_SQ_RE = re.compile(r"[\u2018\u2019\u201a\u201b]")   # ‘ ’ ‚ ‛
_ELLIPSIS_RE = re.compile(r"(?:\u2026|\.{4,}|(?:\. ){2,}\.)")
_ZH_ELLIPSIS_RE = re.compile(r"(?:\u2026|。{3,}){1,}")
_SEMI_RE = re.compile(r";(?![\-\)\(DPpOo\]])\s*")      # 表情 ;) ;-) ;D 不动
_MULTI_BANG_RE = re.compile(r"!(?:\s*!)+")
_MULTI_BANG_ZH_RE = re.compile(r"！(?:\s*[！!])+|!(?:\s*[！!])*！")
_INTERROBANG_RE = re.compile(r"(?:\?!|!\?)+")
_EMOJI = (r"(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF]"
          r"[\uFE0F\u200D\U0001F3FB-\U0001F3FF]*"
          r"(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF][\uFE0F]?)*)")
_EMOJI_RUN_RE = re.compile(rf"({_EMOJI})({_EMOJI})((?:\s*{_EMOJI})+)")
_DEG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*°\s*([CcFf])\b")
_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")

_DEG_WORD = {
    "en": " degrees", "es": " grados", "fr": " degrés", "de": " Grad", "pt": " graus",
    "it": " gradi", "ru": " градусов", "tr": " derece", "id": " derajat", "ms": " darjah",
    "vi": " độ", "th": " องศา", "zh": "度", "ja": "度", "ko": "도",
}
_PCT_WORD = {
    "en": " percent", "es": " por ciento", "fr": " pour cent", "de": " Prozent",
    "pt": " por cento", "it": " per cento", "ru": " процентов",
}

# 列表 / markdown
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•·▪◦]|\d{1,2}[.)]|[a-hA-H][.)]|[①-⑩])\s+(.*\S)\s*$")
_MD_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_MD_HEAD_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)

# 反问尾巴（只删文本末尾）
_TAG_Q_EN = re.compile(
    r"[,，]?\s*(?:isn'?t\s+(?:it|that|she|he)|aren'?t\s+(?:they|we|you)|don'?t\s+you\s+(?:think|agree)"
    r"|wouldn'?t\s+you\s+(?:say|agree)|am\s+i\s+right|right|you\s+know|no|huh|eh|ya\s+know"
    r"|don'?t\s+you|doesn'?t\s+it|wasn'?t\s+it|weren'?t\s+they|won'?t\s+you|can'?t\s+you)"
    r"\s*\?+\s*$", re.IGNORECASE)
_TAG_Q_ZH = re.compile(r"[,，]?\s*(?:对吧|是吧|不是吗|是不是|你说呢|你觉得呢|对不对|是不是呀|对吧？|可不是吗)\s*[？?]+\s*$")

# 结尾总结 / 感悟句（句首匹配；可配追加）
_SUMMARY_EN_DEFAULT = [
    r"at the end of the day\b", r"in the end\b", r"after all\b", r"all in all\b",
    r"life is\b", r"life's\b", r"that'?s (?:what|the beauty|the thing|life|how|the magic|the charm)\b",
    r"sometimes (?:it'?s|the|life|all|that'?s|you just|we all)\b",
    r"it'?s (?:the little|these little|those little|funny how|amazing how|moments like|the small|these small)\b",
    r"here'?s to\b", r"there'?s something (?:special|beautiful|magical|comforting|lovely)\b",
    r"maybe that'?s\b", r"isn'?t it (?:funny|strange|amazing|wonderful)\b",
    r"the best things\b", r"nothing beats\b", r"everything happens\b",
    r"(?:the )?(?:small|little) things\b", r"that'?s what makes\b",
    r"there'?s nothing quite like\b", r"it'?s moments like\b", r"such is life\b",
    r"what a (?:beautiful|wonderful|lovely) (?:reminder|thought|way)\b",
    r"a (?:gentle|little|sweet) reminder\b", r"cheers to\b", r"and that'?s (?:okay|ok|fine|enough)\b",
]
_SUMMARY_ZH_DEFAULT = [
    r"生活就是", r"人生就是", r"有时候", r"其实", r"所以说", r"总之", r"这就是", r"这大概就是",
    r"也许这就是", r"最美的", r"最好的", r"小确幸", r"平凡的日子", r"日子就是", r"生活本来",
    r"愿你", r"这才是", r"说到底", r"归根结底", r"人生啊", r"生活啊",
]

# 句子切分
_SENT_SPLIT_LATIN = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[¿¡])")
# 中日文：句号 / 叹号 / 问号后即句界；「...」后接汉字 / 假名也算（AI 常用省略号连出总结句）
_SENT_SPLIT_CJK = re.compile(r"(?<=[。！？!?])\s*|(?<=\.\.\.)(?=[\u4e00-\u9fff\u3040-\u30ff])")

_STATS_KEYS = ("dash", "quote", "semicolon", "ellipsis", "unit", "exclaim", "emoji",
               "list", "tag_q", "summary", "trimmed")


def _new_stats() -> Dict[str, int]:
    return {k: 0 for k in _STATS_KEYS}


# ── 各步骤（每步纯函数：(text, lang, stats) -> text）────────────────────────

def _fix_dashes(t: str, lang: str, st: Dict[str, int]) -> str:
    if not any(ch in t for ch in _DASH_CHARS):
        return t
    cjk = _is_cjk(lang)
    # 日文顿号；中文全角逗号；泰文不惯用逗号 → 空格；其余半角逗号
    comma = "、" if lang == "ja" else ("，" if cjk else (" " if lang == "th" else ", "))

    def _sub(m: "re.Match[str]") -> str:
        s, e = m.start(), m.end()
        before = t[:s]
        after = t[e:]
        st["dash"] += 1
        prev = before[-1:] if before else ""
        nxt = after[:1] if after else ""
        if not before.strip():
            return ""                         # 行首破折号（对白引导）→ 删
        if not after.strip() or nxt in ".!?。！？,，;；)）\n":
            return ""                         # 句尾 / 标点前 → 删
        if prev.isdigit() and nxt.isdigit():
            return "-"                        # 数字区间 9–5 / 2019–2020
        if prev in ",，、":
            return " " if not cjk else ""
        if nxt in "\"'“‘":
            return comma
        return comma

    out = _DASH_RE.sub(_sub, t)
    # 修补「,,」「, ,」这类连逗
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"，\s*，", "，", out)
    return out


def _fix_quotes(t: str, lang: str, st: Dict[str, int]) -> str:
    n_sq = len(_SQ_RE.findall(t))
    if n_sq:
        t = _SQ_RE.sub("'", t)
        st["quote"] += n_sq
    if not _is_cjk(lang):
        n_dq = len(_DQ_RE.findall(t))
        if n_dq:
            t = _DQ_RE.sub('"', t)
            st["quote"] += n_dq
    return t


def _fix_semicolons(t: str, lang: str, st: Dict[str, int]) -> str:
    if _is_cjk(lang):
        n = t.count("；")
        if n:
            st["semicolon"] += n
            t = t.replace("；", "。")
        return t
    if ";" not in t:
        return t

    def _sub(m: "re.Match[str]") -> str:
        st["semicolon"] += 1
        return ". "

    out = _SEMI_RE.sub(_sub, t)
    # 分号后原是小写开头 → 句首大写
    out = re.sub(r"\. ([a-z])", lambda m: ". " + m.group(1).upper(), out)
    return out


def _fix_ellipsis(t: str, lang: str, st: Dict[str, int]) -> str:
    rx = _ZH_ELLIPSIS_RE if _is_cjk(lang) else _ELLIPSIS_RE
    n = len(rx.findall(t))
    if n:
        st["ellipsis"] += n
        t = rx.sub("...", t)
    return t


def _fix_units(t: str, lang: str, st: Dict[str, int]) -> str:
    deg = _DEG_WORD.get(lang)
    if deg and "°" in t:
        n = len(_DEG_RE.findall(t))
        if n:
            st["unit"] += n
            t = _DEG_RE.sub(lambda m: m.group(1) + deg, t)
    pct = _PCT_WORD.get(lang)
    if pct and "%" in t:
        n = len(_PCT_RE.findall(t))
        if n:
            st["unit"] += n
            t = _PCT_RE.sub(lambda m: m.group(1) + pct, t)
    return t


def _fix_exclaim_emoji(t: str, lang: str, st: Dict[str, int]) -> str:
    n = len(_INTERROBANG_RE.findall(t))
    if n:
        st["exclaim"] += n
        t = _INTERROBANG_RE.sub("?", t)
    for rx, rep in ((_MULTI_BANG_RE, "!"), (_MULTI_BANG_ZH_RE, "！")):
        n = len(rx.findall(t))
        if n:
            st["exclaim"] += n
            t = rx.sub(rep, t)
    n = len(_EMOJI_RUN_RE.findall(t))
    if n:
        st["emoji"] += n
        t = _EMOJI_RUN_RE.sub(lambda m: m.group(1) + m.group(2), t)
    return t


def _flatten_lists(t: str, lang: str, st: Dict[str, int]) -> str:
    if _MD_BOLD_RE.search(t):
        t = _MD_BOLD_RE.sub(lambda m: m.group(2), t)
        st["list"] += 1
    if _MD_HEAD_RE.search(t):
        t = _MD_HEAD_RE.sub("", t)
        st["list"] += 1
    lines = t.split("\n")
    bullets = [(i, _BULLET_LINE_RE.match(ln)) for i, ln in enumerate(lines)]
    items = [(i, m.group(1).strip()) for i, m in bullets if m]
    if len(items) < 2:
        return t
    st["list"] += 1
    cjk = _is_cjk(lang)
    sep = "，" if cjk else ", "
    end = "。" if cjk else "."
    first_idx = items[0][0]
    intro = " ".join(ln.strip() for ln in lines[:first_idx] if ln.strip())
    tail = [ln.strip() for ln in lines[items[-1][0] + 1:] if ln.strip()]
    body = sep.join(x.rstrip(".。;；,，") for _, x in items)
    if intro:
        intro = intro.rstrip(":：") + ("：" if cjk else ": ")
        joined = intro + body + end
    else:
        joined = body[:1].upper() + body[1:] + end if not cjk else body + end
    if tail:
        joined += ("" if cjk else " ") + " ".join(tail)
    return joined


_PUNCT_STRIP_RE = re.compile(r"[.!?。！？,，、~～…]+")


def _is_fragment(p: str, lang: str) -> bool:
    """超短感叹片段（「哈哈哈！」「Ok.」「Haha yes!」）不单独占一句额度，并入下一句计数。"""
    core = _PUNCT_STRIP_RE.sub("", p).strip()
    if not core:
        return True
    if _is_cjk(lang):
        return len(core.replace(" ", "")) <= 4
    return len(core.split()) <= 2 and len(core) <= 12


def _split_raw(t: str, lang: str) -> List[str]:
    """按句末标点切句（不合并片段）——总结句 / 反问尾按这个粒度删。"""
    t = t.strip()
    if not t:
        return []
    if _is_cjk(lang):
        return [p for p in _SENT_SPLIT_CJK.split(t) if p and p.strip()]
    return [p for p in _SENT_SPLIT_LATIN.split(t) if p and p.strip()]


def _merge_fragments(raw: List[str], lang: str) -> List[str]:
    """超短片段并入**前一句**（「…by noon. Unreal.」是尾巴）；无前句则并入后一句
    （「哈哈哈！今天…」）——句数额度按合并后计。"""
    parts: List[str] = []
    carry = ""
    joiner = "" if _is_cjk(lang) else " "
    for p in raw:
        if carry:
            p = carry + joiner + p
            carry = ""
        if _is_fragment(p, lang):
            if parts:
                parts[-1] = parts[-1] + joiner + p
            else:
                carry = p
            continue
        parts.append(p)
    if carry:
        parts.append(carry)
    return parts


def _split_sentences(t: str, lang: str) -> List[str]:
    return _merge_fragments(_split_raw(t, lang), lang)


def _join_sentences(parts: List[str], lang: str) -> str:
    return ("" if _is_cjk(lang) else " ").join(p.strip() for p in parts if p.strip())


def _strip_tag_question(t: str, lang: str, st: Dict[str, int]) -> str:
    rx = _TAG_Q_ZH if _is_cjk(lang) else _TAG_Q_EN
    m = rx.search(t)
    if not m:
        return t
    st["tag_q"] += 1
    head = t[:m.start()].rstrip()
    if not head:
        return t
    return head + ("。" if _is_cjk(lang) else ".")


def _summary_regexes(lang: str, extra: List[str]) -> List["re.Pattern[str]"]:
    base = _SUMMARY_ZH_DEFAULT if _is_cjk(lang) else _SUMMARY_EN_DEFAULT
    out: List["re.Pattern[str]"] = []
    for p in list(base) + list(extra or []):
        try:
            out.append(re.compile(r"^\s*[\"'“‘(]?\s*(?:" + p + r")", re.IGNORECASE))
        except re.error:
            continue
    return out


def _strip_summary_tail(parts: List[str], lang: str, st: Dict[str, int],
                        extra: List[str]) -> List[str]:
    if len(parts) < 2:
        return parts
    rxs = _summary_regexes(lang, extra)
    out = list(parts)
    # 末句连删（最多两句：「感悟 + 反问」常成对出现），至少留 1 句
    for _ in range(2):
        if len(out) < 2:
            break
        last = out[-1].strip()
        if last.endswith(("?", "？")):
            break                             # 向客户提问的句子不删
        if any(rx.search(last) for rx in rxs):
            out.pop()
            st["summary"] += 1
        else:
            break
    return out


def _limit_sentences(parts: List[str], lang: str, st: Dict[str, int], max_s: int) -> List[str]:
    if max_s <= 0 or len(parts) <= max_s:
        return parts
    last = parts[-1].strip()
    if max_s >= 2 and last.endswith(("?", "？")):
        kept = parts[:max_s - 1] + [last]
    else:
        kept = parts[:max_s]
    st["trimmed"] += len(parts) - len(kept)
    return kept


# ── 主入口（纯函数）─────────────────────────────────────────────────────────

def humanize(text: str, lang: str = "", *, cfg: Optional[Dict[str, Any]] = None,
             mode: str = "default") -> Tuple[str, Dict[str, Any]]:
    """确定性去 AI 标点 / 句式。返回 ``(text, stats)``；输入为空 / 异常 → 原文 + 零统计。

    ``cfg``＝:func:`resolve_cfg` 结果（None → 默认全开）。``mode``：``default`` /
    ``punct_only``（只做标点，不动句式与句数——翻译 identity 等场合）。绝不抛。
    """
    src = str(text or "")
    st: Dict[str, Any] = _new_stats()
    if not src.strip():
        return src, _finish(st, src, src)
    c = cfg if isinstance(cfg, dict) else resolve_cfg({})
    lg = _norm_lang(lang, src)
    if lg in (c.get("skip_langs") or set()):
        return src, _finish(st, src, src)
    try:
        t = src
        t = _fix_dashes(t, lg, st)
        t = _fix_quotes(t, lg, st)
        t = _fix_semicolons(t, lg, st)
        t = _fix_ellipsis(t, lg, st)
        if c.get("units", True):
            t = _fix_units(t, lg, st)
        t = _fix_exclaim_emoji(t, lg, st)
        if mode != "punct_only":
            if c.get("flatten_lists", True):
                t = _flatten_lists(t, lg, st)
            if c.get("strip_tag_questions", True):
                t = _strip_tag_question(t, lg, st)
            raw = _split_raw(t, lg)
            kept_raw = raw
            if c.get("strip_summary", True):
                # 总结句按原始切句粒度删（先于片段合并，否则「Coffee first. 感悟句」合成一句漏删）
                kept_raw = _strip_summary_tail(raw, lg, st, c.get("summary_patterns") or [])
            parts = _merge_fragments(kept_raw, lg)
            n_before = len(parts)
            parts = _limit_sentences(parts, lg, st, int(c.get("max_sentences") or 0))
            # 只有句子集合真被删减才重拼（否则原样，保留原有换行）
            if parts and (len(kept_raw) != len(raw) or len(parts) != n_before):
                t = _join_sentences(parts, lg)
                # 裁掉末句后新的句尾也可能是「…对吧？」——再剥一次尾巴
                if c.get("strip_tag_questions", True):
                    t = _strip_tag_question(t, lg, st)
        t = re.sub(r"[ \t]{2,}", " ", t).strip()
        if not t:
            t = src
        return t, _finish(st, src, t)
    except Exception:
        logger.debug("[outbound_humanize] 处理异常（原文放行）", exc_info=True)
        return src, _finish(_new_stats(), src, src)


def _finish(st: Dict[str, Any], src: str, out: str) -> Dict[str, Any]:
    st["punct_fix"] = int(st.get("dash", 0) + st.get("quote", 0) + st.get("semicolon", 0)
                          + st.get("ellipsis", 0) + st.get("unit", 0) + st.get("exclaim", 0)
                          + st.get("emoji", 0))
    st["style_fix"] = int(st.get("list", 0) + st.get("tag_q", 0) + st.get("summary", 0))
    st["changed"] = out != src
    st["len_in"] = len(src)
    st["len_out"] = len(out)
    return st


def text_fingerprint(text: str) -> str:
    try:
        return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:8]
    except Exception:
        return ""


# ── verbatim 探针：deferred 队列里的原文行 ──────────────────────────────────
_DEF_DB_NAME = "deferred_outbox.db"
_def_lock = threading.Lock()
_def_conns: Dict[str, sqlite3.Connection] = {}


def _deferred_db_path(store: Any) -> Optional[Path]:
    p = getattr(store, "_db_path", None)
    if not p:
        return None
    try:
        db = Path(p).parent / _DEF_DB_NAME
        return db if db.exists() else None
    except Exception:
        return None


def deferred_verbatim_pending(store: Any, conversation_id: str, text: str) -> bool:
    """``deferred_outbox`` 里是否有**仍 pending** 的同文原文行（``care:verbatim`` /
    ``extra.verbatim``）。sender 签名不带来源，只读反查是识别「运营到点原文」的唯一口子；
    drain 在 ``fn`` 返回后才标 sent，故投递当刻该行必为 pending。任何失败 → False。
    """
    if store is None or not conversation_id or not str(text or "").strip():
        return False
    parts = str(conversation_id).split(":", 2)
    if len(parts) < 3:
        return False
    db = _deferred_db_path(store)
    if db is None:
        return False
    key = str(db)
    try:
        with _def_lock:
            conn = _def_conns.get(key)
            if conn is None:
                conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                                       timeout=2, check_same_thread=False)
                _def_conns[key] = conn
            row = conn.execute(
                "SELECT reason, extra FROM deferred_outbox WHERE status='pending' "
                "AND platform=? AND account_id=? AND chat_key=? AND reply_text=? "
                "ORDER BY id DESC LIMIT 1",
                (parts[0], parts[1] or "default", parts[2], str(text).strip()),
            ).fetchone()
    except Exception:
        logger.debug("[outbound_humanize] deferred 反查失败（按非原文）", exc_info=True)
        with _def_lock:
            _def_conns.pop(key, None)
        return False
    if not row:
        return False
    reason = str(row[0] or "")
    try:
        extra = json.loads(row[1] or "{}") or {}
    except Exception:
        extra = {}
    return reason.startswith("care:verbatim") or bool(extra.get("verbatim"))


def _reset_probe_for_tests() -> None:
    with _def_lock:
        for c in _def_conns.values():
            try:
                c.close()
            except Exception:
                pass
        _def_conns.clear()


# ── 出站挂点 ─────────────────────────────────────────────────────────────

def resolve_origin(item: Any, store: Any = None) -> str:
    """出站来源：``item.origin`` 显式 > deferred 原文行反查 > ``auto``。"""
    o = ""
    if isinstance(item, dict):
        o = str(item.get("origin") or item.get("_origin") or "").strip().lower()
        if not o and item.get("verbatim"):
            o = "verbatim"
    if o:
        return o
    try:
        if isinstance(item, dict) and deferred_verbatim_pending(
                store, str(item.get("conversation_id") or ""), str(item.get("text") or "")):
            return "verbatim"
    except Exception:
        pass
    return "auto"


def apply_outbound_humanize(
    text: Optional[str], *, conversation_id: str = "", lang: str = "",
    origin: str = "auto", cfg_root: Optional[Dict[str, Any]] = None,
    stage: str = "", mode: str = "default",
) -> Optional[str]:
    """出站文本后处理 + 每条一行 ``[outbound]`` 日志。``None``（HOLD）原样返回。

    ``origin ∈ BYPASS_ORIGINS`` → 不处理只记日志（humanize=skip）。配置关 → 同样只记日志。
    绝不抛：异常一律原文放行。
    """
    if text is None:
        return None
    src = str(text)
    org = str(origin or "auto").lower()
    try:
        cfg = resolve_cfg(cfg_root)
        lg = _norm_lang(lang, src)
        if org in BYPASS_ORIGINS or not cfg.get("enabled", True):
            logger.info(
                "[outbound] conv=%s stage=%s origin=%s lang=%s len=%d humanize=skip fp=%s preview=%r",
                conversation_id or "-", stage or "-", org, lg, len(src),
                text_fingerprint(src), src[:40])
            return src
        pre, svc_act, svc_n = _service_tone_pass(src, cfg)
        if svc_act != "clean":
            logger.info(
                "[persona-guard] service_tone=%d action=%s conv=%s stage=%s",
                svc_n, svc_act, conversation_id or "-", stage or "-")
        out, st = humanize(pre, lg, cfg=cfg, mode=mode)
        logger.info(
            "[outbound] conv=%s stage=%s origin=%s lang=%s len=%d punct_fix=%d style_fix=%d "
            "trimmed=%d dash=%d semi=%d summary=%d tag_q=%d svc_tone=%d fp=%s preview=%r",
            conversation_id or "-", stage or "-", org, lg, len(out),
            st["punct_fix"], st["style_fix"], st["trimmed"], st["dash"], st["semicolon"],
            st["summary"], st["tag_q"], svc_n if svc_act != "clean" else 0,
            text_fingerprint(out), out[:40])
        # P-1 A（#259）：起草层已净化 → 发送门这里正常应 punct_fix=0；>0 说明有绕过起草层
        # 的出站路径（协议直发 / 未挂 apply_draft_humanize 的生成口），告警一行便于补挂点。
        _fp_stats(record_gate=st["punct_fix"])
        if st["punct_fix"] > 0:
            logger.warning(
                "[outbound-leak] conv=%s stage=%s origin=%s punct_fix=%d dash=%d "
                "(a path bypassed draft-stage humanize; send gate caught it)",
                conversation_id or "-", stage or "-", org, st["punct_fix"], st["dash"])
        return out
    except Exception:
        logger.debug("[outbound_humanize] apply 异常（原文放行）", exc_info=True)
        return src


def _fp_stats(*, record_gate: Optional[int] = None, record_draft: Optional[int] = None,
              claim_action: str = "", svc_action: Optional[str] = None) -> None:
    """「AI 指纹」计数（ai_fingerprint_stats）——best-effort，任何失败静默。"""
    try:
        from src.inbox import ai_fingerprint_stats as _fp
        if record_gate is not None:
            _fp.record_gate(int(record_gate))
        if record_draft is not None:
            _fp.record_draft(int(record_draft))
        if claim_action:
            _fp.record_claim(claim_action)
        if svc_action is not None:
            _fp.record_service_tone(svc_action)
    except Exception:
        pass


# ── 起草层挂点（P-1 A · #259 #254）──────────────────────────────────────────

def apply_draft_humanize(
    text: Optional[str], *, conversation_id: str = "", lang: str = "", origin: str = "auto",
    stage: str = "draft", draft_id: str = "", history_texts: Optional[List[Any]] = None,
    memory_facts: Optional[List[Any]] = None, cfg_root: Optional[Dict[str, Any]] = None,
    mode: str = "default",
) -> Tuple[Optional[str], Dict[str, Any]]:
    """AI 稿**落库之前**的净化：先引用锚点守卫（``claim_guard.check_claims``，去谎）再
    :func:`humanize`（去标点 / 句式），使「草稿 = 将发文本」（工作台 / L1 审核稿 / 关怀预览
    看到的就是要发出去的）。发送门 :func:`apply_outbound_humanize` 保留兜底。

    返回 ``(text, meta)``；``meta``: ``punct_fix / style_fix / trimmed / dash / claim_action /
    claim_phrases / claim_anchor / skipped``。``origin ∈ BYPASS_ORIGINS``（verbatim / 人工）或配置关
    → 原文 + ``skipped=True``；``None`` 原样返回。每稿一行日志
    ``[draft] conv=… stage=… draft=… lang=… claim=… punct_fix=n style_fix=n``。绝不抛。
    """
    meta: Dict[str, Any] = {"punct_fix": 0, "style_fix": 0, "trimmed": 0, "dash": 0,
                            "claim_action": "clean", "claim_phrases": [], "claim_anchor": "",
                            "skipped": False}
    if text is None:
        meta["skipped"] = True
        return None, meta
    src = str(text)
    org = str(origin or "auto").lower()
    try:
        cfg = resolve_cfg(cfg_root)
        lg = _norm_lang(lang, src)
        if org in BYPASS_ORIGINS or not cfg.get("enabled", True) or not src.strip():
            meta["skipped"] = True
            logger.info(
                "[draft] conv=%s stage=%s draft=%s origin=%s lang=%s humanize=skip len=%d",
                conversation_id or "-", stage or "-", draft_id or "-", org, lg, len(src))
            return src, meta
        cur = src
        # Q-1 C（#264 #270）：出站重复提问守卫——本稿问句与 24h 内我方问句相似（词干 + 同义 +
        # 槽位标签）≥0.8 → 删句 / 整稿只剩它则改非问句；挂在 claim_guard 之前（先去重问、再去谎）。
        # 绝不抛、原文放行；meta.repeat_q / repeat_q_stripped。
        try:
            from src.inbox.repeat_question_guard import check_repeat_questions
            cur, rrep = check_repeat_questions(cur, conversation_id=conversation_id, lang=lg)
            meta["repeat_q"] = str(rrep.get("action") or "clean")
            meta["repeat_q_stripped"] = int(rrep.get("stripped") or 0)
        except Exception:
            logger.debug("[draft] repeat_question_guard 异常（原文继续）", exc_info=True)
        # Q-1 D（#264 #269）：识破守卫——最近入站命中「you're a bot / 20th time / 你又问」→ 本稿
        # 只出一句轻松不辩解的挽回；任何时候删自证句（I'm not a robot / 我是真人）。meta.exposure。
        try:
            from src.inbox.exposure_guard import guard_outbound
            cur, xrep = guard_outbound(cur, conversation_id=conversation_id, lang=lg)
            meta["exposure"] = str(xrep.get("action") or "clean")
        except Exception:
            logger.debug("[draft] exposure_guard 异常（原文继续）", exc_info=True)
        try:
            from src.inbox.claim_guard import check_claims, log_report
            cur, crep = check_claims(cur, history_texts=history_texts, memory_facts=memory_facts,
                                     lang=lg, seed=conversation_id or draft_id)
            meta["claim_action"] = str(crep.get("action") or "clean")
            meta["claim_phrases"] = list(crep.get("phrases") or [])[:4]
            meta["claim_anchor"] = str(crep.get("anchor") or "")
            log_report(crep, conversation_id=conversation_id, stage=stage)
            if crep.get("phrases"):
                _fp_stats(claim_action=meta["claim_action"])
        except Exception:
            logger.debug("[draft] claim_guard 异常（原文继续）", exc_info=True)
        # Q-2 D（#263）：commitment_claim / self_blame_repromise；与 P-3 media_claim 并表 CLAIM_KINDS。
        # stats 由 apply_claim_rewrites 按 hits 记 commitment_claim / self_blame_repromise / media_claim。
        try:
            from src.inbox.commitment_guard import apply_claim_rewrites
            cur, crep2 = apply_claim_rewrites(cur, lang=lg)
            _hits = list(crep2.get("hits") or [])
            meta["commitment"] = "|".join(_hits) or "clean"
        except Exception:
            logger.debug("[draft] commitment_guard 出站拦截异常（原文继续）", exc_info=True)
        # Q-8 D（#264 #263）：退场闸 exit_claim——AI 自己 "gotta go / talk later / 我先忙了" 退场：
        # 班表到点 / 客户先告别 / 留白策略三者之外一律改写成问句 / 话头；合法退场留钩子 + 关怀排后续。
        # meta.exit = clean|rewrite|allow；stats exit_claim；日志 [exit] conv= allowed= reason=。
        try:
            from src.inbox.exit_gate import guard_exit
            cur, xg = guard_exit(cur, conversation_id=conversation_id, lang=lg, cfg_root=cfg_root)
            meta["exit"] = str(xg.get("action") or "clean")
            if xg.get("reason"):
                meta["exit_reason"] = str(xg.get("reason"))
        except Exception:
            logger.debug("[draft] exit_gate 异常（原文继续）", exc_info=True)
        out, st = humanize(cur, lg, cfg=cfg, mode=mode)
        meta.update({"punct_fix": int(st["punct_fix"]), "style_fix": int(st["style_fix"]),
                     "trimmed": int(st["trimmed"]), "dash": int(st["dash"])})
        _fp_stats(record_draft=st["punct_fix"])
        logger.info(
            "[draft] conv=%s stage=%s draft=%s origin=%s lang=%s len=%d claim=%s punct_fix=%d "
            "style_fix=%d trimmed=%d dash=%d fp=%s preview=%r",
            conversation_id or "-", stage or "-", draft_id or "-", org, lg, len(out),
            meta["claim_action"], st["punct_fix"], st["style_fix"], st["trimmed"], st["dash"],
            text_fingerprint(out), out[:40])
        return out, meta
    except Exception:
        logger.debug("[draft] apply_draft_humanize 异常（原文放行）", exc_info=True)
        return src, meta


__all__ = [
    "DEFAULT_MAX_SENTENCES", "BYPASS_ORIGINS", "resolve_cfg", "humanize",
    "apply_outbound_humanize", "apply_draft_humanize", "resolve_origin",
    "deferred_verbatim_pending", "text_fingerprint",
]
