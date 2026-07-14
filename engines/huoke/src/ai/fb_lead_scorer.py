# -*- coding: utf-8 -*-
"""Facebook 线索评分器(Sprint 2 P1)。

评分维度(0-100):
  - 名字语言/地区匹配度    : 0-30
  - 来源群质量(目标群命中) : 0-25
  - 性别推断(基于名字)     : 0-15
  - 已有 leads.db 信息     : 0-30
       - bio 含目标关键词 +15
       - location 命中 +10
       - 有公司/职位 +5

输出:
  - score (int)
  - reasons (List[str])  解释加分项,便于前端展示
  - tier (S/A/B/C/D)     便于排序

设计原则:
  - 纯本地启发式,不调 LLM,延迟 <1ms
  - 后续可挂 LLM 二次精排(P2)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


import re as _re

# 日本文字检测
_HIRAGANA_RE = _re.compile(r"[\u3040-\u309F]")
_KATAKANA_RE = _re.compile(r"[\u30A0-\u30FF]")
_CJK_RE = _re.compile(r"[\u4E00-\u9FFF]")

# 日本女性名（罗马字 + 仮名/汉字常见字）
_JP_FEMALE_ROMAJI = {
    "miyuki", "sakura", "yuki", "mika", "ayumi", "hanako", "tomoko",
    "kazuko", "mieko", "yumiko", "keiko", "yoshiko", "aiko", "hiroko",
    "yuko", "michiko", "emiko", "satsuki", "rieko", "masako", "chiyo",
    "nobuko", "fumiko", "chieko", "teruko", "sumiko", "setsuko", "mikiko",
    "noriko", "yoko", "kyoko", "etsuko", "sachiko", "hisako", "reiko",
    "naoko", "haruko", "mineko", "kimiko", "akiko", "yukiko", "mariko",
    "junko", "kanako", "miho", "natsuko", "ruriko", "tomoe", "hana",
    "nana", "risa", "yumi", "ryoko", "yuka", "rika", "moe", "yui", "saki",
    "ai", "mai", "emi", "miki", "chie", "aya", "misaki", "nanami",
}
_JP_FEMALE_KANJI_HINTS = {
    "子", "美", "恋", "花", "恵", "惠", "奈", "香", "紗", "理",
    "裕", "麻", "智", "由", "里", "真", "沙", "菜", "愛", "葉",
    "ゆ", "み", "あ", "か", "さ", "ま", "り", "え",
}
# 日本男性名（罗马字）
_JP_MALE_ROMAJI = {
    "takuma", "hideo", "takeshi", "hiroshi", "masashi", "daisuke", "kenji",
    "yusuke", "tatsuya", "kenta", "shota", "ryota", "kazuki", "takuya",
    "naoki", "akira", "makoto", "satoshi", "koji", "taro", "jiro", "ichiro",
    "kenichi", "yoshio", "tadashi", "tsutomu", "masato", "takashi", "hideki",
    "toshio", "kazuo", "minoru", "osamu", "shinji", "yuji", "koichi",
}
_JP_MALE_KANJI_HINTS = {
    "雄", "夫", "郎", "朗", "男", "彦", "勇", "武", "豊", "博", "哲",
    "浩", "剛", "翔", "亮", "健", "隆", "徹", "進", "修",
    "太", "介", "助", "司", "志", "蔵",
}
# 日本姓氏（罗马字，男女通用）
_JP_SURNAMES_ROMAJI = {
    "tanaka", "yamada", "sato", "suzuki", "takahashi", "watanabe", "ito",
    "nakamura", "kobayashi", "yoshida", "yamamoto", "matsumoto", "inoue",
    "kimura", "hayashi", "shimizu", "saito", "ishikawa", "mori", "abe",
    "ikeda", "hashimoto", "yamazaki", "ogawa", "goto", "okada", "nishimura",
    "fujita", "fujii", "kato", "yamashita", "taniguchi", "ueda", "miura",
    "nagahama", "kawasaki", "hirao", "kokura", "matsuda", "honda", "ono",
}
# 非日本男性名（南亚/中东/欧美）
_NON_JP_MALE = {
    "khan", "singh", "mohammad", "ahmed", "ali", "kumar", "ram", "abdul",
    "hassan", "muhammad", "alamin", "carlos", "jose", "pedro", "ivan",
}

# 简化的意大利语 / 美国常用名字典(覆盖主流命中,不求完整)
_ITALIAN_FIRST_NAMES = {
    "marco", "luca", "matteo", "giovanni", "francesco", "andrea", "alessandro",
    "stefano", "lorenzo", "antonio", "giuseppe", "roberto", "paolo", "davide",
    "simone", "fabio", "michele", "claudio", "filippo", "riccardo",
    "mario", "carlo", "salvatore", "vincenzo", "leonardo", "tommaso",
    "giulia", "sofia", "martina", "chiara", "francesca", "elena", "anna",
    "laura", "alessia", "valentina", "elisa",
}

_ITALIAN_LAST_NAME_HINTS = (
    "rossi", "ferrari", "russo", "bianchi", "romano", "gallo", "esposito",
    "ricci", "marino", "greco", "bruno", "conti", "sanna",
)

_US_FIRST_NAMES = {
    "john", "mike", "michael", "david", "james", "robert", "william",
    "richard", "joseph", "thomas", "charles", "daniel", "matthew",
    "anthony", "donald", "mark", "paul", "steven", "andrew", "kevin",
    "brian", "george", "edward", "ronald", "timothy", "jason", "jeffrey",
    "ryan", "jacob", "gary", "nicholas", "eric",
}

_MALE_NAMES = _ITALIAN_FIRST_NAMES | _US_FIRST_NAMES
_FEMALE_HINTS = {"giulia", "sofia", "martina", "chiara", "francesca", "elena",
                 "anna", "laura", "alessia", "valentina", "elisa",
                 "sarah", "mary", "jennifer", "linda", "elizabeth"}


def _has_japanese_script(text: str) -> bool:
    """Check if text contains hiragana, katakana, or CJK."""
    return bool(_HIRAGANA_RE.search(text) or _KATAKANA_RE.search(text) or _CJK_RE.search(text))


def _jp_gender_from_name(name: str) -> str:
    """Infer gender from Japanese name (kanji/romaji)."""
    parts = [p.strip().lower() for p in name.split() if p.strip()]
    # Check romaji parts
    for p in parts:
        if p in _JP_FEMALE_ROMAJI:
            return "female"
        if p in _JP_MALE_ROMAJI:
            return "male"
    # Check kanji hints in full name
    for ch in _JP_FEMALE_KANJI_HINTS:
        if ch in name:
            return "female"
    for ch in _JP_MALE_KANJI_HINTS:
        if ch in name:
            return "male"
    return "unknown"


def _name_signal(name: str, target_country: str = "") -> Dict[str, Any]:
    """从名字推断 country / gender。"""
    if not name:
        return {"name_country": "unknown", "name_gender": "unknown",
                "score_country": 0, "score_gender": 0,
                "reasons": []}
    parts = [p.strip().lower() for p in name.split() if p.strip()]
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""

    name_country = "unknown"
    score_country = 0
    reasons: List[str] = []

    # Japanese detection: script-based or romaji surname/given name match
    _is_jp_script = _has_japanese_script(name)
    _has_jp_surname = any(p in _JP_SURNAMES_ROMAJI for p in parts)
    _has_jp_female = any(p in _JP_FEMALE_ROMAJI for p in parts)
    _has_jp_male = any(p in _JP_MALE_ROMAJI for p in parts)
    _has_non_jp = any(p in _NON_JP_MALE for p in parts)

    if _is_jp_script or _has_jp_surname or _has_jp_female or _has_jp_male:
        name_country = "JP"
        if not target_country or target_country.upper() == "JP":
            score_country = 30
            reasons.append("名字像日本人")
        else:
            score_country = 10
    elif _has_non_jp:
        name_country = "OTHER"
        if target_country and target_country.upper() == "JP":
            score_country = 0
            reasons.append("明显非日本名")
        else:
            score_country = 5
    elif first in _ITALIAN_FIRST_NAMES or any(last.endswith(h) for h in _ITALIAN_LAST_NAME_HINTS):
        name_country = "IT"
        if not target_country or target_country.upper() == "IT":
            score_country = 30
            reasons.append("名字像意大利人")
        else:
            score_country = 10
    elif first in _US_FIRST_NAMES:
        name_country = "US"
        if not target_country or target_country.upper() == "US":
            score_country = 25
            reasons.append("名字像美国人")
        else:
            score_country = 8

    # Gender detection (enhanced for JP)
    name_gender = "unknown"
    score_gender = 0
    if name_country == "JP":
        jp_g = _jp_gender_from_name(name)
        if jp_g == "female":
            name_gender = "female"
            if target_country and target_country.upper() == "JP":
                score_gender = 15
                reasons.append("日本女性名")
        elif jp_g == "male":
            name_gender = "male"
            # Negative score for male when targeting females
            # -30 ensures JP male (country=30 + gender=-30 + source=8 = 8) < min_l1(20)
            score_gender = -30
            reasons.append("日本男性名(非目标)")
    elif first in _FEMALE_HINTS:
        name_gender = "female"
    elif first in _MALE_NAMES or _has_non_jp:
        name_gender = "male"
        score_gender = -15
        reasons.append("可能为男性(非目标)")

    return {"name_country": name_country, "name_gender": name_gender,
            "score_country": score_country, "score_gender": score_gender,
            "reasons": reasons}


def _source_signal(source: str, target_groups: Optional[List[str]] = None) -> Dict[str, Any]:
    if not source:
        return {"score_source": 0, "reasons": []}
    if target_groups:
        if any(g.lower() in source.lower() for g in target_groups if g):
            return {"score_source": 25,
                    "reasons": [f"来自目标群: {source[:30]}"]}
    if any(kw in source.lower() for kw in
           ("italian", "italia", "expat", "italy")):
        return {"score_source": 20, "reasons": [f"群名相关: {source[:30]}"]}
    return {"score_source": 8, "reasons": []}


def _profile_signal(lead: Optional[Dict],
                    keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    if not lead:
        return {"score_profile": 0, "reasons": []}
    score = 0
    reasons: List[str] = []
    bio = (lead.get("notes") or "").lower()
    location = (lead.get("location") or "").lower()
    company = lead.get("company") or ""
    title = lead.get("title") or ""

    if keywords:
        if any(kw.lower() in bio for kw in keywords):
            score += 15
            reasons.append("简介命中关键词")
        if any(kw.lower() in location for kw in keywords):
            score += 10
            reasons.append("地区命中")
    if company or title:
        score += 5
        reasons.append("有职业信息")
    return {"score_profile": min(score, 30), "reasons": reasons}


def _tier_for_score(score: int) -> str:
    if score >= 80:
        return "S"
    if score >= 65:
        return "A"
    if score >= 45:
        return "B"
    if score >= 25:
        return "C"
    return "D"


def score_member(name: str, *,
                 source_group: str = "",
                 target_country: str = "",
                 target_groups: Optional[List[str]] = None,
                 lead_record: Optional[Dict] = None,
                 profile_keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    """评分单个群成员/线索。

    Returns:
        {
          "score": int(0-100),
          "tier": "S"|"A"|"B"|"C"|"D",
          "reasons": [...],
          "breakdown": {country, gender, source, profile},
        }
    """
    n_sig = _name_signal(name, target_country=target_country)
    s_sig = _source_signal(source_group, target_groups)
    p_sig = _profile_signal(lead_record, profile_keywords)

    score = (n_sig["score_country"] + n_sig["score_gender"]
             + s_sig["score_source"] + p_sig["score_profile"])
    score = min(score, 100)
    reasons = n_sig["reasons"] + s_sig["reasons"] + p_sig["reasons"]

    return {
        "score": int(score),
        "tier": _tier_for_score(score),
        "reasons": reasons,
        "breakdown": {
            "country": n_sig["score_country"],
            "gender": n_sig["score_gender"],
            "source": s_sig["score_source"],
            "profile": p_sig["score_profile"],
        },
        "inferred_country": n_sig["name_country"],
        "inferred_gender": n_sig["name_gender"],
    }


def batch_score_and_persist(member_names: List[str], *,
                            source_group: str = "",
                            target_country: str = "",
                            target_groups: Optional[List[str]] = None,
                            profile_keywords: Optional[List[str]] = None,
                            min_score_to_persist: int = 0) -> List[Dict]:
    """批量评分,并把结果写回 LeadsStore.score 字段。

    Returns:
        [{name, score, tier, reasons, lead_id}]
    """
    out = []
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
    except Exception:
        store = None

    for nm in member_names or []:
        nm = (nm or "").strip()
        if not nm:
            continue
        rec = None
        lid = None
        if store is not None:
            try:
                lid = store.find_match(name=nm)
                if lid:
                    rec = store.get_lead(lid)
            except Exception:
                pass

        result = score_member(
            nm,
            source_group=source_group,
            target_country=target_country,
            target_groups=target_groups,
            lead_record=rec,
            profile_keywords=profile_keywords,
        )
        result["name"] = nm
        result["lead_id"] = lid

        if store is not None and result["score"] >= min_score_to_persist:
            try:
                if not lid:
                    lid = store.add_lead(name=nm, source_platform="facebook",
                                         tags=[source_group] if source_group else [],
                                         notes=";".join(result["reasons"])[:500])
                    result["lead_id"] = lid
                if lid:
                    store.update_lead(lid, score=result["score"])
            except Exception:
                pass
        out.append(result)
    return out
