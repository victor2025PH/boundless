# -*- coding: utf-8 -*-
"""昵称解析预填（Q-5 B，#263）：把「Michael/46/New york/union carpenter/5.28」这类自带
分隔符的平台昵称拆成画像槽位候选——名 / 年龄 / 城市 / 职业。

为什么单独一层：陪伴域客户在 Telegram / WhatsApp 上的昵称常常就是「名/年龄/城市/职业」
自报家门，画像卡却一直空着——**从来没人读昵称**（0909 Q-5 §〇 三段之一）。这里只做确定性
解析（分隔符 + 词典），零 LLM；结果一律 ``source="nickname" status="mentioned"``（「来自昵称 ·
待确认」），由坐席在卡上 ✓ 确认，**绝不自动升 confirmed**。

规则（宁漏不错）：
- 只认带分隔符的昵称：``/`` ``|`` ``,`` ``，`` ``·`` ``-``（两侧带空格）``—`` 以及「空格 + 两位数字」
  （``Michael 46 NYC``）。纯单词昵称（``Michael``）不拆——单词既可能是名也可能是城市，不猜。
- 年龄：独立两位数 18–80。
- 日期片段（``5.28`` / ``05-28`` / ``1998`` / ``Jan 5``）忽略——那是生日 / 纪念日，不是年龄。
- 城市：小地名词典（大小写不敏感，含常见别名 ``NYC`` / ``LA`` / ``New york``），词典外的
  「首字母大写多词」片段**不**当城市——``Union Carpenter`` 也是首字母大写。
- 职业：职业词典（英 + 中）；片段含词典词即整段作职业值（``union carpenter`` 整段保留）。
- 名：第一段若是 1–3 个词的字母串 / 2–4 个汉字且不命中年龄 / 城市 / 职业 / 日期 → 名。
纯函数、绝不抛。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

NICKNAME_SOURCE = "nickname"

# 分隔符：/ | , ， · 、 ； ; ，以及两侧带空格的 - 或 —
_SEP_RE = re.compile(r"\s*(?:[/|｜,，·、;；／]|\s[-—–]\s)\s*")
# 「空格 + 两位数字」也算分隔（Michael 46 NYC）——数字段落在 18–80 才拆
_SPACE_AGE_RE = re.compile(r"^(?P<a>.+?)\s+(?P<age>\d{2})(?:\s+(?P<b>.+))?$")

_AGE_RE = re.compile(r"^(?:age\s*)?(\d{2})\s*(?:岁|y/?o|yo|yrs?|years?\s*old)?$", re.IGNORECASE)
_AGE_MIN, _AGE_MAX = 18, 80

# 日期片段：5.28 / 05-28 / 5/28 已被分隔拆开 → 这里认 5.28、05-28、1998、19980528、Jan 5
_DATE_RE = re.compile(
    r"^(?:\d{1,2}[.\-]\d{1,2}(?:[.\-]\d{2,4})?|(?:19|20)\d{2}(?:[.\-]?\d{2}[.\-]?\d{2})?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*\d{1,2}"
    r"|\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
    r"|\d{1,2}月\d{1,2}日?)$",
    re.IGNORECASE,
)

# 城市词典：规范名 → 别名集合（小写比对；含常见错写 New york）。刻意保持小而准。
_CITY_CANON: Dict[str, Tuple[str, ...]] = {
    "New York": ("new york", "newyork", "nyc", "new york city", "ny", "brooklyn", "manhattan", "queens", "bronx", "纽约"),
    "Los Angeles": ("los angeles", "la", "l.a.", "洛杉矶"),
    "San Francisco": ("san francisco", "sf", "旧金山", "三藩市"),
    "Chicago": ("chicago", "芝加哥"),
    "Houston": ("houston", "休斯顿"),
    "Miami": ("miami", "迈阿密"),
    "Seattle": ("seattle", "西雅图"),
    "Boston": ("boston", "波士顿"),
    "Dallas": ("dallas", "达拉斯"),
    "Atlanta": ("atlanta", "亚特兰大"),
    "Las Vegas": ("las vegas", "vegas", "拉斯维加斯"),
    "Phoenix": ("phoenix", "凤凰城"),
    "Denver": ("denver", "丹佛"),
    "Philadelphia": ("philadelphia", "philly", "费城"),
    "Washington": ("washington", "washington dc", "dc", "华盛顿"),
    "Toronto": ("toronto", "多伦多"),
    "Vancouver": ("vancouver", "温哥华"),
    "London": ("london", "伦敦"),
    "Manchester": ("manchester", "曼彻斯特"),
    "Paris": ("paris", "巴黎"),
    "Berlin": ("berlin", "柏林"),
    "Sydney": ("sydney", "悉尼"),
    "Melbourne": ("melbourne", "墨尔本"),
    "Auckland": ("auckland", "奥克兰"),
    "Singapore": ("singapore", "新加坡"),
    "Kuala Lumpur": ("kuala lumpur", "kl", "吉隆坡"),
    "Bangkok": ("bangkok", "曼谷"),
    "Manila": ("manila", "马尼拉"),
    "Jakarta": ("jakarta", "雅加达"),
    "Ho Chi Minh City": ("ho chi minh", "ho chi minh city", "saigon", "hcmc", "胡志明", "胡志明市"),
    "Hanoi": ("hanoi", "河内"),
    "Tokyo": ("tokyo", "东京"),
    "Osaka": ("osaka", "大阪"),
    "Seoul": ("seoul", "首尔"),
    "Dubai": ("dubai", "迪拜"),
    "Hong Kong": ("hong kong", "hongkong", "hk", "香港"),
    "Taipei": ("taipei", "台北"),
    "Beijing": ("beijing", "北京"),
    "Shanghai": ("shanghai", "上海"),
    "Guangzhou": ("guangzhou", "广州"),
    "Shenzhen": ("shenzhen", "深圳"),
    "Chengdu": ("chengdu", "成都"),
    "Hangzhou": ("hangzhou", "杭州"),
}
_CITY_BY_ALIAS: Dict[str, str] = {
    a: canon for canon, aliases in _CITY_CANON.items() for a in aliases
}

# 职业词典（片段含任一词即视为职业段；英文按整词匹配，中文按子串）
_OCCUPATION_WORDS_EN = (
    "carpenter", "electrician", "plumber", "welder", "mechanic", "driver", "trucker",
    "nurse", "doctor", "dentist", "surgeon", "pharmacist", "therapist", "veterinarian",
    "teacher", "professor", "lecturer", "tutor", "student",
    "engineer", "developer", "programmer", "designer", "architect", "analyst", "scientist",
    "accountant", "banker", "trader", "broker", "realtor", "agent", "consultant", "lawyer", "attorney",
    "chef", "cook", "baker", "barista", "bartender", "waiter", "waitress",
    "pilot", "sailor", "captain", "soldier", "veteran", "police", "officer", "firefighter", "marine", "navy",
    "farmer", "rancher", "fisherman", "miner", "contractor", "builder", "foreman", "roofer", "painter", "mason",
    "manager", "director", "ceo", "founder", "owner", "entrepreneur", "businessman", "businesswoman",
    "salesman", "cashier", "clerk", "receptionist", "secretary", "assistant",
    "photographer", "artist", "musician", "singer", "actor", "actress", "writer", "author", "journalist",
    "model", "dancer", "coach", "trainer", "athlete", "boxer",
    "retired", "retiree", "freelancer", "self-employed", "self employed", "unemployed",
    "technician", "operator", "supervisor", "inspector", "surveyor", "landscaper", "logger",
)
_OCCUPATION_WORDS_ZH = (
    "工程师", "程序员", "设计师", "医生", "护士", "老师", "教师", "教授", "学生", "律师", "会计",
    "司机", "厨师", "木工", "电工", "水电工", "焊工", "技工", "工人", "农民", "渔民", "军人", "警察",
    "消防员", "飞行员", "海员", "经理", "总监", "老板", "创业", "自由职业", "退休", "销售", "客服",
    "摄影师", "画家", "音乐人", "歌手", "演员", "作家", "记者", "模特", "教练", "健身", "运动员",
    "建筑师", "分析师", "科学家", "研究员", "公务员", "白领", "个体户", "开店", "做生意",
)
_OCC_EN_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(w) for w in _OCCUPATION_WORDS_EN) + r")(?:s|es)?(?![a-z])",
    re.IGNORECASE,
)

_NAME_LATIN_RE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.\-]*(?:\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.\-]*){0,2}$")
_NAME_CJK_RE = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\uFE0F\u200d]+")
_MAX_VALUE = 40


def _clean(s: Any) -> str:
    t = _EMOJI_RE.sub(" ", str(s or ""))
    t = re.sub(r"\s+", " ", t).strip(" \t\r\n\"'“”‘’()（）[]【】<>")
    return t


def _split(nick: str) -> List[str]:
    """按分隔符拆段；无分隔符时尝试「空格 + 两位数」。拆不出 ≥2 段 → []（不猜）。"""
    parts = [p for p in (_clean(x) for x in _SEP_RE.split(nick)) if p]
    if len(parts) >= 2:
        # 段内再拆一次「空格 + 年龄」（Michael 46 / New york）
        out: List[str] = []
        for p in parts:
            m = _SPACE_AGE_RE.match(p)
            if m and _AGE_MIN <= int(m.group("age")) <= _AGE_MAX and not _looks_date(p):
                out.append(_clean(m.group("a")))
                out.append(m.group("age"))
                if m.group("b"):
                    out.append(_clean(m.group("b")))
            else:
                out.append(p)
        return [p for p in out if p]
    m = _SPACE_AGE_RE.match(nick)
    if m and _AGE_MIN <= int(m.group("age")) <= _AGE_MAX and not _looks_date(nick):
        out = [_clean(m.group("a")), m.group("age")]
        tail = _clean(m.group("b") or "")
        if tail:
            # 纯空格分隔的中文昵称（张伟 35 上海 程序员）尾段再按空格拆；拉丁尾段不拆
            # （New york / union carpenter 是一个整体）
            if re.search(r"[\u4e00-\u9fff]", tail):
                out.extend(x for x in tail.split(" ") if x)
            else:
                out.append(tail)
        return [p for p in out if p]
    return []


def _looks_date(seg: str) -> bool:
    return bool(_DATE_RE.match(_clean(seg)))


def parse_age(seg: str) -> Optional[int]:
    m = _AGE_RE.match(_clean(seg))
    if not m:
        return None
    try:
        a = int(m.group(1))
    except ValueError:
        return None
    return a if _AGE_MIN <= a <= _AGE_MAX else None


def parse_city(seg: str) -> str:
    """词典命中 → 规范城市名（``New york`` → ``New York``）；否则 ""。"""
    key = re.sub(r"\s+", " ", _clean(seg)).casefold().replace("．", ".")
    if not key:
        return ""
    canon = _CITY_BY_ALIAS.get(key)
    if canon:
        return canon
    # 「NYC 🇺🇸」「Bangkok, TH」这类带后缀：取首词组再试
    head = re.split(r"[\s,]+", key)[0]
    return _CITY_BY_ALIAS.get(head, "") if len(key) <= 24 else ""


def parse_occupation(seg: str) -> str:
    """片段含职业词 → 整段作职业值（≤40 字）；否则 ""。"""
    s = _clean(seg)
    if not s or len(s) > _MAX_VALUE:
        return ""
    if _OCC_EN_RE.search(s):
        return s
    if any(w in s for w in _OCCUPATION_WORDS_ZH):
        return s
    return ""


def _looks_name(seg: str) -> bool:
    s = _clean(seg)
    if not s or len(s) > 30:
        return False
    return bool(_NAME_LATIN_RE.match(s) or _NAME_CJK_RE.match(s))


def parse_nickname(nickname: Any) -> Dict[str, str]:
    """昵称 → ``{name?, age?, location?, occupation?}``（值均为字符串，年龄为十进制串）。
    拆不出分隔段 / 全部片段都不可识别 → {}。绝不抛。"""
    try:
        nick = _clean(nickname)
        if not nick:
            return {}
        segs = _split(nick)
        if len(segs) < 2:
            return {}
        out: Dict[str, str] = {}
        for idx, seg in enumerate(segs):
            if _looks_date(seg):
                continue
            age = parse_age(seg)
            if age is not None and "age" not in out:
                out["age"] = str(age)
                continue
            city = parse_city(seg)
            if city and "location" not in out:
                out["location"] = city
                continue
            occ = parse_occupation(seg)
            if occ and "occupation" not in out:
                out["occupation"] = occ[:_MAX_VALUE]
                continue
            if idx == 0 and "name" not in out and _looks_name(seg):
                out["name"] = seg[:_MAX_VALUE]
                continue
        # 只拆出「名」一项＝没有任何自报信息，等于没拆（名单独不预填，防把网名当称呼）
        if set(out.keys()) == {"name"}:
            return {}
        return out
    except Exception:
        return {}


def nickname_candidates(nickname: Any) -> Dict[str, Dict[str, str]]:
    """``profile_fill.apply`` 输入形状：``{slot: {"value", "evidence"}}``，evidence＝昵称原文。"""
    nick = _clean(nickname)
    return {k: {"value": v, "evidence": nick[:80]} for k, v in parse_nickname(nick).items()}


__all__ = [
    "NICKNAME_SOURCE",
    "nickname_candidates",
    "parse_age",
    "parse_city",
    "parse_nickname",
    "parse_occupation",
]
