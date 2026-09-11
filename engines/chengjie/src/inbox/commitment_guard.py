# -*- coding: utf-8 -*-
"""现实承诺守卫（Q-2 #263 #264 · D-Q3，2026-09-10）。

事故（PHBRBW / CBD4J5 / XBGPBN）：客户约周末见面，起草「Saturday noon sounds lovely,
I'll make sure to have some fresh tea ready」``risk=low shadow=-`` 自动发出；下一句
「Just need your address」才命中 privacy；被质问没发图后又「you're right, I completely
forgot… later today, promise」。词表不认见面、无人设政策、底稿无可行性——三处一起把
「答应上门」放行了。

本模块五类意图 ``meet / contact / media / gift / money``：入站检测 → 按人设
``boundaries.meeting_policy``（默认 **never**）从话术库选委婉延后；出站
``commitment_claim`` / ``self_blame_repromise`` 改写拒绝（与 P-3 ``media_claim`` 并成
:data:`CLAIM_KINDS`，照片兑现不重做）。第二次坚持转人工。

存储：InboxStore ``app_settings`` KV ``commitment_hit:<cid>``（24h 账本），**不动
store.py**。``risk_hold.set`` 用 ``hasattr`` 兜底（接口约定②）。绝不抛。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KINDS = ("meet", "contact", "media", "gift", "money")
#: 更具体的优先（钱 > 礼物 > 地址电话 > 媒体 > 见面）
KIND_PRIORITY = ("money", "gift", "contact", "media", "meet")
POLICIES = ("never", "after_months", "handoff")
DEFAULT_POLICY = "never"
DEFAULT_MONTHS = 3
DEFAULT_STYLE = "soft"  # soft=委婉 / direct=直接
HIT_TTL_SEC = 24 * 3600.0
KV_PREFIX = "commitment_hit:"
SECONDS_PER_MONTH = 30.44 * 86400.0

_LB = r"(?<![A-Za-z0-9_])"
_RB = r"(?![A-Za-z0-9_])"


def _rx(pat: str) -> "re.Pattern[str]":
    return re.compile(pat, re.IGNORECASE | re.DOTALL)


# ── 入站：五类词表（三语）+ 句式 ──────────────────────────────────────────
# 强匹配＝短语本身就是邀约/索要，不依赖疑问词。弱匹配要疑问/请求/确认框。

_MEET_STRONG = [_rx(p) for p in (
    _LB + r"(?:come\s+over|come\s+by|come\s+round|drop\s+by|come\s+thru|come\s+through)" + _RB,
    _LB + r"(?:meet\s+up|meet\s+me|meet\s+in\s+person|in[\s-]?person\s+meet)" + _RB,
    _LB + r"(?:visit\s+me|visit\s+you|come\s+see\s+me|come\s+see\s+you)" + _RB,
    r"上门|来找你|来找我|来我家|去你家|到你家|到我家|来你那|去你那|来我这|来我這",
    r"见个面|見個面|见一面|見一面|出来见面|出來見面|约出来|約出來|约见面|約見面",
    r"会いたい|会いましょう|会おうよ|会える[？?]",
)]
_MEET_WEAK = [_rx(p) for p in (
    r"见面|見面",
    _LB + r"(?:see\s+you|meet)\s+(?:on\s+)?(?:this\s+weekend|saturday|sunday|tonight|tomorrow|mon|tue|wed|thu|fri|sat|sun)" + _RB,
    r"会いたい|会える|会おう",
)]
_MEET_EXCLUDE = [_rx(p) for p in (
    r"见了|見過|见过|见到老板|見了老闆|见了同事|见了朋友",
    _LB + r"(?:i|we|she|he|they)\s+(?:met|saw|visited|saw)\b",
    r"见面礼|見面禮",
    r"email\s+address|mail\s+address",
)]

_CONTACT_STRONG = [_rx(p) for p in (
    _LB + r"(?:your|home|shipping|mailing|delivery)\s+address" + _RB,
    _LB + r"(?:phone|cell|mobile|whatsapp)\s+(?:number|no\.?|#)" + _RB,
    _LB + r"(?:what(?:'s|s|\s+is)\s+your\s+(?:number|addr))" + _RB,
    _LB + r"(?:text|send)\s+me\s+your\s+(?:address|number|phone)" + _RB,
    _LB + r"just\s+need\s+your\s+address" + _RB,
    r"你(的)?地址|你家地址|收货地址|收貨地址|住址|门牌|住所",
    r"手机号|手機號|电话号码|電話號碼|電話番号|微信号|微訊號|加微信|加我微信|line\s*id|ライン[アｱ]イ[デﾃﾞ]",
)]
_CONTACT_EXCLUDE = [_rx(p) for p in (
    r"e-?mail\s+address|email\s+address|mail\s+address",
    r"邮箱|郵箱|邮件地址|郵件地址",
    r"ip\s+address|mac\s+address|wallet\s+address",
)]

_MEDIA_STRONG = [_rx(p) for p in (
    _LB + r"(?:send|sent)\s+(?:me\s+)?(?:a\s+|some\s+|more\s+)?(?:pic|pics|photo|photos|picture|pictures|selfie)s?" + _RB,
    _LB + r"(?:video\s+call|facetime|face\s*time|voice\s+call|zoom\s+call)" + _RB,
    r"发张照片|發張照片|来张自拍|來張自拍|发个自拍|發個自拍|看看你的照片|发照片",
    r"视频通话|視頻通話|打[个個]?视频|打[個个]?視頻|开视频|開視頻|语音通话|語音通話|打[个個]?语音|打[個个]?語音",
    r"写真[をを]?送|通話しよう|ビデオ通話",
)]
_MEDIA_VIDEO = [_rx(p) for p in (
    _LB + r"(?:video\s+call|facetime|face\s*time|voice\s+call)" + _RB,
    r"视频通话|視頻通話|打[个個]?视频|打[個个]?視頻|开视频|開視頻|语音通话|語音通話|打[个個]?语音|ビデオ通話",
)]

_GIFT_STRONG = [_rx(p) for p in (
    _LB + r"(?:send|mail|post|ship)\s+(?:you\s+)?(?:a\s+|the\s+)?(?:gift|package|parcel|present)" + _RB,
    _LB + r"(?:buy|got)\s+you\s+(?:a\s+)?(?:gift|present)" + _RB,
    r"寄(给|給)你|送你礼物|送你禮物|给你寄|給你寄|收货地址.{0,6}寄|寄.{0,6}收货",
    r"プレゼント|贈り物を送",
)]
_GIFT_WEAK = [_rx(p) for p in (
    r"礼物|禮物|寄过来|寄過來|快递|快遞",
    r"プレゼント|贈り物",
)]

_MONEY_STRONG = [_rx(p) for p in (
    _LB + r"(?:send|wire|transfer)\s+(?:me\s+)?(?:some\s+|the\s+)?money" + _RB,
    _LB + r"(?:cash\s*app|venmo|zelle|paypal\s+me|send\s+cash)" + _RB,
    r"转账给我|轉賬給我|转账给你|打钱|打錢|借钱|借錢|红包|紅包|给点钱|給點錢|汇点钱",
    r"送金して|お金貸",
)]

_REQUEST_FRAME = _rx(
    r"(?:"
    r"[?？]"
    r"|" + _LB + r"(?:can|could|would|will|wanna|want\s+to|let'?s|please|pls|"
    r"how\s+about|why\s+don'?t|do\s+you\s+want|you\s+want\s+me|"
    r"you\s+really\s+want(?:\s+me)?)" + _RB +
    r"|要不要|能不能|可以吗|可以嗎|好不好|来不来|來不來|见不见|見不見|"
    r"约不约|約不約|行不行|好嘛|好吗|好嗎"
    r"|ください|してくれる|しよう"
    r")"
)
_CONFIRM_FRAME = _rx(
    r"(?:"
    r"you\s+really\s+want\s+me\s+to"
    r"|so\s+we(?:'re|\s+are)\s+(?:meeting|coming)"
    r"|你真的(要|想)(来|來|见|見)"
    r"|那就(见面|見面|过来|過來)"
    r")"
)

# ── 出站：答应 / 自责再承诺 ─────────────────────────────────────────────
_CLAIM_MEET = [_rx(p) for p in (
    r"sounds?\s+lovely",
    r"see\s+you\s+(?:on\s+)?(?:this\s+weekend|saturday|sunday|tonight|tomorrow|"
    r"monday|tuesday|wednesday|thursday|friday|sat|sun)\b",
    r"i'?ll\s+(?:make\s+sure\s+to\s+)?have\s+.{0,40}\s+ready",
    r"i'?ll\s+be\s+waiting",
    r"it'?s\s+a\s+date",
    r"can'?t\s+wait\s+to\s+(?:see|meet)\s+you",
    r"i(?:'?ll|\s+will)\s+(?:come\s+over|come\s+by|meet\s+you)",
    r"到时见|到時見|到時候見|那到时见|我等你来|我等你來|我过来找你|我過來找你",
)]
_CLAIM_CONTACT = [_rx(p) for p in (
    r"my\s+address\s+is",
    r"i'?ll\s+text\s+you\s+my\s+address",
    r"i'?ll\s+(?:send|give|text)\s+you\s+(?:my\s+)?(?:address|number|phone)",
    r"我地址是|我的地址是|给你地址|給你地址|发你地址|發你地址|我电话是|我電話是",
)]
_CLAIM_GIFT = [_rx(p) for p in (
    r"i'?ll\s+(?:mail|ship|send)\s+(?:you\s+)?(?:a\s+)?(?:gift|package|present)",
    r"我(给你|給你)寄",
)]
_CLAIM_MONEY = [_rx(p) for p in (
    r"i'?ll\s+(?:send|wire|transfer)\s+(?:you\s+)?(?:the\s+|some\s+)?money",
    r"我(转账|轉賬|打钱|打錢)",
)]
_CLAIM_MEDIA_OUT = [_rx(p) for p in (
    r"i'?ll\s+(?:send|grab|take)\s+(?:you\s+)?(?:a\s+|some\s+|those\s+)?(?:pic|photo|selfie)",
    r"我(发|發)(张|張)?(照片|自拍)",
)]

_BLAME_ADMIT = _rx(
    r"(?:you'?re\s+right|your\s+right|my\s+bad|completely\s+forgot|i\s+forgot|"
    r"i\s+totally\s+forgot|slip(?:ped)?\s+my\s+mind|"
    r"我忘了|是我不好|怪我|完全忘了|忘光了)"
)
_BLAME_PROMISE = _rx(
    r"(?:i\s+promise|later\s+today|next\s+time|i'?ll\s+make\s+sure|"
    r"i'?ll\s+(?:grab|send|get)\s+(?:them|it|those)|"
    r"下次一定|待会(就|发给|發給)|今晚一定|回头就|回頭就|马上发|馬上發)"
)


def sniff_lang(text: str, lang: Optional[str] = None) -> str:
    raw = str(lang or "").strip().lower()
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("ja"):
        return "ja"
    if raw.startswith("en"):
        return "en"
    t = str(text or "")
    if re.search(r"[\u3040-\u30ff]", t):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", t):
        return "zh"
    return "en"


def _any(pats: List["re.Pattern[str]"], text: str) -> bool:
    return any(p.search(text) for p in pats)


def _framed(text: str) -> bool:
    return bool(_REQUEST_FRAME.search(text) or _CONFIRM_FRAME.search(text))


def detect_commitment(inbound_text: str, lang: Optional[str] = None) -> Optional[str]:
    """入站是否在邀约/索要现实动作。返回 ``meet|contact|media|gift|money`` 或 None。

    疑问 / 请求 / 确认句式（「you really want me to…」）与强短语并判。
    「我今天见了老板」这类过去陈述不算。
    """
    t = str(inbound_text or "").strip()
    if not t:
        return None
    _ = sniff_lang(t, lang)  # 词表三语写在同一组正则里，lang 仅供话术侧
    if _any(_MEET_EXCLUDE, t) and not _any(_CONTACT_STRONG, t) and not _any(_MONEY_STRONG, t):
        # 过去见人的陈述：除非同时在要地址/钱，否则不当 meet
        meet_blocked = True
    else:
        meet_blocked = False
    hits: List[str] = []
    if _any(_MONEY_STRONG, t):
        hits.append("money")
    if _any(_GIFT_STRONG, t) or (_any(_GIFT_WEAK, t) and _framed(t)):
        hits.append("gift")
    if _any(_CONTACT_STRONG, t) and not _any(_CONTACT_EXCLUDE, t):
        hits.append("contact")
    if _any(_MEDIA_STRONG, t):
        hits.append("media")
    if not meet_blocked and (
            _any(_MEET_STRONG, t) or (_any(_MEET_WEAK, t) and _framed(t))):
        hits.append("meet")
    for k in KIND_PRIORITY:
        if k in hits:
            return k
    return None


def detect_commitment_claim(outbound_text: str) -> Optional[str]:
    """出站是否在**答应**见面/给地址/寄礼物/打钱/稍后发图。返回 kind 或 None。"""
    t = str(outbound_text or "").strip()
    if not t:
        return None
    if _any(_CLAIM_MONEY, t):
        return "money"
    if _any(_CLAIM_GIFT, t):
        return "gift"
    if _any(_CLAIM_CONTACT, t):
        return "contact"
    if _any(_CLAIM_MEDIA_OUT, t):
        return "media"
    if _any(_CLAIM_MEET, t):
        return "meet"
    return None


# ── Q-17 #277②：客户侧「索要句式」（比 detect_commitment 严：只认对**我们**开口要）──
# detect_commitment 的强短语（your address / phone number / sent me pictures）把客户的**叙述**
# （「they asked for my phone number」「pictures they have sent me」）也当邀约 → 全部 high →
# 「需人工」。本表只收「发我 / 给我 / can I have / what's your / show me yours / 你的电话 / 你住哪」
# 这类**指向对方**的索要框架 + 对象词；过去时（sent / asked）不收。返回 kind 与 detect_commitment 同域。
_REQ_CONTACT = [_rx(p) for p in (
    _LB + r"(?:send|give|text|tell|share|drop|shoot)\s+me\s+(?:your\s+)?(?:address|number|phone|cell|mobile|whatsapp|wechat|line|insta(?:gram)?|snap(?:chat)?|telegram|contact)" + _RB,
    _LB + r"what(?:'s|s|\s+is)\s+your\s+(?:address|number|phone|cell|mobile|whatsapp|wechat|line|insta(?:gram)?|snap(?:chat)?)" + _RB,
    _LB + r"(?:can|could|may)\s+i\s+(?:have|get)\s+your\s+(?:address|number|phone|cell|whatsapp|wechat|line|insta(?:gram)?)" + _RB,
    _LB + r"where\s+do\s+you\s+live" + _RB,
    _LB + r"(?:just\s+)?need\s+your\s+(?:address|number|phone)" + _RB,
    _LB + r"(?:add|follow)\s+(?:me\s+on\s+)?(?:your\s+)?(?:whatsapp|wechat|line|insta(?:gram)?|snap(?:chat)?|telegram)" + _RB,
    r"你住哪|你住在哪|你家在哪|你(的)?(电话|手机号|微信|地址|住址|微信号|line)(是)?(多少|发我|給我|给我|告诉我|是啥|是什么)",
    r"(发|給|给|告诉)我你的(地址|电话|手机号|微信|住址)|加(个|個)?微信|把你(的)?(地址|电话|手机号|微信)(发|给)我",
    r"住所教えて|電話番号(教えて|くれる)|ライン(教えて|交換)|どこに住んで",
)]
_REQ_MEDIA = [_rx(p) for p in (
    _LB + r"(?:send|show|give|shoot|drop)\s+me\s+(?:a\s+|some\s+|another\s+|more\s+|your\s+|ur\s+)?(?:pic|pics|photo|photos|picture|pictures|selfie|selfies|video|videos|face)s?" + _RB,
    _LB + r"show\s+me\s+yours" + _RB,
    _LB + r"(?:can|could|may)\s+i\s+(?:see|have|get)\s+(?:a\s+|some\s+|your\s+|ur\s+)?(?:pic|pics|photo|photos|picture|pictures|selfie|face|video)s?" + _RB,
    _LB + r"(?:can\s+we|let'?s|wanna|want\s+to)\s+(?:video\s+call|facetime|face\s*time|voice\s+call|zoom)" + _RB,
    r"发(我|张|個|个|几张)(照片|自拍|视频|照)|给我(看|发)(张|个|你的|几张)?(照片|自拍|视频)|看看你的(照片|脸|视频)|来张自拍|來張自拍",
    r"打(个|個)?(视频|視頻|语音|語音)|开视频|開視頻|视频(一下|聊|通话)",
    r"写真(を)?送って|自撮り送って|ビデオ通話(しよう|しない)",
)]
_REQ_MEET = [_rx(p) for p in (
    _LB + r"(?:can|could|shall|should)\s+we\s+.{0,24}?(?:meet|meet\s+up|hang\s+out|get\s+together|grab\s+(?:a\s+)?(?:coffee|drink|dinner|lunch|bite))" + _RB,
    _LB + r"(?:let'?s|wanna|want\s+to|do\s+you\s+want\s+to|would\s+you\s+like\s+to)\s+.{0,16}?(?:meet|meet\s+up|hang\s+out|come\s+over|grab\s+(?:a\s+)?(?:coffee|drink|dinner|lunch))" + _RB,
    _LB + r"(?:come\s+over|come\s+to\s+my\s+place|meet\s+me)" + _RB,
    r"要不要(出来)?见(个|一)?面|出来见(个)?面|见个面|约(你|个)?(吃饭|见面|出来)|来我家|去你家|上门|见一面",
    r"会いたい|会おう|会える\?|会いましょう",
)]
_REQ_GIFT = [_rx(p) for p in (
    _LB + r"(?:can|could|let\s+me|want\s+to|wanna|i'?d\s+like\s+to)\s+.{0,10}?(?:send|mail|ship)\s+you\s+(?:a\s+|the\s+)?(?:gift|present|package|parcel)" + _RB,
    _LB + r"(?:send|mail|ship)\s+you\s+(?:a\s+)?(?:gift|present|package).{0,20}?address" + _RB,
    r"寄(给|給)你(个|一个|份)?(礼物|禮物|东西)|送你(个)?礼物|给你寄",
    r"プレゼント(を)?送りたい|贈り物を送って",
)]
_REQ_MONEY = [_rx(p) for p in (
    _LB + r"(?:send|wire|transfer|lend|loan|give|spare)\s+me\s+(?:some\s+|a\s+little\s+|the\s+)?(?:money|cash|\$\s*\d+|\d+\s*(?:dollars|bucks|usd|euros?))" + _RB,
    _LB + r"(?:cash\s*app|venmo|zelle|paypal)\s+me" + _RB + r"|" + _LB + r"(?:your\s+)?(?:cash\s*app|venmo|zelle)\s*(?:\?|tag|handle|name)",
    _LB + r"(?:can|could)\s+you\s+(?:send|lend|loan|spare|wire|transfer)\s+me\s+(?:some\s+)?(?:money|cash|\$)",
    _LB + r"(?:help\s+me\s+(?:out\s+)?with\s+(?:some\s+)?(?:money|cash|rent|bills?))" + _RB,
    r"转账给我|轉賬給我|打钱给我|打錢給我|借(我|点|點)钱|借(我|点|點)錢|给(点|點)钱|給(點|点)錢|发(个|個)红包|發(個|个)紅包|汇(点|點)钱给我",
    r"送金して|お金貸して|お金送って",
)]


def detect_request(inbound_text: str, lang: Optional[str] = None) -> Optional[str]:
    """Q-17 #277②：客户是否在**向我们索要** meet / contact / media / gift / money（只认索要句式）。

    与 ``detect_commitment`` 的差别：后者把「your address / phone number / sent me pictures」这类
    强短语也当邀约（客户叙述「they asked for my phone number」「pictures they have sent me」也 high）；
    本函数只认「send me / give me / can I have / what's your / show me yours / 发我 / 给我 / 你的电话 /
    你住哪」等**指向对方**的索要框架。过去时叙述 / 第三人称叙述不算。返回 kind（同 ``KINDS``）或 None；
    多类并中按 ``KIND_PRIORITY``（钱 > 礼物 > 地址电话 > 媒体 > 见面）。
    """
    t = str(inbound_text or "").strip()
    if not t:
        return None
    _ = sniff_lang(t, lang)
    hits: List[str] = []
    if _any(_REQ_MONEY, t):
        hits.append("money")
    if _any(_REQ_GIFT, t):
        hits.append("gift")
    if _any(_REQ_CONTACT, t) and not _any(_CONTACT_EXCLUDE, t):
        hits.append("contact")
    if _any(_REQ_MEDIA, t):
        hits.append("media")
    if _any(_REQ_MEET, t) and not _any(_MEET_EXCLUDE, t):
        hits.append("meet")
    for k in KIND_PRIORITY:
        if k in hits:
            return k
    return None


def detect_self_blame_repromise(outbound_text: str) -> bool:
    """被质问后「you're right / 我忘了 / 下次一定」——承认失误并再承诺。

    Q-1 自证句（``I promise I'm not a robot``）不当本类——交给 exposure_guard。
    """
    t = str(outbound_text or "").strip()
    if not t:
        return False
    if re.search(r"(?:not a robot|i'?m (?:a )?real (?:person|human)|我是真人|我不是机器人|我不是機器人)",
                 t, re.I) and not re.search(r"forgot|later today|下次一定|今晚一定", t, re.I):
        return False
    return bool(_BLAME_ADMIT.search(t) and _BLAME_PROMISE.search(t))


def is_video_call_ask(text: str) -> bool:
    return _any(_MEDIA_VIDEO, str(text or ""))


# ── 人设政策 ────────────────────────────────────────────────────────────
def meeting_policy_of(persona: Any) -> Tuple[str, int]:
    """``(never|after_months|handoff, months)``。缺省 / 脏值 → never。"""
    raw = ""
    months = DEFAULT_MONTHS
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        raw = str((b or {}).get("meeting_policy") or "").strip().lower()
    except Exception:
        raw = ""
    if raw.startswith("after_months"):
        rest = raw.split(":", 1)[-1] if ":" in raw else ""
        try:
            months = int(str(rest).strip() or DEFAULT_MONTHS)
        except (TypeError, ValueError):
            months = DEFAULT_MONTHS
        months = max(1, min(months, 36))
        return "after_months", months
    if raw == "handoff":
        return "handoff", months
    return DEFAULT_POLICY, months


def commitment_style_of(persona: Any) -> str:
    raw = ""
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        raw = str((b or {}).get("commitment_style") or "").strip().lower()
    except Exception:
        raw = ""
    if raw in ("direct", "直接", "blunt"):
        return "direct"
    return DEFAULT_STYLE


def conversation_age_months(store: Any, cid: str, now: Optional[float] = None) -> float:
    ts = float(now if now is not None else time.time())
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "get_conversation"):
        return 0.0
    try:
        conv = store.get_conversation(cid) or {}
        created = float(conv.get("created_at") or conv.get("created_ts") or 0.0)
    except Exception:
        return 0.0
    if created <= 0:
        return 0.0
    return max(0.0, (ts - created) / SECONDS_PER_MONTH)


def effective_policy(persona: Any, *, store: Any = None, conversation_id: str = "",
                     now: Optional[float] = None) -> str:
    """after_months:N → 会话龄 < N 月当 never，≥ N 月当 handoff（AI 永不接受见面）。"""
    policy, months = meeting_policy_of(persona)
    if policy != "after_months":
        return policy
    age = conversation_age_months(store, conversation_id, now)
    return "handoff" if age >= float(months) else "never"


# ── 话术库 ──────────────────────────────────────────────────────────────
_REFUSE: Dict[str, Dict[str, Dict[str, List[str]]]] = {
    "meet": {
        "en": {
            "soft": [
                "I'm not ready to meet just yet, let's keep getting to know each other",
                "Not yet, I don't want to rush this",
                "maybe another time, I'm happier just texting for now",
            ],
            "direct": [
                "I don't meet people from here",
                "nah I don't do meetups",
                "not happening, let's stay on here",
            ],
        },
        "zh": {
            "soft": [
                "我还没准备好见面，等我们更熟一点再说",
                "先这样聊着吧，见面我还没准备好",
                "暂时不想见面，我们再多聊聊",
            ],
            "direct": [
                "我不跟网上认识的人见面",
                "见面就算了，继续聊天就好",
                "不去，就在这聊",
            ],
        },
        "ja": {
            "soft": [
                "まだ会うのは早い気がする、もう少しこのまま話そう",
                "今は会う準備できてない、チャットでいいかな",
                "もう少し仲良くなってからで",
            ],
            "direct": [
                "ネットの人とは会わないんだ",
                "会うのはなし、ここで話そう",
                "会わない、チャットだけ",
            ],
        },
    },
    "contact": {
        "en": {
            "soft": [
                "I don't share my number on here",
                "I keep my address private, let's just chat here",
                "not giving that out, hope that's ok",
            ],
            "direct": [
                "I don't give my address or number here",
                "nope, not sharing contact info",
                "I don't do that on here",
            ],
        },
        "zh": {
            "soft": [
                "地址电话我不在这给，咱们就在这聊",
                "联系方式我不太外传，先在这说说话",
                "这个我不方便给，别介意",
            ],
            "direct": [
                "地址电话不给",
                "不留联系方式",
                "这上面不给私人信息",
            ],
        },
        "ja": {
            "soft": [
                "番号も住所もここでは教えないんだ",
                "連絡先は控えてる、ここで話そう",
                "それはちょっと渡せない",
            ],
            "direct": [
                "住所も番号も出さない",
                "連絡先は教えない",
                "ここでは個人情報出さない",
            ],
        },
    },
    "media": {
        "en": {
            "soft": [
                "not sending pics right now",
                "I don't really do video calls, text is better",
                "nah not tonight",
            ],
            "direct": [
                "I don't send pics on here",
                "no video calls",
                "not sending that",
            ],
        },
        "zh": {
            "soft": [
                "今晚不行，照片先不发",
                "视频就不打了，打字聊吧",
                "这个先不发",
            ],
            "direct": [
                "不发照片",
                "不打视频",
                "不发",
            ],
        },
        "ja": {
            "soft": [
                "今は写真送らない",
                "通話はしない、文字で話そう",
                "今夜はなし",
            ],
            "direct": [
                "写真は送らない",
                "ビデオ通話しない",
                "送らない",
            ],
        },
    },
    "gift": {
        "en": {
            "soft": [
                "that's sweet but I don't do gifts over this",
                "let's not do packages, chatting is enough",
                "I don't share a shipping address, but thank you",
            ],
            "direct": [
                "I don't do gifts on here",
                "no packages, no address",
                "don't send me stuff",
            ],
        },
        "zh": {
            "soft": [
                "心意领了，东西就别寄了，地址我也不留",
                "别寄东西，咱们聊天就好",
                "收货地址不给，谢谢你想着",
            ],
            "direct": [
                "不寄东西，也不给地址",
                "礼物就免了",
                "别寄",
            ],
        },
        "ja": {
            "soft": [
                "気持ちは嬉しいけど送り物はなしで",
                "住所は出せない、話してるだけでいい",
                "荷物は送らないで",
            ],
            "direct": [
                "贈り物はしない",
                "住所は出さない",
                "送らないで",
            ],
        },
    },
    "money": {
        "en": {
            "soft": [
                "I don't do money stuff on here",
                "let's not mix cash into this",
                "I don't send or take money in chat",
            ],
            "direct": [
                "no money stuff",
                "I don't do cash on here",
                "not sending money",
            ],
        },
        "zh": {
            "soft": [
                "钱的事我不在这搞",
                "转账借钱都不弄，聊天归聊天",
                "这上面不碰钱",
            ],
            "direct": [
                "不转账不借钱",
                "钱的事免谈",
                "不搞钱",
            ],
        },
        "ja": {
            "soft": [
                "お金の話はここではしない",
                "送金も借りもなし、話だけ",
                "金銭はからめない",
            ],
            "direct": [
                "お金は扱わない",
                "送金しない",
                "金の話はなし",
            ],
        },
    },
}

_RETRACT = {
    "en": [
        "I spoke too fast just now. I'm not actually ready to meet, please don't be mad, let's take it slow",
        "wait, I shouldn't have said that. I'm not ready for that, let's stay here",
    ],
    "zh": [
        "刚才说得太快，其实我还没准备好见面，别生气，我们慢慢来",
        "刚才那句收回，我还没准备好，咱们继续这样聊",
    ],
    "ja": [
        "さっき言いすぎた。まだ会う準備できてない、怒らないでゆっくりいこう",
        "今のなし。まだそのつもりない、このまま話そう",
    ],
}

_CASUAL_REFUSE = {
    "en": ["nah not tonight", "I don't do that on here", "not sending pics right now"],
    "zh": ["今晚不行", "这上面不弄这个", "先不发"],
    "ja": ["今夜はなし", "ここではやらない", "今は送らない"],
}


def _pool(kind: str, lang: str, style: str) -> List[str]:
    k = kind if kind in _REFUSE else "meet"
    lg = lang if lang in ("en", "zh", "ja") else "en"
    st = style if style in ("soft", "direct") else "soft"
    rows = ((_REFUSE.get(k) or {}).get(lg) or {}).get(st) or []
    if rows:
        return list(rows)
    return list(((_REFUSE.get(k) or {}).get("en") or {}).get("soft") or ["not yet"])


def pick_refuse_line(kind: str, lang: Optional[str] = None, style: str = DEFAULT_STYLE,
                     *, seed: str = "", policy: str = DEFAULT_POLICY) -> str:
    """从话术库选一句。``policy=handoff`` 仍给延后句（人工未接手前垫一句）。"""
    _ = policy
    lg = sniff_lang(str(seed or ""), lang)
    rows = _pool(kind, lg, style)
    if not rows:
        return "not yet"
    n = 0
    if seed:
        n = sum(ord(c) for c in str(seed)) % len(rows)
    return rows[n]


def refuse_candidates(kind: str, lang: Optional[str] = None, style: str = DEFAULT_STYLE,
                      n: int = 3) -> List[str]:
    """审核稿 2–3 条拒绝候选（禁止接受 / 给地址）。"""
    lg = sniff_lang("", lang)
    rows = _pool(kind, lg, style)
    alt_style = "direct" if style != "direct" else "soft"
    extra = _pool(kind, lg, alt_style)
    out: List[str] = []
    for s in rows + extra:
        if s and s not in out:
            out.append(s)
        if len(out) >= max(2, min(int(n or 3), 3)):
            break
    return out[:3]


def retract_line(lang: Optional[str] = None, *, seed: str = "") -> str:
    lg = sniff_lang(str(seed or ""), lang)
    rows = list(_RETRACT.get(lg) or _RETRACT["en"])
    n = (sum(ord(c) for c in str(seed)) % len(rows)) if seed else 0
    return rows[n]


def casual_refuse_line(sample_text: str, *, photo: bool = False) -> str:
    """self_blame 改写：直接随意拒绝。照片语境委托 P-3 诚实句。"""
    if photo:
        try:
            from src.ai.outbound_promise_guard import honest_no_photo_line
            return honest_no_photo_line(sample_text)
        except Exception:
            pass
    lg = sniff_lang(sample_text)
    rows = list(_CASUAL_REFUSE.get(lg) or _CASUAL_REFUSE["en"])
    n = sum(ord(c) for c in str(sample_text or "x")) % len(rows)
    return rows[n]


def rewrite_commitment_claim(text: str, *, lang: str = "", hit: Optional[str] = None) -> str:
    kind = hit or detect_commitment_claim(text) or "meet"
    lg = sniff_lang(text, lang)
    return pick_refuse_line(kind, lg, DEFAULT_STYLE, seed=text[:24])


def rewrite_self_blame_repromise(text: str, *, lang: str = "", hit: Any = None) -> str:
    _ = hit
    photo = bool(re.search(r"\b(?:pic|pics|photo|selfie|照片|自拍)\b", str(text or ""), re.I)
                 or "照片" in str(text or "") or "自拍" in str(text or ""))
    return casual_refuse_line(text or lang, photo=photo)


def detect_media_claim_kind(text: str) -> str:
    """P-3 委托：列出 CLAIM_KINDS 用。无 media_context 时不在起草层改写。"""
    try:
        from src.ai.outbound_promise_guard import detect_media_claim
        return str(detect_media_claim(text, media_context=False) or "")
    except Exception:
        return ""


# 接口约定④：与 P-3 media_claim 并成一表。rewrite=None → 本层不改写（P-3 动作型）。
CLAIM_KINDS: Tuple[Tuple[str, Callable[..., Any], Optional[Callable[..., str]]], ...] = (
    ("commitment_claim", detect_commitment_claim, rewrite_commitment_claim),
    ("self_blame_repromise", detect_self_blame_repromise, rewrite_self_blame_repromise),
    ("media_claim", detect_media_claim_kind, None),
)


def apply_claim_rewrites(text: str, *, lang: str = "") -> Tuple[str, Dict[str, Any]]:
    """起草层：commitment_claim / self_blame_repromise 命中即改写。media_claim 只记账不改。"""
    cur = str(text or "")
    hits: List[str] = []
    for kind, detect, rewrite in CLAIM_KINDS:
        try:
            found = detect(cur)
        except Exception:
            found = None
        if not found:
            continue
        hits.append(kind)
        if rewrite is None:
            continue
        try:
            nxt = rewrite(cur, lang=lang, hit=found if kind != "self_blame_repromise" else True)
        except Exception:
            logger.debug("[commitment] rewrite %s 失败（原文）", kind, exc_info=True)
            continue
        if nxt and str(nxt).strip() and str(nxt).strip() != cur.strip():
            cur = str(nxt).strip()
    if hits:
        try:
            from src.inbox import ai_fingerprint_stats as _fp
            for k in hits:
                _fp.record(k)
        except Exception:
            pass
    return cur, {"hits": hits, "action": "rewrite" if hits and any(
        k != "media_claim" for k in hits) else ("hit" if hits else "clean")}


# ── 24h 账本 ────────────────────────────────────────────────────────────
def _kv_get(store: Any, cid: str) -> Dict[str, Any]:
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return {}
    try:
        raw = store.get_app_setting(KV_PREFIX + cid, "") or ""
        got = json.loads(raw) if raw else {}
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _kv_set(store: Any, cid: str, rec: Dict[str, Any]) -> None:
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return
    try:
        store.set_app_setting(KV_PREFIX + cid, json.dumps(rec, ensure_ascii=False),
                              updated_by="commitment_guard")
    except Exception:
        logger.debug("[commitment] 账本写入失败 conv=%s", cid, exc_info=True)


def note_hit(store: Any, cid: str, kind: str, *, now: Optional[float] = None,
             promised: bool = False) -> Dict[str, Any]:
    ts = float(now if now is not None else time.time())
    rec = _kv_get(store, cid)
    hits = [h for h in (rec.get("hits") or []) if isinstance(h, dict)
            and ts - float(h.get("ts") or 0) <= HIT_TTL_SEC]
    hits.append({"kind": str(kind), "ts": ts})
    rec = {
        "hits": hits[-20:],
        "last_kind": str(kind),
        "last_ts": ts,
        "count": len(hits),
        "promised": bool(rec.get("promised") or promised),
    }
    _kv_set(store, cid, rec)
    return rec


def is_second_insist(store: Any, cid: str, kind: str, *, now: Optional[float] = None) -> bool:
    ts = float(now if now is not None else time.time())
    rec = _kv_get(store, cid)
    for h in rec.get("hits") or []:
        if not isinstance(h, dict):
            continue
        if str(h.get("kind") or "") == str(kind) and ts - float(h.get("ts") or 0) <= HIT_TTL_SEC:
            return True
    return False


def already_promised(store: Any, cid: str, *, now: Optional[float] = None) -> bool:
    rec = _kv_get(store, cid)
    if rec.get("promised"):
        return True
    if store is None or not cid or not hasattr(store, "list_recent_messages"):
        return False
    ts = float(now if now is not None else time.time())
    try:
        rows = store.list_recent_messages(cid, limit=30) or []
    except Exception:
        return False
    for m in rows:
        if str(m.get("direction") or "") != "out":
            continue
        try:
            mts = float(m.get("ts") or 0)
        except (TypeError, ValueError):
            mts = 0.0
        if mts and ts - mts > HIT_TTL_SEC:
            continue
        if detect_commitment_claim(str(m.get("text") or m.get("body") or "")):
            return True
    return False


def set_risk_hold(store: Any, cid: str, kind: str, hit: str = "") -> None:
    """接口约定②：Q-3 ``risk_hold.set``；未合并 / 缺模块 → hasattr 兜底静默。"""
    try:
        from src.inbox import risk_hold as rh
        if hasattr(rh, "set"):
            rh.set(store, cid, "commitment:" + str(kind or "meet"), hit or kind)
    except Exception:
        logger.debug("[commitment] risk_hold.set 不可用（忽略）", exc_info=True)


def prompt_block(persona: Any, *, compact: bool = False) -> str:
    """注入人设块：never 不给时间承诺；照片能力另判。"""
    policy, months = meeting_policy_of(persona)
    style = commitment_style_of(persona)
    style_zh = "直接" if style == "direct" else "委婉"
    if policy == "handoff":
        body = (
            "现实动作（见面/上门/给地址电话/视频通话/寄礼物/转账）你自己绝不答应，"
            "一律交给人工；未接手前只准用一句委婉延后，禁止给地址、禁止说「周末见」。"
        )
    elif policy == "after_months":
        body = (
            f"现实动作（见面/上门/给地址电话/视频通话/寄礼物/转账）在这段关系未满"
            f"{months}个月时一律婉拒；满{months}个月也只把请求交给人工，你本人永不答应见面。"
            "第一次就拒，被追问仍拒，不解释之前为何没做，不给任何时间承诺。"
        )
    else:
        body = (
            "现实动作（见面/上门/给地址电话/视频通话/寄礼物/转账）一律婉拒："
            "第一次就拒，被追问仍拒，不解释之前为何没做，不给任何时间承诺"
            "（下周/等我忙完/熟悉了再说都不许）。"
        )
    photos = False
    try:
        caps = (persona or {}).get("capabilities") if isinstance(persona, dict) else None
        photos = bool(isinstance(caps, dict) and caps.get("photos"))
    except Exception:
        photos = False
    photo_bit = "照片按相册能力另判，不要用见面那套话术挡发图。" if photos else (
        "没有发照片能力时照片请求也拒，不要说「待会发给你」。")
    if compact:
        return f"【现实承诺·{policy}·口吻{style_zh}】{body}{photo_bit}"
    return (
        f"【现实承诺守卫·见面政策={policy}·拒绝口吻={style_zh}】\n"
        f"{body}{photo_bit}"
    )


def handle_inbound(
    text: str, *, conversation_id: str = "", store: Any = None,
    persona: Any = None, lang: Optional[str] = None, now: Optional[float] = None,
    photos_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    """入站处置。``decision`` ∈ clean|delegate_p3|refuse_sent|handoff|second_insist。"""
    kind = detect_commitment(text, lang)
    lg = sniff_lang(text, lang)
    if not kind:
        return {"kind": None, "decision": "clean", "text": "", "candidates": [],
                "policy": meeting_policy_of(persona)[0]}
    if photos_ok is None:
        try:
            caps = (persona or {}).get("capabilities") if isinstance(persona, dict) else None
            photos_ok = bool(isinstance(caps, dict) and caps.get("photos"))
        except Exception:
            photos_ok = False
    policy_raw, _months = meeting_policy_of(persona)
    policy = effective_policy(persona, store=store, conversation_id=conversation_id, now=now)
    style = commitment_style_of(persona)
    if kind == "media" and photos_ok and not is_video_call_ask(text):
        logger.info("[commitment] conv=%s kind=%s policy=%s decision=delegate_p3",
                    conversation_id or "-", kind, policy_raw)
        return {"kind": kind, "decision": "delegate_p3", "text": "", "candidates": [],
                "policy": policy_raw, "lang": lg}
    cands = refuse_candidates(kind, lg, style, n=3)
    insist = is_second_insist(store, conversation_id, kind, now=now)
    promised = already_promised(store, conversation_id, now=now)
    if insist:
        body = retract_line(lg, seed=conversation_id) if promised else (
            cands[1] if len(cands) > 1 else cands[0])
        note_hit(store, conversation_id, kind, now=now)
        logger.info("[commitment] conv=%s kind=%s policy=%s decision=second_insist",
                    conversation_id or "-", kind, policy_raw)
        return {"kind": kind, "decision": "second_insist", "text": body,
                "candidates": cands, "policy": policy_raw, "lang": lg, "promised": promised}
    if policy == "handoff":
        note_hit(store, conversation_id, kind, now=now)
        logger.info("[commitment] conv=%s kind=%s policy=%s decision=handoff",
                    conversation_id or "-", kind, policy_raw)
        return {"kind": kind, "decision": "handoff", "text": cands[0] if cands else "",
                "candidates": cands, "policy": policy_raw, "lang": lg}
    note_hit(store, conversation_id, kind, now=now)
    logger.info("[commitment] conv=%s kind=%s policy=%s decision=refuse_sent",
                conversation_id or "-", kind, policy_raw)
    return {"kind": kind, "decision": "refuse_sent", "text": cands[0] if cands else pick_refuse_line(kind, lg),
            "candidates": cands, "policy": policy_raw, "lang": lg}


def evaluate_inbound(store: Any, conv: Dict[str, Any], text: str, *, kind: str,
                     automation_mode: str = "auto_ai", lang: str = "", cfg: Any = None,
                     now: Optional[float] = None, persona: Any = None) -> Dict[str, Any]:
    """drafts.auto_generate_draft 调用口：detect 已完成，此处按政策出话术。

    refuse_sent 不登记 risk_hold（Q-3 worker 闸会取消本条 pacing）；handoff /
    second_insist 才 set + 暂停全自动。``decision=p3`` 表示照片交 P-3。
    """
    _ = (kind, cfg)
    cid = str((conv or {}).get("conversation_id") or "")
    photos_ok = False
    if persona is None:
        try:
            plat = str((conv or {}).get("platform") or "")
            acct = str((conv or {}).get("account_id") or "")
            ck = str((conv or {}).get("chat_key") or "")
            if plat or acct or ck:
                from src.ai.persona_voice import resolve_effective_persona_id
                pid = str(resolve_effective_persona_id(cfg or {}, plat, acct, ck) or "")
                if pid:
                    from src.utils.persona_manager import PersonaManager
                    persona = PersonaManager.get_instance().get_persona_by_id(pid)
        except Exception:
            persona = None
    try:
        caps = (persona or {}).get("capabilities") if isinstance(persona, dict) else None
        photos_ok = bool(isinstance(caps, dict) and caps.get("photos"))
    except Exception:
        photos_ok = False
    got = handle_inbound(
        text, conversation_id=cid, store=store, persona=persona,
        lang=lang, now=now, photos_ok=photos_ok)
    got["line"] = str(got.get("text") or "")
    dec = str(got.get("decision") or "")
    if dec == "delegate_p3":
        got["decision"] = "p3"
        return got
    if dec in ("handoff", "second_insist"):
        set_risk_hold(store, cid, str(got.get("kind") or kind), str(text or "")[:80])
        if dec == "second_insist" and store is not None and hasattr(store, "set_automation_mode"):
            try:
                if str(automation_mode or "") == "auto_ai":
                    store.set_automation_mode(cid, "review", source="guard:commitment")
            except Exception:
                logger.debug("[commitment] 暂停全自动失败", exc_info=True)
    return got


__all__ = [
    "KINDS", "CLAIM_KINDS", "DEFAULT_POLICY",
    "detect_commitment", "detect_commitment_claim", "detect_request", "detect_self_blame_repromise",
    "meeting_policy_of", "commitment_style_of", "effective_policy",
    "pick_refuse_line", "refuse_candidates", "retract_line",
    "apply_claim_rewrites", "handle_inbound", "evaluate_inbound", "set_risk_hold",
    "note_hit", "is_second_insist", "already_promised", "prompt_block",
    "sniff_lang", "is_video_call_ask", "conversation_age_months",
]
