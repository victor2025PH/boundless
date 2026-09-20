"""记忆抽取接地护栏 — 事实必须锚定在用户原话上（防幻觉自我强化）。

真实事故（2026-07-13，telegram 5433982810）：LLM 记忆抽取器把 **AI 自己回复里的
臆测**存成了"用户事实"——AI 问「你明天是不是不用上班啊？」→ 抽出「用户明天不用
上班」；AI 说「你刚才说想去大阪玩」（本身是幻觉）→ 抽出「用户想去大阪玩」。
假记忆下轮注入 prompt → AI 更确信 → 复读幻觉 → 用户："你精神错乱了吗"。

防线（宁可漏记不可错记——漏一条记忆无感，错一条记忆是"精神错乱"事故）：
  - 抽出的每条事实必须与**用户消息**有内容级词汇重叠（CJK bigram / 拉丁词 / 数字）；
  - 只在助手回复里出现、用户从没说过的内容 → 丢弃；
  - 意译到零词汇重叠的极端 legit 案例会被误丢（可接受的保守代价，prompt 侧同时
    硬化要求 LLM 引用用户原词）。

J-10 A1（2026-09-05，#183 跨语言接地 bug）：上面的词汇重叠判据把「事实文本」和
「用户原话」直接比——抽取器把英文客户的话抽成中文事实 → 零重叠 → 全丢。skuio 机
88MP86 实锤：``客户结过一次婚`` / ``客户有4个兄弟姐妹`` 等 BABY BEAR 英文亲口说的
事实整晚被丢，外语客户基本零记忆。修法＝**引文级接地**（:func:`ground_fact_with_evidence`）：

  1. 抽取器每条事实附 ``evidence``＝客户原话逐字引文（原语言）。无引文 → 丢
     （``no_evidence``，沿用宁可漏记）。
  2. ``evidence`` 必须是 ``user_msg`` 的子串（归一化空白/大小写/标点后）或 token
     重叠 ≥60% → 否则丢（``evidence_mismatch``，防 LLM 编引文 / 从助手回复里摘）。
  3. **安全语义不弱化**——引文只证明「客户说过这句话」，不证明「事实是这句话的
     结论」，所以：
     - 事实与引文同语种（中文事实 ↔ 中文引文、英文 ↔ 英文）时，**照旧**跑旧词汇
       重叠判据（事实 ↔ 用户原话），Phase8 事故语料（用户「好呀好呀」+ 事实
       「用户想去大阪玩」+ 引文「好呀好呀」）仍必丢（``fact_unanchored``）；
     - 跨语种时无法做词汇比对，改用两条语言无关的钉子：事实里的**语言无关 token**
       （拉丁词 / ≥2 位数字：名字、地名、年龄、品牌）必须全部出现在用户原话里
       （「客户自称Tom」↔「I'm Bob」丢）；没有这类 token 的事实要求引文**有内容**
       （≥3 字且不全是 ok/yes/好呀 一类填充词——防「客户说了个 ok，AI 臆测一句
       就能配上引文」）。
  4. 判定器自身异常 → 回退旧判据（绝不比旧防线更松，也绝不阻断记忆链）。

旧 API :func:`fact_grounded_in_user_msg` / :func:`filter_grounded_facts` 原样保留
（启发式抽取、旧测试、其他调用方不受影响）。纯函数、零依赖、可单测。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 事实开头的主语样板（剥掉后再取内容词，防止「用户」二字本身参与匹配）
_FACT_PREFIX_RE = re.compile(r"^(用户|对方|客户|他|她|TA)[:：\s]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_NUM_RE = re.compile(r"[A-Za-z][A-Za-z']{1,}|\d{2,}")


def _content_tokens(text: str) -> Tuple[set, set]:
    """文本 → (CJK bigram 集合, 拉丁词/数字 token 集合)。

    bigram 只在连续 CJK 段内生成（不跨标点/空格）；拉丁词统一小写、≥2 字符；
    数字 ≥2 位（单字符/单位数噪声大）。
    """
    bigrams: set = set()
    for run in _CJK_RUN_RE.findall(str(text or "")):
        for i in range(len(run) - 1):
            bigrams.add(run[i:i + 2])
    latin = {t.lower() for t in _LATIN_NUM_RE.findall(str(text or ""))}
    return bigrams, latin


def fact_grounded_in_user_msg(fact: str, user_msg: str) -> bool:
    """一条抽取事实是否锚定在用户原话上（任一内容 token 重叠即接地）。

    - fact 剥掉「用户/对方…」主语前缀后取内容 token；
    - 与 user_msg 的 token 集合有交集（CJK bigram 或 拉丁/数字 token）→ True；
    - fact 本身取不出内容 token（超短/纯符号）→ 保守放行 True（无从判断）。
    """
    f = _FACT_PREFIX_RE.sub("", str(fact or "").strip())
    f_bi, f_latin = _content_tokens(f)
    if not f_bi and not f_latin:
        return True
    u_bi, u_latin = _content_tokens(user_msg)
    if f_bi & u_bi:
        return True
    if f_latin & u_latin:
        return True
    return False


def filter_grounded_facts(
    facts: List[str], user_msg: str,
) -> Tuple[List[str], List[str]]:
    """批量过滤：返回 (接地保留的, 被丢弃的)。防御式，绝不抛。"""
    kept: List[str] = []
    dropped: List[str] = []
    for f in facts or []:
        try:
            (kept if fact_grounded_in_user_msg(f, user_msg) else dropped).append(f)
        except Exception:
            kept.append(f)  # 判定器自身异常 → 放行（回到旧行为）
    return kept, dropped


# ── J-10 A1：引文级接地（语言无关）───────────────────────────────────────────

DROP_NO_EVIDENCE = "no_evidence"
DROP_EVIDENCE_MISMATCH = "evidence_mismatch"
DROP_FACT_UNANCHORED = "fact_unanchored"
DROP_REASONS: Tuple[str, ...] = (
    DROP_NO_EVIDENCE, DROP_EVIDENCE_MISMATCH, DROP_FACT_UNANCHORED,
)

# 引文 token 与用户原话 token 的最低重叠比（引文侧口径：|E∩U| / |E|）
EVIDENCE_TOKEN_OVERLAP = 0.6
# 跨语种、事实无语言无关 token 时，引文归一后至少要有这么多字符才算「有内容」
_EVIDENCE_MIN_CHARS = 3

_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
# 无空格书写的音节文字（泰/假名/谚文）：整段当一个词会变成全有全无，改切 bigram
_UNSPACED_SCRIPT_RE = re.compile(r"[\u0e00-\u0e7f\u3040-\u30ff\uac00-\ud7af]")
# Unicode 字母 run（\w 去掉数字/下划线；含带音标拉丁、西里尔、阿拉伯、泰、假名、谚文、汉字）
_WORD_RUN_RE = re.compile(r"[^\W\d_]+")
_NUM_RE = re.compile(r"\d{2,}")

# 填充词（casefold 后整词）：这类引文不足以支撑任何具体事实。只在**跨语种且事实
# 无语言无关 token** 这条最窄的路上用；同语种走旧词汇判据，不查表。
_FILLER_TOKENS = frozenset({
    # en
    "ok", "okay", "kk", "yes", "yeah", "yep", "yup", "ya", "no", "nope", "nah",
    "sure", "fine", "good", "great", "nice", "cool", "wow", "oh", "ohh", "hmm",
    "hm", "haha", "hahaha", "hehe", "lol", "lmao", "thanks", "thank", "you", "thx",
    "ty", "hi", "hello", "hey", "bye", "morning", "night", "really", "right",
    "alright", "please", "pls", "the", "and", "but", "so", "do", "did", "it",
    "is", "am", "are", "was", "were", "me", "my", "i", "u", "yo", "ah", "eh",
    # tl（菲律宾市场）
    "oo", "opo", "hindi", "sige", "salamat", "po", "ha", "ay", "ano",
    # zh（引文为中文时一般走同语种旧判据，这里只兜底混排）
    "好", "好的", "好呀", "好啊", "嗯", "嗯嗯", "哦", "噢", "是", "是的", "对",
    "对的", "行", "可以", "哈哈", "哈哈哈", "谢谢", "你好", "在吗", "早", "晚安",
    # ja / ko 常见应答
    "はい", "うん", "ええ", "いいえ", "そう", "そうです", "ありがとう", "おはよう",
    "네", "응", "예", "아니", "아니요", "그래", "감사합니다", "안녕",
})


def normalize_for_match(text: Any) -> str:
    """引文/原话 → 可做子串比对的归一形态。

    NFKC（全角→半角、兼容字符折叠）→ casefold → 只保留字母/数字/组合标记
    （空白、标点、符号、emoji 全部剥掉）。「I'm Tom!!」与「I’m tom」归一后相等，
    LLM 抄引文时常见的引号/省略号/大小写差异不再算「不符」。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(
        ch for ch in s.casefold() if unicodedata.category(ch)[0] in ("L", "N", "M")
    )


def evidence_tokens(text: Any) -> set:
    """语言无关的内容 token 集合：汉字 bigram ∪ 非汉字词（casefold，≥2 字符；
    泰/假名/谚文 run ≥4 字符时切 bigram）∪ ≥2 位数字。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    toks: set = set()
    for run in _CJK_RUN_RE.findall(s):
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    for run in _WORD_RUN_RE.findall(s):
        # \w 把汉字也算字母：混排 run（「我有个daughter」）先按汉字切开
        parts = _HAN_RE.sub(" ", run).split() if _HAN_RE.search(run) else [run]
        for p in parts:
            p = p.casefold()
            if len(p) < 2:
                continue
            if len(p) >= 4 and _UNSPACED_SCRIPT_RE.search(p):
                for i in range(len(p) - 1):
                    toks.add(p[i:i + 2])
            else:
                toks.add(p)
    toks.update(_NUM_RE.findall(s))
    return toks


def evidence_matches_user_msg(
    evidence: Any, user_msg: Any, *, min_overlap: float = EVIDENCE_TOKEN_OVERLAP,
) -> bool:
    """引文是否真出自用户原话：归一化后是子串，或 token 重叠 ≥ ``min_overlap``。

    空引文 / 空原话 → False。语言无关（子串比对不依赖分词）。
    """
    ne = normalize_for_match(evidence)
    nu = normalize_for_match(user_msg)
    if not ne or not nu:
        return False
    if ne in nu:
        return True
    et = evidence_tokens(evidence)
    if not et:
        return False
    ut = evidence_tokens(user_msg)
    return (len(et & ut) / float(len(et))) >= float(min_overlap)


def _script_of(text: str, *, is_fact: bool) -> str:
    """粗粒度语种：zh（汉字）/ ja（含假名）/ ko（含谚文）/ latin（ASCII 字母）/ other。

    事实以汉字为准（抽取器规定中文短句，夹带的拉丁名字不改变语种）；引文含假名
    即 ja（日文夹汉字仍是日文）。
    """
    t = str(text or "")
    if is_fact and _HAN_RE.search(t):
        return "zh"
    if _KANA_RE.search(t):
        return "ja"
    if _HANGUL_RE.search(t):
        return "ko"
    if _HAN_RE.search(t):
        return "zh"
    if _ASCII_LETTER_RE.search(t):
        return "latin"
    return "other"


def _invariant_tokens(fact_body: str) -> set:
    """事实里的语言无关 token：ASCII 拉丁词（≥2 字符，casefold）+ ≥2 位数字。"""
    s = unicodedata.normalize("NFKC", str(fact_body or ""))
    toks = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z']+", s)}
    toks.update(_NUM_RE.findall(s))
    return toks


def _evidence_substantive(evidence: str) -> bool:
    """引文是否「有内容」：归一后 ≥3 字符，且 token 不全是填充词。"""
    if len(normalize_for_match(evidence)) < _EVIDENCE_MIN_CHARS:
        return False
    toks = evidence_tokens(evidence)
    if not toks:
        # 无法切 token 的文字（极短泰文等）：长度已过线，放行
        return True
    return any(t not in _FILLER_TOKENS for t in toks)


def ground_fact_with_evidence(
    fact: Any, evidence: Any, user_msg: Any,
) -> Tuple[bool, str]:
    """引文级接地判定 → ``(通过, 丢弃原因)``；通过时原因为空串。

    见模块 docstring 第 1–4 条。任何内部异常 → 回退旧判据
    :func:`fact_grounded_in_user_msg`（原因 ``fact_unanchored``）。
    """
    f_raw = str(fact or "").strip()
    ev = str(evidence or "").strip()
    um = str(user_msg or "")
    if not ev:
        return False, DROP_NO_EVIDENCE
    try:
        if not evidence_matches_user_msg(ev, um):
            return False, DROP_EVIDENCE_MISMATCH
        body = _FACT_PREFIX_RE.sub("", f_raw)
        f_lang = _script_of(body, is_fact=True)
        e_lang = _script_of(ev, is_fact=False)
        if f_lang != "other" and f_lang == e_lang:
            # 同语种：旧词汇判据原样保留（Phase8 事故语料仍必丢）
            if not fact_grounded_in_user_msg(f_raw, um):
                return False, DROP_FACT_UNANCHORED
            return True, ""
        # 跨语种（或事实/引文取不出语种）：语言无关钉子
        inv = _invariant_tokens(body)
        if inv:
            if not inv <= evidence_tokens(um):
                return False, DROP_FACT_UNANCHORED
            return True, ""
        if not _evidence_substantive(ev):
            return False, DROP_FACT_UNANCHORED
        return True, ""
    except Exception:
        ok = fact_grounded_in_user_msg(f_raw, um)
        return (True, "") if ok else (False, DROP_FACT_UNANCHORED)


def ground_fact_items(
    items: Iterable[Dict[str, Any]], user_msg: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """批量引文级过滤 → ``(kept, dropped)``。

    每项 ``{"text": 事实, "evidence": 引文}``（兼容 ``fact`` / ``quote`` 键名）；
    ``dropped`` 每项多一个 ``reason``（见 :data:`DROP_REASONS`）。防御式，绝不抛；
    单项判定异常时按旧判据兜底（与 :func:`ground_fact_with_evidence` 同）。
    """
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for it in items or []:
        try:
            if isinstance(it, dict):
                text = str(it.get("text") or it.get("fact") or "").strip()
                ev = str(it.get("evidence") or it.get("quote") or "").strip()
            else:
                text, ev = str(it or "").strip(), ""
            if not text:
                continue
            ok, reason = ground_fact_with_evidence(text, ev, user_msg)
        except Exception:
            text = str(it or "").strip()
            ev = ""
            ok = fact_grounded_in_user_msg(text, str(user_msg or ""))
            reason = "" if ok else DROP_FACT_UNANCHORED
        rec = {"text": text, "evidence": ev}
        if ok:
            kept.append(rec)
        else:
            rec["reason"] = reason
            dropped.append(rec)
    return kept, dropped


def summarize_drop_reasons(dropped: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """``dropped`` → ``{reason: count}``（未知原因归 ``fact_unanchored``）。"""
    out: Dict[str, int] = {}
    for d in dropped or []:
        r = str((d or {}).get("reason") or "") if isinstance(d, dict) else ""
        if r not in DROP_REASONS:
            r = DROP_FACT_UNANCHORED
        out[r] = out.get(r, 0) + 1
    return out


__all__ = [
    "fact_grounded_in_user_msg", "filter_grounded_facts",
    "DROP_NO_EVIDENCE", "DROP_EVIDENCE_MISMATCH", "DROP_FACT_UNANCHORED",
    "DROP_REASONS", "EVIDENCE_TOKEN_OVERLAP",
    "normalize_for_match", "evidence_tokens", "evidence_matches_user_msg",
    "ground_fact_with_evidence", "ground_fact_items", "summarize_drop_reasons",
]
