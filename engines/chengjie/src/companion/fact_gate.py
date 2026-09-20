# -*- coding: utf-8 -*-
"""事实门（FactGate，Q-25 #295 #300 #272追加 Y39D8U）——画像 / 记忆 / 目标回填**同一道门**。

0911 Y39D8U：AI 自己问「inspection work 做多久」，客户只答「couple months」，画像 occupation 被
写成「AI work」（来源=AI 推断）。三条写链（画像 ``profile_fill.apply`` / 目标引擎 LLM 回填
``profile_llm.run_llm_capture`` / 记忆 ``episodic.add_fact``）各自一把尺子：画像有 Q-19 三闸，
目标回填只有词汇重叠接地（``work`` 撞 ``inspection work`` 即过），记忆只核「引文出自客户」。
本模块把 Q-19 的闸抽成共享件并升级，三链都从这里过；拿不准一律「丢 + 日志」。

:func:`check` 判据（顺序即短路顺序，返回 ``(ok, reason)``）：

1. **类型尺子**（``slot_or_kind`` 是注册槽位时）：复用 Q-19 ``profile_slots.slot_validate``
   （age 16–99 / 短语 ≤24 字禁整句 / 枚举闭集）→ ``invalid:<why>``。
1b. **人设名硬闸**（仅 ``slot=name``，Q-38 #272）：值 ∈ ``reserved_self_names`` → ``own_name``；
   evidence 只是呼格（hi/hey/hello/你好/嗨/哈喽 X，或整句就是 X）且 X ∈ reserved → ``vocative``。
   放在类型尺子之后、锚定之前；``reserved`` 空则跳过（缺人设不误伤）。非 name 槽零新判据。
2. **来源守卫**：``inbound_texts`` 只认客户入站——传 ``{direction, text}`` 行时剔掉
   ``direction`` 不是 ``in / inbound`` 的；出站 / 译文 / 关怀稿永不成为锚定语料。
3. **原句锚定**：``evidence`` 必填（无 → ``no_evidence`` 记忆 / ``unanchored`` 画像），且
   归一（空白 / 标点 / 大小写）后须是**同一条**客户入站的子串——禁跨句拼接。
4. **值核心 token 全在 evidence 里**（禁部分匹配）：拉丁词 / 数字按整词（允许 s/es/ed/ing/y
   词尾），汉字 / 假名 / 谚文按连续段子串；``AI work`` × ``inspection work`` 因 ``ai`` 缺席
   而丢，``work`` × ``network`` 因非整词而丢。枚举槽改验 evidence 含该标签触发词
   （``enum_evidence_ok``）。→ ``unanchored``。
5. **语种守卫**（画像槽）：值语种 ≠ 客户主语种（``customer_lang`` 显式给或按入站投票）→
   ``lang_mismatch``。记忆事实按约定写中文短句，不做语种比对。
6. **主体守卫**（``kind=fact``）：事实主体只能是客户或客户明确提到的人——事实里出现
   「一起 / 我们 / 咱们 / together / we / us / with me」等合体主语而客户原句没有 → ``subject``
   （「我出差去越南」绝不能变「一起去越南」）；事实里的语言无关 token（拉丁词 / ≥2 位数字：
   名字、地名、年龄）必须全部出现在 evidence 里。

纯函数、零 I/O；内部异常 → ``(False, "gate_error")``（门自己坏了也不放行）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, List, Optional, Sequence, Tuple

KIND_FACT = "fact"
#: Q-29（#305）：瞬态事实——客户「我这边现在是晚上 / 四点了」这类**只在一段时间内成立**的叙述。
#: 判据 = 原句锚定（evidence 必填且逐字在同一条入站）+ 值锚定在 evidence 里；不做类型尺子 / 语种 /
#: 主体守卫；调用方（``companion.peer_time``）附 ``ttl_sec`` 落 KV，过期即视为不存在。
KIND_TRANSIENT = "transient"
TRANSIENT_TTL_SEC = 12 * 3600
#: Q-36（#313 2JK95C / #318 VV7BRY）：**观察**事实——入站图 / 视频的识图描述（caption）。画面所见 ≠ 客户自述：
#: 「几个菜」不能变「一桌菜」，「水边的房子 / 码头」不能变成客户住处或专名「Marina」。判据 = evidence（caption）
#: 必填且逐字在同一条入站里 + 值锚定在 caption 里；不做类型尺子 / 语种 / 主体守卫；调用方
#: （``inbox.image_observation``）附 ``ttl_sec`` 落 KV，过期即视为不存在；注入措辞固定「TA 发过一张…的照片」。
KIND_OBSERVATION = "observation"
OBSERVATION_TTL_SEC = 24 * 3600

REASON_EMPTY = "empty"
REASON_NO_EVIDENCE = "no_evidence"
REASON_UNANCHORED = "unanchored"
REASON_LANG = "lang_mismatch"
REASON_SUBJECT = "subject"
REASON_GATE_ERROR = "gate_error"
#: Q-38（#272 HPS7C3 / E2GXEP）：画像 name 槽写了人设自称 / 呼格
REASON_OWN_NAME = "own_name"
REASON_VOCATIVE = "vocative"

# hi / 你好 X —— 只认「整句就是招呼 + 名字」，带自称从句（Hi X, I'm Y）不算
_VOCATIVE_LEAD_RE = re.compile(
    r"^(?:hi+|hey+|hello+|hola+|yo+|howdy+|你好呀?|嗨+|哈[喽囉啰]|您好)\s*[,:，：]?\s*",
    re.IGNORECASE)
_VOCATIVE_CLAUSE_RE = re.compile(
    r"[,，;；]|i['’]?m\b|i am\b|my name\b|call me\b|我叫|我是|叫我",
    re.IGNORECASE)
_SELF_INTRO_RE = re.compile(
    r"\b(?:my|name|is|am|the|a|an)\b|我叫|我是|叫我|我的名字",
    re.IGNORECASE)

# 与 profile_fill._lit 同口径（空白 / 中英标点剥掉 + casefold）——两处必须一致，否则边界分叉
_NORM_STRIP_RE = re.compile(r"[\s，。,.!！?？、;；:：'\"“”‘’()（）]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*|\d+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+|[\u3040-\u30ff]+|[\uac00-\ud7af]+")
_FACT_PREFIX_RE = re.compile(r"^(用户|对方|客户|他|她|TA)[:：\s]*")
# 合体主语：事实把「客户一个人做的事」写成「和我方一起」——Y39D8U 同批「我出差去越南 → 一起去越南」
_JOINT_SUBJECT_RE = re.compile(
    r"一起|一同|我们|咱们|我俩|你我|和我|跟我|与我|陪我|"
    r"(?<![a-z])(?:together|we|us|our|with\s+me|both\s+of\s+us)(?![a-z])",
    re.IGNORECASE)
_INBOUND_DIRS = ("in", "inbound")


def _lit(s: Any) -> str:
    return _NORM_STRIP_RE.sub("", unicodedata.normalize("NFKC", str(s or ""))).casefold()


def inbound_only(texts: Any) -> List[str]:
    """来源守卫：``str`` 序列原样（调用方已保证是入站）；``{direction, text}`` 行只留
    ``direction ∈ {in, inbound}``（缺 direction 视为入站，兼容旧调用）。空条剔除。"""
    out: List[str] = []
    for t in list(texts or []):
        if isinstance(t, dict):
            d = str(t.get("direction") or "in").strip().lower()
            if d not in _INBOUND_DIRS:
                continue
            s = str(t.get("text") or "").strip()
        else:
            s = str(t or "").strip()
        if s:
            out.append(s)
    return out


def evidence_in_inbound(evidence: Any, inbound_texts: Any) -> bool:
    """evidence 归一后是**某一条**客户入站的子串（禁跨句拼接）。"""
    ev = _lit(evidence)
    if not ev:
        return False
    for t in inbound_only(inbound_texts):
        if ev in _lit(t):
            return True
    return False


def value_tokens(value: Any) -> Tuple[List[str], List[str]]:
    """值 → ``(拉丁词 / 数字 token（casefold）, 汉字 / 假名 / 谚文连续段)``。"""
    s = unicodedata.normalize("NFKC", str(value or ""))
    latin = [t.casefold().strip("'") for t in _LATIN_TOKEN_RE.findall(s)]
    latin = [t for t in latin if t]
    cjk = [r for r in _CJK_RUN_RE.findall(s) if r]
    return latin, cjk


def _whole_word_in(tok: str, text_low: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?:s|es|ed|ing|y)?(?![a-z0-9])",
                     text_low) is not None


def _cjk_run_anchored(run: str, ev_lit: str) -> bool:
    """汉字 / 假名 / 谚文段：整段子串直接过；否则要求**每个字**都在 evidence 里且相邻字对
    至少一半也在（「客服回不过来」← 「客服都回不过来」这类摘录省字放行；「老师」←
    「老板和师傅」这类拆字重组不放）。"""
    r = _lit(run)
    if not r:
        return True
    if r in ev_lit:
        return True
    if any(ch not in ev_lit for ch in r):
        return False
    if len(r) < 2:
        return True
    pairs = [r[i:i + 2] for i in range(len(r) - 1)]
    hit = sum(1 for p in pairs if p in ev_lit)
    return hit * 2 >= len(pairs)


def value_anchored_in_evidence(value: Any, evidence: Any) -> bool:
    """值的**每个**核心 token 都在 evidence 里（禁部分匹配）：拉丁词 / 数字整词；
    汉字 / 假名 / 谚文段见 :func:`_cjk_run_anchored`。"""
    latin, cjk = value_tokens(value)
    if not latin and not cjk:
        # 取不出 token（纯符号 / 表情）→ 退回归一子串
        v = _lit(value)
        return bool(v) and v in _lit(evidence)
    ev_low = unicodedata.normalize("NFKC", str(evidence or "")).casefold()
    for tok in latin:
        if not _whole_word_in(tok, ev_low):
            return False
    ev_lit = _lit(evidence)
    for run in cjk:
        if not _cjk_run_anchored(run, ev_lit):
            return False
    return True


def _is_registered_slot(slot: str) -> bool:
    try:
        from src.companion.goals.profile_slots import get_slot
        return get_slot(slot) is not None
    except Exception:
        return False


def _validate_slot(slot: str, value: str) -> Tuple[str, str]:
    """→ (规范值, 原因)；校验器缺席 / 异常 → 值原样、原因空。"""
    try:
        from src.companion.goals.profile_slots import slot_validate
        nv, why = slot_validate(slot, value)
        return (str(nv or "") or value), str(why or "")
    except Exception:
        return value, ""


def _enum_anchor_ok(slot: str, value: str, evidence: str) -> Optional[bool]:
    """枚举槽 → True/False；非枚举槽 → None（走 token 规则）。"""
    try:
        from src.companion.goals.profile_slots import enum_evidence_ok, is_enum_slot
        if is_enum_slot(slot):
            return bool(enum_evidence_ok(slot, value, evidence))
    except Exception:
        return None
    return None


def _lang_reason(value: str, inbound: Sequence[str], evidence: str, customer_lang: str) -> str:
    try:
        from src.companion.goals.profile_fill import lang_mismatch_reason, text_lang
    except Exception:
        return ""
    cl = str(customer_lang or "").strip().lower()
    if cl and cl != "neutral":
        vl = text_lang(value)
        if vl == "neutral" or vl == cl or (vl == "zh" and cl == "ja"):
            return ""
        el = text_lang(evidence) if evidence else "neutral"
        if el != vl and (el == "neutral" or el == cl):
            return ""
        # 显式语种不合，再看客户入站里是否本就夹用该语种（港式中英夹杂）
        return lang_mismatch_reason(value, inbound, evidence) if inbound else REASON_LANG
    return lang_mismatch_reason(value, inbound, evidence)


def _name_norm(s: Any) -> str:
    try:
        from src.utils.persona_guard import _reserved_norm
        return _reserved_norm(s)
    except Exception:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or ""))).casefold()


def _reserved_bag(reserved: Any) -> set:
    bag: set = set()
    for x in (reserved or []):
        nv = _name_norm(x)
        if nv:
            bag.add(nv)
    return bag


def name_in_reserved(value: Any, reserved: Any) -> bool:
    nv = _name_norm(value)
    return bool(nv) and nv in _reserved_bag(reserved)


def vocative_target(evidence: Any) -> str:
    """evidence 只是呼格时返回被称呼的 X，否则空串。

    「Hi Mizuki」/「hey Alicia」/「你好小语」/ 整句「Mizuki」→ X；
    「Hi Mizuki, I'm Jeo」/「My name is Jeo」→ 空（不是只是呼格）。"""
    s = re.sub(r"\s+", " ", str(evidence or "")).strip()
    s = re.sub(r"[!！.。~～…]+$", "", s).strip()
    if not s:
        return ""
    m = _VOCATIVE_LEAD_RE.match(s)
    if m:
        rest = s[m.end():].strip()
        rest = re.sub(r"[!！.。~～…]+$", "", rest).strip()
        rest = re.sub(r"[\s\W]+$", "", rest, flags=re.UNICODE).strip() if rest else rest
        if not rest or len(rest) > 24 or _VOCATIVE_CLAUSE_RE.search(rest):
            return ""
        return rest
    if len(s) <= 24 and not re.search(r"[,，.。!！?？;；:：]", s):
        if _SELF_INTRO_RE.search(s):
            return ""
        if len(s.split()) <= 3:
            return s
    return ""


def name_reserved_reason(value: Any, reserved: Any, evidence: Any = "") -> str:
    """画像 ``name`` 槽人设名硬闸 → ``own_name`` / ``vocative`` / 空。reserved 空不判。"""
    bag = _reserved_bag(reserved)
    if not bag:
        return ""
    if name_in_reserved(value, bag):
        return REASON_OWN_NAME
    xt = vocative_target(evidence)
    if xt and name_in_reserved(xt, bag):
        return REASON_VOCATIVE
    return ""


def subject_reason(fact: Any, evidence: Any) -> str:
    """kind=fact 主体守卫 → ``""`` / ``"subject"``。"""
    body = _FACT_PREFIX_RE.sub("", str(fact or "").strip())
    if _JOINT_SUBJECT_RE.search(body) and not _JOINT_SUBJECT_RE.search(str(evidence or "")):
        return REASON_SUBJECT
    return ""


def _invariant_tokens(text: str) -> set:
    s = unicodedata.normalize("NFKC", str(text or ""))
    toks = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z']+", s)}
    toks.update(re.findall(r"\d{2,}", s))
    return toks


def check(value: Any, *, slot_or_kind: str, evidence: Any, inbound_texts: Any,
          customer_lang: str = "", raw_value: Any = None,
          reserved_self_names: Any = None) -> Tuple[bool, str]:
    """事实门总入口 → ``(通过, 原因)``；通过时原因为空串。见模块 docstring。

    ``slot_or_kind``：注册槽位键（画像 / 目标回填）或 :data:`KIND_FACT`（记忆事实）。
    ``inbound_texts``：最近 ≤30 条客户入站（``str`` 或 ``{direction, text}`` 行）。
    ``raw_value``：校验前的原值（``slot_validate`` 可能把 ``32岁`` 规范成 ``32``；两者任一
    能锚定即算锚定）。
    ``reserved_self_names``：Q-38 人设自称 ∪ ``peer_calls_you``（仅 ``name`` 槽；默认空
    = 不判，Q-19 / Q-25 既有调用零行为变化）。"""
    try:
        kind = str(slot_or_kind or "").strip().lower()
        val = re.sub(r"\s+", " ", str(value or "")).strip()
        if not val:
            return False, REASON_EMPTY
        ev = re.sub(r"\s+", " ", str(evidence or "")).strip()
        inbound = inbound_only(inbound_texts)
        if kind == KIND_FACT:
            if not ev:
                return False, REASON_NO_EVIDENCE
            if not evidence_in_inbound(ev, inbound):
                return False, REASON_UNANCHORED
            why = subject_reason(val, ev)
            if why:
                return False, why
            inv = _invariant_tokens(_FACT_PREFIX_RE.sub("", val))
            if inv and not inv <= _invariant_tokens(ev):
                return False, REASON_UNANCHORED
            return True, ""
        if kind in (KIND_TRANSIENT, KIND_OBSERVATION):
            # Q-29：瞬态叙述——只要原句锚定 + 值在原句里；TTL 由调用方带。
            # Q-36：观察（识图 caption）同判据——caption 本身就在入站行里（「[图片内容] …」），值须锚在 caption 里；
            # 与 fact 的差别只在「它是画面所见，不是客户说的」，这层语义由调用方按 kind 决定怎么措辞 / 怎么存。
            if not ev:
                return False, REASON_NO_EVIDENCE
            if not evidence_in_inbound(ev, inbound):
                return False, REASON_UNANCHORED
            if not value_anchored_in_evidence(val, ev):
                return False, REASON_UNANCHORED
            return True, ""
        # ── 画像槽 / 目标回填 ──
        norm = val
        if _is_registered_slot(kind):
            norm, why = _validate_slot(kind, val)
            if why:
                return False, f"invalid:{why}"
        if kind == "name":
            why = name_reserved_reason(norm or val, reserved_self_names, ev)
            if why:
                return False, why
        if not ev:
            return False, REASON_UNANCHORED
        if not evidence_in_inbound(ev, inbound):
            return False, REASON_UNANCHORED
        enum_ok = _enum_anchor_ok(kind, norm, ev) if _is_registered_slot(kind) else None
        if enum_ok is None:
            cands = [norm, val] + ([str(raw_value)] if raw_value else [])
            if not any(value_anchored_in_evidence(c, ev) for c in cands if str(c or "").strip()):
                return False, REASON_UNANCHORED
        elif not enum_ok:
            return False, REASON_UNANCHORED
        lang_why = _lang_reason(str(raw_value or val), inbound, ev, customer_lang)
        if lang_why:
            return False, REASON_LANG
        return True, ""
    except Exception:
        return False, REASON_GATE_ERROR


def check_many(items: Iterable[Tuple[str, Any, Any]], *, inbound_texts: Any,
               customer_lang: str = "", reserved_self_names: Any = None
               ) -> List[Tuple[str, Any, bool, str]]:
    """批量：``[(slot_or_kind, value, evidence)]`` → ``[(slot_or_kind, value, ok, reason)]``。"""
    out: List[Tuple[str, Any, bool, str]] = []
    for k, v, e in items or []:
        ok, why = check(v, slot_or_kind=k, evidence=e, inbound_texts=inbound_texts,
                        customer_lang=customer_lang, reserved_self_names=reserved_self_names)
        out.append((k, v, ok, why))
    return out


__all__ = [
    "KIND_FACT", "KIND_TRANSIENT", "TRANSIENT_TTL_SEC", "KIND_OBSERVATION", "OBSERVATION_TTL_SEC",
    "REASON_EMPTY", "REASON_NO_EVIDENCE", "REASON_UNANCHORED", "REASON_LANG",
    "REASON_SUBJECT", "REASON_GATE_ERROR", "REASON_OWN_NAME", "REASON_VOCATIVE",
    "check", "check_many", "evidence_in_inbound", "inbound_only", "subject_reason",
    "name_reserved_reason", "vocative_target", "name_in_reserved",
    "value_anchored_in_evidence", "value_tokens",
]
