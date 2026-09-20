# -*- coding: utf-8 -*-
"""客户私事 vs 可共享事实 —— 记忆去向判定的单一来源（O-2 C，#201 / W7ZTSB / SV62XJ③）。

问题：客户说的「周六下午三点星巴克见」「我生日是 3 月 2 号」若进了**共享**知识库，别的
客户会被复述别人的私事（#201 跨客户串记忆）；而「曼谷雨季一般是五月到十月」这类通用
常识没有任何个人锚点，可以进共享 KB。此前只有 DailyLearner 的 ``private_kinds`` 一处
在判（J-10 六类高影响词表 + 承诺 + 自报姓名），**具体时间 / 具体地点 / 生日 / 第一二
人称属性**都不在词表里——正是指令验收三例里前两例会漏的原因。本模块把「个人锚点」
判定收成一个纯函数，抽取链日志与 DailyLearner 分流同一口径。

判据（宁严勿松——误把业务问题判成私事，会让本该进共享 KB 的条目改写进客户记忆）：

- **强锚点**（单独即成立）：J-10 六类 ``money / meet / address / identity / family /
  health``、承诺 ``commitment``、自报姓名 ``name``、生日 ``birthday``、带「日 / 号」的
  完整日期（``time``）、第一 / 二人称属性 ``personal``（我住在… / 你今年 30 岁 / I live in…）；
- **弱信号必须合取**：星期 / 相对日（明天 / 周末）/ 钟点 / 场所词单独都不算——
  「你们周六营业吗」「附近有公园吗」「有三点建议」是业务问题；
  ``time`` = 日期 ∨ (钟点 ∧ (星期 ∨ 相对日 ∨ 约见动词)) ∨ ((星期 ∨ 相对日) ∧ (钟点 ∨ 约见动词))；
  ``place`` = 场所词 ∧ (time 成立 ∨ 约见动词)。

``memory_scope(text)`` → ``("customer", "birthday,time,personal")`` 或
``("shared", "no_personal_anchor")``。纯函数、零框架依赖、确定性可测。
"""
from __future__ import annotations

import re
from typing import List, Tuple

from src.utils.memory_review import high_impact_categories, is_commitment

SCOPE_CUSTOMER = "customer"
SCOPE_SHARED = "shared"
REASON_NO_ANCHOR = "no_personal_anchor"

#: 锚点类别（顺序即 reason 串 / 学习页标签顺序）
ANCHOR_KINDS: Tuple[str, ...] = (
    "name", "birthday", "time", "place", "meet", "commitment",
    "money", "address", "identity", "family", "health", "personal",
)

# ── 强锚点 ────────────────────────────────────────────────────────────────────
# 自报姓名（与 L-4 F daily_learner 原 _NAME_HINT_RE 同式，迁到这里作单一来源）
_NAME_RE = re.compile(
    r"我叫|叫我|我的名字|名字是|我姓|"
    r"\b(?:my\s+name\s+is|call\s+me|i\s*am|i'm|this\s+is)\s+[A-Z][a-z]{1,15}\b",
    re.IGNORECASE,
)
_BIRTHDAY_RE = re.compile(
    r"生日|出生于|出生日期|出生在|\b(?:birthday|b-?day|born\s+on|date\s+of\s+birth)\b",
    re.IGNORECASE,
)
# 完整日期：必须带「日 / 号」或 ISO / 英文月名 + 日——「五月到十月」「几天到账」都不算
_DATE_RE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|"
    r"[一二三四五六七八九十]{1,2}月[一二三四五六七八九十]{1,3}[日号]|"
    r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
# 第一 / 二人称属性（「你们」「我们」不算——那是对业务方 / 群体说话）
_PERSONAL_RE = re.compile(
    r"(?:^|[^\w们們])(?:我|你)(?!们|們)(?:今年|现在|目前|以前)?\s*"
    r"(?:住在|住|来自|老家在|老家是|家在|\d{1,2}\s*岁|是\s*\d{1,2}\s*岁|"
    r"在\S{1,10}(?:工作|上班|读书|上学)|的?(?:老家|职业|工作是|名字|电话|手机号))|"
    r"\b(?:I|you)\s+(?:live|work|am\s+\d{1,2}\b|grew\s+up|was\s+born|were\s+born)\b|"
    r"\bI'?m\s+\d{1,2}\b|\bmy\s+(?:hometown|job|age|phone|number)\b",
    re.IGNORECASE,
)

# ── 弱信号（合取用） ──────────────────────────────────────────────────────────
_WEEKDAY_RE = re.compile(
    r"(?:周|星期|礼拜|週)[一二三四五六日天]|"
    r"\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)day\b",
    re.IGNORECASE,
)
_RELATIVE_DAY_RE = re.compile(
    r"今晚|今天晚上|明天|明早|明晚|后天|後天|下周|下週|下星期|下个月|下個月|这周末|這週末|周末|週末|月底|"
    r"\b(?:tonight|tomorrow|next\s+(?:week|month|weekend|friday|saturday|sunday|monday|tuesday|"
    r"wednesday|thursday)|this\s+(?:weekend|evening|afternoon))\b",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    r"(?:早上|上午|中午|下午|晚上|傍晚|凌晨)\s*[\d一二三四五六七八九十两]{1,3}\s*[点點]|"
    r"[\d一二三四五六七八九十两]{1,3}\s*[点點](?:半|[一二三四五]十|\d{1,2}\s*分)|"
    r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)\b|\b\d{1,2}\s*o'?clock\b",
    re.IGNORECASE,
)
_MEET_VERB_RE = re.compile(
    r"见吧|见啦|见面|碰面|碰头|等你|等我|来找|去找|来接|去接|接你|接我|出发|出發|一起(?:吃|喝|看|玩|去)|"
    r"吃饭|喝咖啡|看电影|逛街|约你|约我|约在|到了|我到|你到|飞过来|飞过去|"
    r"\b(?:meet|see\s+you|pick\s+(?:you|me)\s+up|dinner|lunch|brunch|coffee|hang\s+out|"
    r"come\s+over|be\s+there|i'?ll\s+be\s+at|wait\s+for\s+(?:you|me))\b",
    re.IGNORECASE,
)
_PLACE_RE = re.compile(
    r"星巴克|咖啡[厅馆店廳館]|餐厅|餐廳|饭店|飯店|酒店|宾馆|賓館|民宿|机场|機場|火车站|高铁站|地铁站|"
    r"车站|車站|公园|公園|商场|商場|广场|廣場|门口|門口|楼下|樓下|我家|你家|酒吧|KTV|电影院|電影院|"
    r"影院|海边|海邊|沙滩|沙灘|\b(?:starbucks|caf[eé]|coffee\s+shop|restaurant|hotel|airbnb|"
    r"airport|train\s+station|station|park|mall|bar|club|cinema|movie\s+theat(?:er|re)|beach|"
    r"my\s+place|your\s+place)\b",
    re.IGNORECASE,
)


def _time_anchor(t: str) -> bool:
    if _DATE_RE.search(t):
        return True
    clock = bool(_CLOCK_RE.search(t))
    day = bool(_WEEKDAY_RE.search(t) or _RELATIVE_DAY_RE.search(t))
    verb = bool(_MEET_VERB_RE.search(t))
    if clock and (day or verb):
        return True
    return day and (clock or verb)


def personal_anchors(text: str) -> List[str]:
    """文本里的「个人锚点」类别列表（按 ``ANCHOR_KINDS`` 顺序；空＝无锚点，可共享）。"""
    t = str(text or "")
    if not t.strip():
        return []
    hits = set()
    if _NAME_RE.search(t):
        hits.add("name")
    if _BIRTHDAY_RE.search(t):
        hits.add("birthday")
    if _time_anchor(t):
        hits.add("time")
    if _PLACE_RE.search(t) and ("time" in hits or _MEET_VERB_RE.search(t)):
        hits.add("place")
    try:
        hits.update(c for c in high_impact_categories(t) if c in ANCHOR_KINDS)
        if is_commitment(t):
            hits.add("commitment")
    except Exception:
        pass
    if _PERSONAL_RE.search(t):
        hits.add("personal")
    return [k for k in ANCHOR_KINDS if k in hits]


def memory_scope(text: str) -> Tuple[str, str]:
    """→ ``(scope, reason)``：有锚点 ``("customer", "birthday,time")``，否则
    ``("shared", "no_personal_anchor")``。日志 ``[episodic] scope=… reason=…`` 直接拼。"""
    kinds = personal_anchors(text)
    if kinds:
        return SCOPE_CUSTOMER, ",".join(kinds)
    return SCOPE_SHARED, REASON_NO_ANCHOR


def is_customer_private(text: str) -> bool:
    return bool(personal_anchors(text))


__all__ = [
    "ANCHOR_KINDS", "SCOPE_CUSTOMER", "SCOPE_SHARED", "REASON_NO_ANCHOR",
    "personal_anchors", "memory_scope", "is_customer_private",
]
