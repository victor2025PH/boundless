"""B56 翻译专名/数字保真对账（2026-08-23 实施64 P1-3，`_297` 实录）。

事故：出站英文「…see you in Cebu…」正确，操作员中文译文镜像却成了
「我很想在马尼拉见到你」——地名被模型「上下文合理化」偷换。操作员靠译文
监督出稿，监督链失真比翻错更危险（人对着假译文放行了真消息）。

两道防线：
1. **提示词铁律**（translation_engines 的 AI 引擎 prompt/system 注入）：
   地名/人名/数字绝不替换，无把握保留原词；
2. **译后对账**（本模块，确定性纯函数）：源文锚点（拉丁专名 + 多位数字）
   在译文缺失/被换 → 括注原词追加在译文尾——无论哪个引擎哪种偷换法，
   读译文的人都能看到原词。已知音译词典命中（Cebu→宿务）不打扰；
   词典外的正确音译会被冗余括注一次，诚实的噪声远好于静默的偷换。

与 fabrication 守卫同族：确定性、宁多括一笔不放过偷换、绝不阻塞翻译主链。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

#: 高频地名音译词典（简/繁），命中任一变体＝译文保真，不括注。
#: 只收业务高频（东南亚为主）；词典的另一半价值＝**换名检测**（源文是 Cebu、
#: 译文却出现词典里另一个地名的音译且宿务缺席 → 高置信偷换）。
GEO_TRANSLIT: Dict[str, Tuple[str, ...]] = {
    "cebu": ("宿务", "宿霧", "宿雾"),
    "manila": ("马尼拉", "馬尼拉"),
    "davao": ("达沃", "達沃"),
    "boracay": ("长滩", "長灘"),
    "palawan": ("巴拉望",),
    "makati": ("马卡蒂", "馬卡蒂"),
    "clark": ("克拉克",),
    "bangkok": ("曼谷",),
    "pattaya": ("芭提雅", "芭堤雅"),
    "phuket": ("普吉",),
    "singapore": ("新加坡",),
    "malaysia": ("马来西亚", "馬來西亞"),
    "jakarta": ("雅加达", "雅加達"),
    "bali": ("巴厘", "峇里"),
    "tokyo": ("东京", "東京"),
    "osaka": ("大阪",),
    "seoul": ("首尔", "首爾"),
    "hanoi": ("河内", "河內"),
    "saigon": ("西贡", "西貢"),
    "phnom": ("金边", "金邊"),
    "sihanoukville": ("西哈努克", "西港"),
    "vientiane": ("万象", "萬象"),
    "yangon": ("仰光",),
    "taipei": ("台北", "臺北"),
    "dubai": ("迪拜", "杜拜"),
    "hongkong": ("香港",),
    "macau": ("澳门", "澳門"),
}

#: 首字母大写但不是专名的常见词（句首/称呼/客套），绝不当锚点。
_COMMON_CAP_WORDS = frozenset({
    "the", "and", "but", "you", "your", "yours", "yes", "yeah", "okay", "ok",
    "hi", "hello", "hey", "thanks", "thank", "please", "sorry", "bye",
    "good", "morning", "night", "evening", "afternoon", "today", "tomorrow",
    "yesterday", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
    "miss", "mister", "madam", "sir", "dear", "baby", "babe", "honey",
    "how", "what", "when", "where", "why", "who", "wait", "see", "let",
    "not", "now", "just", "really", "haha", "wow", "hmm", "come", "take",
})

_CAP_TOKEN_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_DIGIT_RUN_RE = re.compile(r"\d{2,}")
_SENT_SPLIT_RE = re.compile(r"[.!?。！？\n]+\s*")


def _norm_token(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(tok or "").lower())


def extract_anchors(source: str) -> List[str]:
    """源文里的保真锚点：句中大写拉丁专名 + 多位数字串。保守宁缺勿滥。"""
    src = str(source or "")
    if not src.strip():
        return []
    anchors: List[str] = []
    seen = set()
    # 拉丁专名：排除每个句子的首词（英文句首必大写，不构成专名证据）——
    # 但词典在册的地名（Cebu 开头也算）不受句首豁免。
    sent_heads = set()
    for sent in _SENT_SPLIT_RE.split(src):
        m = _CAP_TOKEN_RE.search(sent)
        if m and m.start() == 0:
            sent_heads.add((sent, m.group(0)))
    head_words = {w for _s, w in sent_heads}
    for m in _CAP_TOKEN_RE.finditer(src):
        tok = m.group(0)
        low = _norm_token(tok)
        if low in _COMMON_CAP_WORDS:
            continue
        if tok in head_words and low not in GEO_TRANSLIT:
            continue
        if low not in seen:
            seen.add(low)
            anchors.append(tok)
    for m in _DIGIT_RUN_RE.finditer(src):
        run = m.group(0)
        if run not in seen:
            seen.add(run)
            anchors.append(run)
    return anchors[:6]  # 一条聊天消息的锚点不该超过这个数，防长文刷屏


def anchor_missing(anchor: str, translated: str) -> bool:
    """锚点是否在译文缺失：原词在场（不区分大小写）或已知音译在场＝保真。"""
    out = str(translated or "")
    if not out:
        return True
    a = str(anchor or "").strip()
    if not a:
        return False
    if a.lower() in out.lower():
        return False
    variants = GEO_TRANSLIT.get(_norm_token(a), ())
    return not any(v in out for v in variants)


def fidelity_issues(source: str, translated: str) -> List[str]:
    """译文里缺失/被换的源文锚点列表（空＝保真通过）。"""
    return [a for a in extract_anchors(source) if anchor_missing(a, translated)]


def swapped_geo(source: str, translated: str) -> str:
    """换名检测：源文含词典地名 A、译文出现词典**另一个**地名的音译且 A 的
    音译缺席 → 返回被换成的原词 key（`_297` 的宿务→马尼拉形态）；无 → 空串。"""
    src_keys = {_norm_token(a) for a in extract_anchors(source)}
    src_geo = {k for k in src_keys if k in GEO_TRANSLIT}
    if not src_geo:
        return ""
    out = str(translated or "")
    for key, variants in GEO_TRANSLIT.items():
        if key in src_geo:
            continue
        if any(v in out for v in variants):
            # 译文冒出源文没有的地名——只有当源文地名的音译同时缺席才判换名
            for sk in src_geo:
                if anchor_missing(sk, out):
                    return key
    return ""


def annotate_missing_anchors(
    source: str, translated: str, target_lang: str = "",
) -> Tuple[str, List[str]]:
    """译后对账主入口：``(可能追加括注的译文, 缺失锚点列表)``。

    有缺失 → 译文尾追加「（原词：Cebu / 38）」（目标非中文用英文标签），
    读译文的人（操作员镜像 / 客户）永远能看到源文原词；无缺失原样返回。
    """
    out = str(translated or "")
    issues = fidelity_issues(source, out)
    if not issues or not out.strip():
        return out, issues
    joined = " / ".join(issues)
    is_cjk_target = str(target_lang or "").lower().startswith("zh") or any(
        "\u4e00" <= ch <= "\u9fff" for ch in out)
    note = f"（原词：{joined}）" if is_cjk_target else f" (original: {joined})"
    return out + note, issues


#: AI 翻译引擎的提示词铁律（prompt 与 system 双注入；确定性引擎无提示面，
#: 由译后对账兜底）。
FIDELITY_PROMPT_RULE = (
    "Proper nouns (place names, person names, brand names) and numbers must "
    "be preserved exactly - never substitute a different place/person/number "
    "even if it seems more plausible in context; if unsure how to translate "
    "a name, keep the original word untranslated."
)


__all__ = [
    "FIDELITY_PROMPT_RULE",
    "GEO_TRANSLIT",
    "annotate_missing_anchors",
    "anchor_missing",
    "extract_anchors",
    "fidelity_issues",
    "swapped_geo",
]
