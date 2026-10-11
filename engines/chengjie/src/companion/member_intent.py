"""群成员意向分：按对方在群里说过的话，判断是不是这个销售人设要找的人。

开口名额很稀缺（每号每天 3–10 人），先发给最可能成交的人。关键词随人设走
（``persona.sales.intent_keywords`` / ``intent_negative``），所以客户导入一个销售人设，
目标客户画像也一起带上；``companion.group_members.intent_keywords`` 可在配置里追加。

纯函数、确定性：同一句话、同一组词永远同分，不调用 LLM。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

INTENT_PER_HIT = 25
INTENT_MAX = 100
# 命中排除词＝不开口（违规行业等）；排序时压到最后，并在卡片上写明原因。
INTENT_NEGATIVE = -100
_MAX_TERMS = 200


def _clean_terms(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [x for x in raw.replace("，", ",").split(",")]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    seen = set()
    for t in raw:
        s = " ".join(str(t or "").split()).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= _MAX_TERMS:
            break
    return out


def intent_terms(persona: Any, cfg: Any = None) -> Dict[str, List[str]]:
    """人设 ``sales`` 段 + 配置追加 → {positive, negative}。都没配 → 两个空表（不打分）。"""
    sales = persona.get("sales") if isinstance(persona, dict) else None
    sales = sales if isinstance(sales, dict) else {}
    gm: Any = cfg if isinstance(cfg, dict) else {}
    for key in ("companion", "group_members"):
        gm = gm.get(key) if isinstance(gm, dict) else None
    gm = gm if isinstance(gm, dict) else {}
    pos = _clean_terms(sales.get("intent_keywords")) + _clean_terms(gm.get("intent_keywords"))
    neg = _clean_terms(sales.get("intent_negative")) + _clean_terms(gm.get("intent_negative"))
    return {"positive": list(dict.fromkeys(pos)), "negative": list(dict.fromkeys(neg))}


def intent_score(text: Any, terms: Optional[Dict[str, Sequence[str]]]) -> Tuple[int, List[str]]:
    """(分数, 命中词)。命中排除词 → (INTENT_NEGATIVE, [排除词])；否则每个不同的正向词 +25，封顶 100。"""
    s = " ".join(str(text or "").split()).lower()
    if not s or not terms:
        return 0, []
    for t in terms.get("negative") or ():
        if t and t in s:
            return INTENT_NEGATIVE, [t]
    hits = [t for t in (terms.get("positive") or ()) if t and t in s]
    return min(INTENT_MAX, INTENT_PER_HIT * len(hits)), hits


def member_intent(member: Dict[str, Any], terms: Optional[Dict[str, Sequence[str]]]) -> Tuple[int, List[str]]:
    return intent_score(member.get("last_msg_text"), terms)


__all__ = ["INTENT_PER_HIT", "INTENT_MAX", "INTENT_NEGATIVE", "intent_terms",
           "intent_score", "member_intent"]
