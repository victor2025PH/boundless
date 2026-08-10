"""出站「时空接轨」守卫（纯函数）：人设当地时钟 vs 回复里的时段/地点断言。

背景（2026-07 实录）：温哥华人设注入行被二次换算成近似 UTC+8 正午，LLM 跟着说
「下午好/刚吃午饭」，客户一对照真时区立刻穿帮。根因在 ``persona_now`` 幂等修复后，
本模块作第二道防线——出站文本仍声称与当地墙钟冲突的问候/作息/错城「我在 X」时，
句级剥离（与 outbound_promise_guard 同族：宁可漏报不误伤）。
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from src.companion.persona_location import (
    CITY_PRESETS,
    PersonaPlace,
    daypart_label,
    resolve_place_with_fallback,
)

__all__ = [
    "detect_daypart_conflict",
    "strip_daypart_conflicts",
    "detect_weekday_conflict",
    "strip_weekday_conflicts",
    "detect_wrong_place_claim",
    "strip_wrong_place_claims",
    "apply_world_clock_guard",
    "world_clock_prompt_nail",
]

_SENT_SPLIT_RE = re.compile(r"([。！？!?～~;；…\n]+)")


def _sentences(text: str) -> List[str]:
    parts = _SENT_SPLIT_RE.split(str(text or ""))
    return [p for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]


def _rejoin(original: str, kept: Sequence[str]) -> str:
    """按原句序拼回；若全剥空回空串（调用方再兜底）。"""
    if not kept:
        return ""
    # 简单空格/无空格：中文原样紧拼，若原文含换行则用换行
    joiner = "\n" if "\n" in (original or "") else ""
    out = joiner.join(s.strip() for s in kept if str(s).strip())
    return out.strip()


# (regex, hour_lo_inclusive, hour_hi_exclusive) —— 命中且当地小时不在 [lo,hi) → 冲突
# 口径刻意窄：只抓明确时段问候/刚吃某餐，不抓「今天天气不错」这类无时段句。
_DAYPART_CLAIMS: Tuple[Tuple[re.Pattern[str], int, int, str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), lo, hi, tag)
    for pat, lo, hi, tag in (
        (r"早上\s*好|早安|早啊|刚\s*起床|剛\s*起床|刚\s*睡醒|剛\s*睡醒", 5, 11, "morning"),
        (r"上午\s*好", 8, 12, "late_morning"),
        (r"中午\s*好|刚\s*吃\s*(?:了|完)?\s*午\s*饭|剛\s*吃\s*(?:了|完)?\s*午\s*飯", 11, 15, "noon"),
        (r"下午\s*好|下午好呀|下午好啊", 12, 18, "afternoon"),
        (r"傍晚\s*好|黄昏\s*好", 17, 20, "dusk"),
        (r"晚上\s*好|晚上好呀|晚上好啊", 17, 24, "evening"),
        (r"good\s*morning", 5, 11, "morning"),
        (r"good\s*afternoon", 12, 18, "afternoon"),
        (r"good\s*evening", 17, 24, "evening"),
    )
)


def detect_daypart_conflict(text: str, local_hour: int) -> Optional[str]:
    """出站是否含与当地小时冲突的时段断言；返回冲突 tag 或 None。"""
    try:
        h = int(local_hour) % 24
        blob = str(text or "")
        if not blob.strip():
            return None
        for pat, lo, hi, tag in _DAYPART_CLAIMS:
            if not pat.search(blob):
                continue
            if lo <= h < hi:
                continue
            # 晚上好跨午夜：0-1 点仍可算「还没睡的晚上」——不剥
            if tag == "evening" and h < 2:
                continue
            return tag
    except Exception:
        return None
    return None


def strip_daypart_conflicts(text: str, local_hour: int) -> str:
    """句级剥离与当地小时冲突的时段断言句；无冲突原样返回。"""
    try:
        if detect_daypart_conflict(text, local_hour) is None:
            return str(text or "")
        h = int(local_hour) % 24
        kept: List[str] = []
        for sent in _sentences(text):
            bad = False
            for pat, lo, hi, tag in _DAYPART_CLAIMS:
                if not pat.search(sent):
                    continue
                ok = lo <= h < hi or (tag == "evening" and h < 2)
                if not ok:
                    bad = True
                    break
            if not bad:
                kept.append(sent)
        # 保留句间定界符：用原文切分重组
        if not kept:
            return ""
        # 从原文按句子重建（保非冲突句及其后标点）
        parts = _SENT_SPLIT_RE.split(str(text or ""))
        out: List[str] = []
        for i in range(0, len(parts), 2):
            body = parts[i]
            delim = parts[i + 1] if i + 1 < len(parts) else ""
            if body.strip() and body.strip() in {k.strip() for k in kept}:
                out.append(body + delim)
            elif not body.strip() and delim and out:
                # 孤立定界符丢弃
                pass
        return "".join(out).strip() or _rejoin(text, kept)
    except Exception:
        return str(text or "")


# ---- 星期断言（2026-08-02 实录：周日被说成 "Saturday evening"，手机端
# DataDetector 给时间词加下划线，星期说错=可点击的穿帮证物）----
#
# 设计要点（宁可漏报不误伤）：
# 1. 只抓「现在时锚定」断言（今天是周六 / it's Saturday）；习惯性泛指
#    （"我一般周日跑步" / "I usually run on a Sunday"）不碰——那不是对
#    今天的断言。
# 2. 合法集=调用方给的 allowed_weekdays（人设当地星期 ∪ 客户当地星期）：
#    跨时区会话里两个框架的星期都算对（温哥华周六傍晚 vs 客户周日凌晨），
#    只剥两边都解释不通的真幻觉（如凭空说「今天周五」）。
_ZH_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_EN_WD = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WD_CLAIM_ZH = re.compile(
    r"(?:今天|今晚|今儿|今兒|现在|現在)\s*(?:已经|已經)?\s*(?:是|可是)?\s*"
    r"(?:周|週|星期|礼拜|禮拜)([一二三四五六日天])")
_WD_CLAIM_EN = re.compile(
    r"(?:today\s+is|it(?:'|’)?s)\s+(?:a\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE)
_WEEKEND_CLAIM_ZH = re.compile(
    r"(?:今天|今晚|今儿|今兒|现在|現在)\s*(?:已经|已經)?\s*(?:是|可是)?\s*周末")
_WEEKEND_CLAIM_EN = re.compile(
    r"(?:today\s+is|it(?:'|’)?s)\s+(?:the\s+)?weekend\b", re.IGNORECASE)


def _claimed_weekdays(text: str) -> List[int]:
    """抽取文本里「现在时锚定」的星期断言（0=周一..6=周日）；无断言回空表。"""
    out: List[int] = []
    blob = str(text or "")
    for m in _WD_CLAIM_ZH.finditer(blob):
        idx = _ZH_WD.get(m.group(1))
        if idx is not None and idx not in out:
            out.append(idx)
    for m in _WD_CLAIM_EN.finditer(blob):
        idx = _EN_WD.get(m.group(1).lower())
        if idx is not None and idx not in out:
            out.append(idx)
    return out


def detect_weekday_conflict(
    text: str, allowed_weekdays: Sequence[int],
) -> Optional[int]:
    """出站是否断言了合法集之外的星期；返回冲突的星期 idx 或 None。

    ``allowed_weekdays`` 空 → 无判据，恒不冲突（调用方拿不到任何一侧的
    当地时间时保守放行）。「周末」断言：合法集里有周六或周日即放行。
    """
    try:
        allowed = {int(d) % 7 for d in (allowed_weekdays or ())}
        if not allowed:
            return None
        blob = str(text or "")
        if not blob.strip():
            return None
        for idx in _claimed_weekdays(blob):
            if idx not in allowed:
                return idx
        if (_WEEKEND_CLAIM_ZH.search(blob) or _WEEKEND_CLAIM_EN.search(blob)) \
                and not (allowed & {5, 6}):
            return -1  # 周末断言但两边都是工作日
    except Exception:
        return None
    return None


def strip_weekday_conflicts(
    text: str, allowed_weekdays: Sequence[int],
) -> str:
    """句级剥离含冲突星期断言的句子；无冲突原样返回。"""
    try:
        if detect_weekday_conflict(text, allowed_weekdays) is None:
            return str(text or "")
        allowed = {int(d) % 7 for d in (allowed_weekdays or ())}
        parts = _SENT_SPLIT_RE.split(str(text or ""))
        out: List[str] = []
        for i in range(0, len(parts), 2):
            body = parts[i]
            delim = parts[i + 1] if i + 1 < len(parts) else ""
            if body.strip() and detect_weekday_conflict(body, sorted(allowed)) is not None:
                continue
            out.append(body + delim)
        return "".join(out).strip()
    except Exception:
        return str(text or "")


# 错城「我人在 X」——城市词表来自 CITY_PRESETS；排除本城 + 同国家兜底词误伤需调用方给 place。
_HERE_PREFIX = (
    r"(?:我|人家)?\s*(?:现在|現在|就)?\s*"
    r"(?:人\s*)?(?:在|来到|來到|住在|待在|待在)"
)
_HERE_PREFIX_EN = r"\b(?:i(?:'|’)?m|i\s+am|i(?:'|’)?m\s+currently)\s+(?:in|at)\s+"


def _city_aliases(slug: str, meta: dict) -> List[str]:
    names = [str(meta.get("city_zh") or ""), str(meta.get("city_en") or "")]
    extras = {
        "vancouver": ["温哥华", "溫哥華"],
        "cebu": ["宿务", "宿霧", "Cebu"],
        "manila": ["马尼拉", "馬尼拉", "马尼剌"],
        "hong_kong": ["香港", "HK"],
        "tokyo": ["东京", "東京"],
    }.get(slug, [])
    out: List[str] = []
    for n in names + extras:
        n = str(n or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def detect_wrong_place_claim(text: str, place: Optional[PersonaPlace]) -> Optional[str]:
    """第一人称「我（现在）在 X」且 X≠人设城 → 返回错城 slug；遗产闲聊不触发。"""
    try:
        if place is None:
            return None
        blob = str(text or "")
        if not blob.strip():
            return None
        home = place.slug
        # 国家级词：仅当「我在菲律宾/加拿大」这类现居断言才抓；妈妈是菲律宾人不抓
        country_here = [
            (re.compile(
                _HERE_PREFIX + r"\s*(?:菲律宾|菲律賓|the\s+philippines)",
                re.IGNORECASE), "manila"),
            (re.compile(
                _HERE_PREFIX + r"\s*(?:加拿大|canada)",
                re.IGNORECASE), "vancouver"),
            (re.compile(
                _HERE_PREFIX_EN + r"(?:the\s+)?philippines\b",
                re.IGNORECASE), "manila"),
            (re.compile(
                _HERE_PREFIX_EN + r"canada\b",
                re.IGNORECASE), "vancouver"),
        ]
        for pat, slug in country_here:
            if pat.search(blob) and slug != home and CITY_PRESETS.get(slug, {}).get("country") != place.country:
                return slug
        for slug, meta in CITY_PRESETS.items():
            if slug == home:
                continue
            # 同国家另一城（宿务 vs 马尼拉）仍算错城——人设钉在具体城
            for alias in _city_aliases(slug, meta):
                if not alias:
                    continue
                if re.search(_HERE_PREFIX + re.escape(alias), blob, re.IGNORECASE):
                    return slug
                if re.search(_HERE_PREFIX_EN + re.escape(alias) + r"\b", blob, re.IGNORECASE):
                    return slug
        return None
    except Exception:
        return None


def strip_wrong_place_claims(text: str, place: Optional[PersonaPlace]) -> str:
    """句级剥离错城现居断言。"""
    try:
        if detect_wrong_place_claim(text, place) is None:
            return str(text or "")
        parts = _SENT_SPLIT_RE.split(str(text or ""))
        out: List[str] = []
        for i in range(0, len(parts), 2):
            body = parts[i]
            delim = parts[i + 1] if i + 1 < len(parts) else ""
            if body.strip() and detect_wrong_place_claim(body, place):
                continue
            out.append(body + delim)
        return "".join(out).strip()
    except Exception:
        return str(text or "")


def world_clock_prompt_nail(
    place: Optional[PersonaPlace],
    local_hour: int,
    *,
    lang: str = "zh",
) -> str:
    """短钉：叠在当地时间行后，防 LLM 滑回服务器 UTC+8 口径。"""
    try:
        if place is None:
            return ""
        part = daypart_label(int(local_hour) % 24, lang)
        if str(lang or "zh").lower().startswith("zh"):
            return (
                f"【时空钉】你此刻在{place.display('zh')}，当地是{part}。"
                f"不要按中国或菲律宾时间打招呼/谈作息；"
                f"提到别处只可作回忆或家人所在，不可说成你人现在在那里。"
            )
        return (
            f"[World clock] You are in {place.display('en')} ({part} locally). "
            f"Do not greet or narrate routines on China/Philippines time; "
            f"other places may be family/memory only — not where you are now."
        )
    except Exception:
        return ""


def apply_world_clock_guard(
    text: str,
    *,
    persona: Any = None,
    place: Optional[PersonaPlace] = None,
    local_hour: Optional[int] = None,
    local_now: Any = None,
    peer_now: Any = None,
) -> Tuple[str, dict]:
    """一站式：时段冲突 + 星期断言 + 错城现居。返回 (新文本, 观测 dict)。

    ``peer_now`` 可选＝客户当地 naive 时间（user_clock 显式信号解析所得）：
    跨时区会话星期合法集＝人设星期 ∪ 客户星期——两个框架的星期都算对。
    """
    info: dict = {
        "daypart_conflict": None,
        "weekday_conflict": None,
        "wrong_place": None,
        "changed": False,
    }
    try:
        raw = str(text or "")
        if place is None and persona is not None:
            place = resolve_place_with_fallback(persona)
        hour = local_hour
        if hour is None and local_now is not None:
            try:
                hour = int(getattr(local_now, "hour"))
            except Exception:
                hour = None
        if hour is None:
            hour = -1
        allowed_wd: List[int] = []
        for _dt in (local_now, peer_now):
            try:
                _w = int(_dt.weekday())
            except Exception:
                continue
            if _w not in allowed_wd:
                allowed_wd.append(_w)
        # 先在原文上取证（再剥）：时段句常与错城同句，先剥时段会吞掉错城证据。
        if hour >= 0:
            info["daypart_conflict"] = detect_daypart_conflict(raw, hour)
        info["weekday_conflict"] = detect_weekday_conflict(raw, allowed_wd)
        info["wrong_place"] = detect_wrong_place_claim(raw, place)
        out = raw
        if info["daypart_conflict"]:
            out = strip_daypart_conflicts(out, hour)
        if info["weekday_conflict"] is not None:
            out = strip_weekday_conflicts(out, allowed_wd)
        if info["wrong_place"]:
            out = strip_wrong_place_claims(out, place)
        info["changed"] = out.strip() != raw.strip()
        return out, info
    except Exception:
        return str(text or ""), info
