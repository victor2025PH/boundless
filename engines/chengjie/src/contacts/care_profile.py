"""M-1 A（#214 #218，2026-09-06）：关怀 LLM 档的客户档案注入 + 事实校验 + 强制预览判定。

事故：运营为菲律宾本地客户（相识一年）排了一条关怀，LLM 改写成「How are you finding
life in the Philippines? Is the food there as good as what I cook?」并真发——生成时
**没有任何客户档案**（国籍 / 居住地 / 相识时长 / 语言）进 prompt，出稿也没人核。

本模块三件事，派发器与预览端点共用同一口径（预判＝行为）：
- ``build_customer_profile``：从 inbox 会话 / conv_meta（用户时钟推断国家）/ contacts 档案
  拼出一份**只含硬事实**的客户档案（全部 best-effort，任一源缺失即该字段为空）；
- ``profile_block``：档案 → prompt 注入块（【客户档案】，明确写「本地人不是旅居」「老朋友
  不是刚认识」这两句最容易被 LLM 编错的事实）；
- ``forced_preview_reason``：这条 LLM 拟稿该不该强制过人眼——首次对该客户真发 / 含地名 /
  含人名 → 返回原因码（非空即强制预览，行留 pending 待运营确认）。

事实校验本体（``detect_profile_contradiction``）放在 ``proactive_fabrication_guard``
（L-1 C 守卫家族），这里只做组装与调用。纯函数无 IO；provider 部分只吃 duck-typed store。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from src.utils.proactive_fabrication_guard import (
    detect_profile_contradiction,
    known_days_from_profile,
    normalize_country_code,
    place_mentions,
)

_DAY = 86400.0

# 相识时长档 → 中文短语（与 origin_context.KNOWN_SINCE_LABELS 同口径，避免跨模块依赖）
_KNOWN_SINCE_PHRASE = {
    "recent": "最近才认识（新朋友）",
    "months": "认识几个月了",
    "halfyear": "认识半年以上",
    "years": "认识一年以上（老朋友，不是刚认识）",
}

_COUNTRY_ZH = {
    "PH": "菲律宾", "TH": "泰国", "VN": "越南", "ID": "印尼", "MY": "马来西亚", "SG": "新加坡",
    "KH": "柬埔寨", "MM": "缅甸", "LA": "老挝", "JP": "日本", "KR": "韩国", "CN": "中国",
    "HK": "香港", "TW": "台湾", "IN": "印度", "PK": "巴基斯坦", "BD": "孟加拉", "NP": "尼泊尔",
    "AE": "阿联酋", "SA": "沙特", "TR": "土耳其", "EG": "埃及", "NG": "尼日利亚", "ZA": "南非",
    "US": "美国", "CA": "加拿大", "GB": "英国", "AU": "澳大利亚", "NZ": "新西兰", "DE": "德国",
    "FR": "法国", "ES": "西班牙", "IT": "意大利", "RU": "俄罗斯", "BR": "巴西", "MX": "墨西哥",
}

_LANG_ZH = {
    "en": "英语", "zh": "中文", "zh-tw": "繁体中文", "yue": "粤语", "ja": "日语", "ko": "韩语",
    "th": "泰语", "vi": "越南语", "id": "印尼语", "ms": "马来语", "tl": "他加禄语",
    "es": "西班牙语", "pt": "葡萄牙语", "fr": "法语", "de": "德语", "ru": "俄语", "ar": "阿拉伯语",
}


def empty_profile() -> Dict[str, Any]:
    return {
        "country": "", "residence": "", "language": "", "known_since": "",
        "known_days": 0, "display_name": "", "sources": [],
    }


def profile_is_empty(profile: Optional[dict]) -> bool:
    p = profile or {}
    return not any((p.get("country"), p.get("residence"), p.get("language"),
                    p.get("known_since"), int(p.get("known_days") or 0) > 0))


def build_customer_profile(
    item: dict, *, inbox_store: Any = None, contacts_store: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """care 行（contact_key=会话 id，platform/account_id/chat_key）→ 客户档案 dict。

    来源优先级（每个字段独立取，先到先得）：
      国家/居住地：contacts.country_hint → conv_meta.tz_country（用户时钟：客户自述城市 /
        WhatsApp 号码国码 / 语种默认国；``tz_source`` 为 behavior 的统计推断**不采**——
        猜错就是当着客户面说错事实）；
      语言：contacts.language_hint → conversations.language（unknown 视为空）；
      相识时长：contact_profiles.known_since（运营填的档位）→ conversations.first_seen /
        created_at 折算天数。
    全程 best-effort：任一 store 缺席 / 抛错 → 对应字段为空，绝不抛。
    """
    out = empty_profile()
    n = float(now if now is not None else time.time())
    it = dict(item or {})
    cid = str(it.get("contact_key") or "").strip()
    conv: Dict[str, Any] = {}
    if inbox_store is not None and cid:
        try:
            conv = dict(inbox_store.get_conversation(cid) or {})
        except Exception:
            conv = {}
    if conv:
        out["display_name"] = str(conv.get("display_name") or "")
        lang = str(conv.get("language") or "").strip().lower()
        if lang and lang != "unknown":
            out["language"] = lang
            out["sources"].append("conv.language")
        first = 0.0
        for k in ("first_seen", "created_at"):
            try:
                v = float(conv.get(k) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0 and (first <= 0 or v < first):
                first = v
        if first > 0 and n > first:
            out["known_days"] = int((n - first) // _DAY)
            out["sources"].append("conv.first_seen")
    # 用户时钟推断的国家（只信显式信号）
    if inbox_store is not None and cid and hasattr(inbox_store, "get_conv_meta"):
        try:
            meta = dict(inbox_store.get_conv_meta(cid) or {})
        except Exception:
            meta = {}
        src = str(meta.get("tz_source") or "").strip().lower()
        country = normalize_country_code(str(meta.get("tz_country") or ""))
        if country and src and "behavior" not in src and "activity" not in src:
            out["residence"] = country
            out["sources"].append(f"conv_meta.tz_country:{src}")
    # contacts 档案（country_hint / language_hint / known_since）
    contact_id = str(conv.get("contact_id") or "").strip()
    if contacts_store is not None and not contact_id:
        try:
            from src.contacts.origin_context import resolve_contact_for_conversation
            contact_id = resolve_contact_for_conversation(
                contacts_store, platform=str(it.get("platform") or ""),
                account_id=str(it.get("account_id") or "default"),
                chat_key=str(it.get("chat_key") or ""))
        except Exception:
            contact_id = ""
    if contacts_store is not None and contact_id:
        try:
            c = contacts_store.get_contact(contact_id)
        except Exception:
            c = None
        if c is not None:
            ch = normalize_country_code(str(getattr(c, "country_hint", "") or ""))
            if ch:
                out["country"] = ch
                out["sources"].append("contacts.country_hint")
            lh = str(getattr(c, "language_hint", "") or "").strip().lower()
            if lh and lh != "unknown":
                out["language"] = lh
                out["sources"].append("contacts.language_hint")
        try:
            prof = contacts_store.get_contact_profile(contact_id) or {}
        except Exception:
            prof = {}
        ks = str((prof or {}).get("known_since") or "").strip()
        if ks:
            out["known_since"] = ks
            out["sources"].append("contact_profiles.known_since")
    if not out["country"] and out["residence"]:
        out["country"] = out["residence"]
    return out


def profile_block(profile: Optional[dict]) -> str:
    """档案 → prompt 注入块。全空 → ""（prompt 与旧版逐字一致）。"""
    p = profile or {}
    if profile_is_empty(p):
        return ""
    lines: List[str] = []
    home = normalize_country_code(str(p.get("residence") or "")) \
        or normalize_country_code(str(p.get("country") or ""))
    if home:
        name = _COUNTRY_ZH.get(home, home)
        lines.append(f"- 对方是{name}人、就住在{name}（本地人，不是去{name}旅居/旅行的外国人，"
                     f"不要问 TA「{name}的生活习惯吗 / 好玩吗」这类外来者问题）")
    days = known_days_from_profile(p)
    ks = str(p.get("known_since") or "").strip().lower()
    if ks in _KNOWN_SINCE_PHRASE:
        lines.append(f"- 你们{_KNOWN_SINCE_PHRASE[ks]}")
    elif days >= 30:
        lines.append(f"- 你们已经认识 {days} 天了（老朋友，不是刚认识，不要说「很高兴认识你」）")
    elif days > 0:
        lines.append(f"- 你们认识 {days} 天")
    lang = str(p.get("language") or "").strip().lower()
    if lang:
        lines.append(f"- 对方常用语言：{_LANG_ZH.get(lang, lang)}")
    if not lines:
        return ""
    return ("【客户档案（硬事实，话术绝不能与之矛盾；不要逐条复述）】\n"
            + "\n".join(lines))


# 人名判定：句中（非句首）的大写词、不在常见词表里 → 人名候选。刻意保守。
_CAP_WORD_STOP = {
    "I", "AI", "OK", "Ok", "Hi", "Hey", "Hello", "Yes", "No", "Oh", "Wow", "Haha", "Lol",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Christmas", "Easter", "God", "Facebook", "Instagram",
    "Telegram", "WhatsApp", "Messenger", "LINE", "Line", "Netflix", "YouTube", "Google",
    "English", "Chinese", "Japanese", "Korean", "Thai", "Filipino", "Tagalog", "Spanish",
    "Good", "Happy", "Sweet", "Take", "Have", "How", "What", "When", "Where", "Why", "Who",
    "Are", "Did", "Do", "Is", "It", "The", "This", "That", "You", "Your", "We", "Our", "My",
    "Me", "And", "But", "So", "If", "Just", "Also", "Then", "Now", "Today", "Tonight",
    "Tomorrow", "Yesterday", "Morning", "Night", "Miss", "Mr", "Mrs", "Ms", "Sir", "Ma",
}
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？\n])\s+")
_CAP_WORD = re.compile(r"\b([A-Z][a-z]{1,})\b")


def person_name_candidates(text: str, profile: Optional[dict] = None) -> List[str]:
    """文案里疑似人名：① 客户显示名的词元出现在文案里；② 英文句中非句首的大写词
    （不在常见词表、不是地名）。保守：宁漏不误。"""
    t = str(text or "")
    if not t.strip():
        return []
    out: List[str] = []
    seen: set = set()

    def _add(w: str) -> None:
        k = w.lower()
        if len(w) >= 2 and k not in seen:
            seen.add(k)
            out.append(w)

    p = profile or {}
    for tok in re.split(r"[\s,，、/|@()（）]+", str(p.get("display_name") or "")):
        tok = tok.strip()
        if len(tok) >= 2 and re.search(r"[A-Za-z\u4e00-\u9fff]", tok):
            if re.search(r"(?<![A-Za-z])" + re.escape(tok) + r"(?![A-Za-z])", t, re.IGNORECASE):
                _add(tok)
    place_low = {n for zh, en, dem in _gazetteer().values() for n in (*zh, *en, *dem)}
    for sent in _SENT_SPLIT.split(t):
        stripped = sent.strip()
        words = _CAP_WORD.findall(stripped)
        if not words:
            continue
        # 句首大写词不算（英文句首必大写）
        if stripped.startswith(words[0]):
            words = words[1:]
        for w in words:
            if w in _CAP_WORD_STOP or w.lower() in place_low:
                continue
            _add(w)
    return out


def _gazetteer() -> dict:
    from src.utils.proactive_fabrication_guard import PLACE_GAZETTEER
    return PLACE_GAZETTEER


def forced_preview_reason(text: str, profile: Optional[dict] = None, *,
                          first_real_send: bool = False) -> str:
    """LLM 拟稿是否须强制过人眼。返回原因码：``first_send`` / ``place`` / ``person`` / ""。"""
    if first_real_send:
        return "first_send"
    if place_mentions(text):
        return "place"
    if person_name_candidates(text, profile):
        return "person"
    return ""


def check_care_reply(text: str, profile: Optional[dict] = None, *,
                     first_real_send: bool = False) -> Dict[str, Any]:
    """一次性出「拦 / 预览 / 放行」三态判定（派发器与预览端点共用）。

    返回 ``{"verdict": "block"|"preview"|"pass", "reason": str}``。矛盾优先于预览。
    """
    bad, why = detect_profile_contradiction(text, profile)
    if bad:
        return {"verdict": "block", "reason": f"profile_contradiction:{why}"}
    pv = forced_preview_reason(text, profile, first_real_send=first_real_send)
    if pv:
        return {"verdict": "preview", "reason": pv}
    return {"verdict": "pass", "reason": ""}


__all__ = [
    "build_customer_profile", "check_care_reply", "empty_profile", "forced_preview_reason",
    "person_name_candidates", "profile_block", "profile_is_empty",
]
