"""记忆「例外审核」打标（J-10 A2，#183 · 决策 D8 · 2026-09-05）——纯函数、零依赖、可单测。

D8：客户说的事实**自动记**，不设人审闸；人审只做**例外**——五类：
  ``conflict``（与同槽 stable 冲突，两条并列让人选，不自动覆盖）/
  ``high_impact``（金钱·见面·地址·健康·身份证件·家庭成员）/
  ``low_confidence``（LLM 置信低于阈，或推断句带「可能/也许/probably」一类模糊词）/
  ``self_fact``（人设/AI 自己的事被写进「关于客户」的库——Phase8 镜像事故风险）/
  ``commitment``（承诺/约定类：谁答应了什么、什么时候）。
目标日均 <5 条；其余条目不进队列、照常召回。敏感类标 ``impact=high`` 页面标红，
**不阻断记录**（陪聊场景漏记健康/见面信息代价更大）。

``conflict`` 需要查库（同槽 stable），由 ``EpisodicMemoryStore.add_fact`` 判定；本模块
只做文本级四类 + impact。词表刻意窄：多字词优先，单字（钱/病/元）只在有数字/上下文
时算，误标一条＝多让坐席看一眼，漏标一条＝少看一眼——都不是事故，但队列要小。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

REVIEW_CONFLICT = "conflict"
REVIEW_HIGH_IMPACT = "high_impact"
REVIEW_LOW_CONFIDENCE = "low_confidence"
REVIEW_SELF_FACT = "self_fact"
REVIEW_COMMITMENT = "commitment"
REVIEW_REASONS: Tuple[str, ...] = (
    REVIEW_CONFLICT, REVIEW_HIGH_IMPACT, REVIEW_LOW_CONFIDENCE,
    REVIEW_SELF_FACT, REVIEW_COMMITMENT,
)

IMPACT_HIGH = "high"
IMPACT_NORMAL = "normal"

STATUS_ACTIVE = "active"
STATUS_IGNORED = "ignored"
STATUSES: Tuple[str, ...] = (STATUS_ACTIVE, STATUS_IGNORED)

# LLM 置信低于此值 → low_confidence（memory.review.low_confidence_threshold 可配）
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.6

_HIGH_IMPACT_RE = re.compile(
    # 金钱
    r"转账|汇款|借钱|还钱|欠钱|欠款|贷款|房贷|车贷|工资|月薪|年薪|收入|投资|理财|存款|"
    r"块钱|多少钱|\d+\s*(?:元|块|万|美元|美金|刀|披索|泰铢|日元|韩元|欧元|英镑)|"
    r"\b(?:money|cash|transfer|remit|loan|debt|salary|wage|income|invest|savings|"
    r"pesos?|dollars?|usd|php|rmb|thb|jpy|krw|eur|gbp)\b|[$€£₱฿]\s*\d|\d\s*[$€£₱฿]|"
    # 见面
    r"见面|约见|见一面|来见我|去见你|来找我|去找你|来看我|去看你|接机|机场|订机票|买机票|飞过来|飞过去|"
    r"\b(?:meet(?:ing)?\s+(?:up|you|me|in\s+person)|meet\b|visit(?:ing)?\s+(?:you|me)|"
    r"come\s+over|fly(?:ing)?\s+(?:to|over)|see\s+you\s+in\s+person|airport|flight\s+to)|"
    # 地址（门牌级，不含城市级「住在上海」）
    r"地址|住址|门牌|街道|小区|公寓|房号|几号楼|\d+\s*(?:号楼|单元|栋|室)|"
    r"\b(?:address|street|apartment|apt\.?|unit\s*\d|block\s*\d|zip\s*code|postal)\b|"
    # 健康
    r"生病|病了|住院|手术|癌症|肿瘤|抑郁|焦虑症|医院|诊断|化疗|吃药|服药|怀孕|流产|"
    r"糖尿病|高血压|心脏病|哮喘|艾滋|HIV|自杀|自残|"
    r"\b(?:hospital|surgery|cancer|tumou?r|depress(?:ed|ion)|anxiety|pregnan(?:t|cy)|"
    r"miscarriage|diagnos(?:ed|is)|medication|chemo|diabet(?:es|ic)|sick|illness|disease|"
    r"therapy|suicid(?:e|al)|self-?harm)\b|"
    # 身份证件 / 账号
    r"身份证|护照|签证|证件|银行卡|卡号|账号|帐号|密码|社保|户口|"
    r"\b(?:passport|visa|id\s*card|national\s*id|bank\s*account|account\s*number|"
    r"password|pin\s*code|ssn|driver'?s?\s*licen[cs]e)\b|"
    # 家庭成员
    r"女儿|儿子|孩子|小孩|闺女|老公|老婆|丈夫|妻子|前夫|前妻|父母|爸爸|妈妈|母亲|父亲|"
    r"兄弟|姐妹|哥哥|弟弟|姐姐|妹妹|爷爷|奶奶|外公|外婆|孙子|孙女|"
    r"\b(?:daughters?|sons?|kids?|children|child|husband|wife|ex-?husband|ex-?wife|"
    r"parents?|mother|father|mom|mum|dad|brothers?|sisters?|siblings?|grand(?:ma|pa|mother|father|son|daughter))\b",
    re.IGNORECASE,
)

_COMMITMENT_RE = re.compile(
    r"承诺|答应|约好|说好|约定|保证|发誓|一定会|"
    r"会(?:来|去|发|转|还|带|给|打电话|联系|过来|过去)|"
    r"(?:下周|下个月|明天|后天|周末|这周|月底|年底)[^，。！？\n]{0,12}(?:见|来|去|发|转|还|给|打|聊)|"
    r"\b(?:promis(?:e|ed|es)|swear|agreed\s+to|committed\s+to|"
    r"(?:will|'ll|gonna|going\s+to)\s+(?:come|visit|send|pay|call|meet|bring|transfer|return|see\s+you)|"
    r"(?:next|this)\s+(?:week|month|weekend|friday|saturday|sunday|monday)[^.!?\n]{0,20}"
    r"(?:meet|come|visit|see|send|pay|call))\b",
    re.IGNORECASE,
)

# 推断句里的模糊词：ai_inferred + 命中 → low_confidence（不看数值置信也能兜住）
_HEDGE_RE = re.compile(
    r"可能|也许|大概|好像|似乎|应该是|估计|或许|疑似|貌似|说不定|"
    r"\b(?:maybe|probably|perhaps|seems?|might|likely|possibly|presumably|apparently)\b",
    re.IGNORECASE,
)

# 第一人称开头（剥掉引号/空白后）＝人设自己的事混进了「关于客户」的库
_SELF_FACT_RE = re.compile(
    r"^[\"“「『\s]*(?:我(?!们?(?:的客户|方))|I(?:'m|'ve|'ll|\s+am|\s+have|\s+live|\s+work)\b|My\s)",
)
_CUSTOMER_SUBJECT_RE = re.compile(r"^[\"“「『\s]*(?:用户|客户|对方|他|她|TA|customer|user)\b", re.IGNORECASE)


# J-10 B1：客户记忆时间线的人话分组（basic 基本信息 / family 关系与家人 / pref 偏好与习惯 /
# event 近期事件与约定 / other）。纯展示用，不进任何判定；关系/事件优先于基本信息，
# 免得「结婚」「搬家」被归到基本信息。
TIMELINE_GROUPS: Tuple[str, ...] = ("basic", "family", "pref", "event", "other")
_TL_FAMILY_RE = re.compile(
    r"女儿|儿子|孩子|小孩|闺女|老公|老婆|丈夫|妻子|前夫|前妻|父母|爸|妈|兄|弟|姐|妹|家人|亲戚|"
    r"男朋友|女朋友|对象|婚|单身|恋爱|分手|宠物|养了|猫|狗|"
    r"\b(?:daughter|sons?|kids?|child(?:ren)?|husband|wife|parents?|mother|father|brother|sister|"
    r"married|divorce|single|boyfriend|girlfriend|pets?|dog|cat)\b", re.IGNORECASE)
_TL_EVENT_RE = re.compile(
    r"明天|后天|下周|下个月|周末|这周|月底|约好|约定|见面|承诺|答应|计划|打算|准备|要去|会来|会去|"
    r"生病|住院|手术|搬家|出差|旅行|旅游|考试|面试|辞职|换工作|买房|买车|转账|还钱|借钱|"
    r"\b(?:meet|promis|plan|next\s+week|tomorrow|weekend|trip|travel|hospital|surgery|moving|exam|interview)",
    re.IGNORECASE)
_TL_BASIC_RE = re.compile(
    r"自称|叫我|名字|昵称|称呼|岁|年龄|生日|出生|住在|居住|来自|老家|家在|城市|职业|工作|上班|"
    r"是.{0,4}(?:护士|老师|医生|工程师|司机|老板|学生|设计师)|学历|大学|身高|体重|星座|属相|电话|手机|微信|地址|"
    r"\b(?:name|call\s+me|age|years?\s+old|birthday|born|lives?\s+in|from|city|works?|job|occupation|"
    r"student|teacher|nurse|doctor|engineer|address|phone)\b", re.IGNORECASE)
_TL_PREF_RE = re.compile(
    r"喜欢|喜爱|爱吃|爱喝|爱看|爱听|讨厌|受不了|习惯|经常|总是|每天|每周|偏好|口味|爱好|兴趣|最爱|想吃|想去|"
    r"\b(?:likes?|loves?|hates?|prefers?|usually|always|every\s+(?:day|week)|favou?rite|hobby|interest|enjoys?)\b",
    re.IGNORECASE)


def timeline_group(content: str) -> str:
    t = str(content or "")
    if _TL_FAMILY_RE.search(t):
        return "family"
    if _TL_EVENT_RE.search(t):
        return "event"
    if _TL_BASIC_RE.search(t):
        return "basic"
    if _TL_PREF_RE.search(t):
        return "pref"
    return "other"


def is_high_impact(content: str) -> bool:
    return bool(_HIGH_IMPACT_RE.search(str(content or "")))


def is_commitment(content: str) -> bool:
    t = str(content or "")
    if _COMMITMENT_RE.search(t):
        return True
    # 复用出站媒体承诺守卫词表（「等我拍张给你」类即时承诺）——纯函数，失败不算命中
    try:
        from src.ai.outbound_promise_guard import detect_media_promise
        return bool(detect_media_promise(t))
    except Exception:
        return False


def is_hedged(content: str) -> bool:
    return bool(_HEDGE_RE.search(str(content or "")))


def is_self_fact(content: str, author: str = "") -> bool:
    """人设/AI 自己的事：显式 ``author`` 为 human/ai/persona，或第一人称开头且不带客户主语。"""
    a = str(author or "").strip().lower()
    if a in ("human", "ai", "persona", "self"):
        return True
    t = str(content or "")
    if _CUSTOMER_SUBJECT_RE.search(t):
        return False
    return bool(_SELF_FACT_RE.search(t))


def classify_fact(
    content: str,
    *,
    source: str = "user_stated",
    confidence: Optional[float] = None,
    author: str = "",
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
) -> Tuple[str, str]:
    """→ ``(review_reason, impact)``；不需要人看时 review_reason 为空串。

    优先级（单一原因）：self_fact > commitment > high_impact > low_confidence。
    ``conflict`` 由 store 在查到同槽 stable 时覆盖为最高优先级。impact 与原因独立：
    命中敏感词表即 ``high``（页面标红），哪怕原因是别的。
    """
    text = str(content or "")
    impact = IMPACT_HIGH if is_high_impact(text) else IMPACT_NORMAL
    if is_self_fact(text, author):
        return REVIEW_SELF_FACT, impact
    if is_commitment(text):
        return REVIEW_COMMITMENT, impact
    if impact == IMPACT_HIGH:
        return REVIEW_HIGH_IMPACT, impact
    low = False
    if confidence is not None:
        try:
            low = float(confidence) < float(low_confidence_threshold)
        except (TypeError, ValueError):
            low = False
    if not low and str(source or "") == "ai_inferred" and is_hedged(text):
        low = True
    if low:
        return REVIEW_LOW_CONFIDENCE, impact
    return "", impact


__all__ = [
    "REVIEW_CONFLICT", "REVIEW_HIGH_IMPACT", "REVIEW_LOW_CONFIDENCE",
    "REVIEW_SELF_FACT", "REVIEW_COMMITMENT", "REVIEW_REASONS",
    "IMPACT_HIGH", "IMPACT_NORMAL", "STATUS_ACTIVE", "STATUS_IGNORED", "STATUSES",
    "DEFAULT_LOW_CONFIDENCE_THRESHOLD", "TIMELINE_GROUPS",
    "is_high_impact", "is_commitment", "is_hedged", "is_self_fact", "classify_fact",
    "timeline_group",
]
