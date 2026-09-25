# -*- coding: utf-8 -*-
"""风控三级分级（Q-17 #277②，2026-09-11）。

事故（Cameron 15635715247）：04:39:45 人工摘「需人工」→ 04:50:12 客户一句「Trying to show you the
pictures they have sent me…」→ ``keyword_risk_hits`` 对客户入站也跑 ``detect_commitment / detect_commitment_claim``
→ ``commitment:media`` → high → 第三次打标。根因是**一把尺子**：隐私词 / 照片 / 电话 / 钱只要出现就 high，
叙述与索要、客户说与 AI 说不分。

本模块把风险分三级、每级一种动作，其它模块只消费结论：

  高（high）  诈骗 / 索钱 / 威胁 / 自伤危机 / 未成年 / 露骨+施压（Q-15 ``pressure``）
              → L1 + 「需人工」+ risk_hold —— **现状行为一字不变**（本模块只补 threat / minor / scam 三张新表）。
  中（medium）露骨提及（Q-15 ``explicit``）/ 客户**索要**见面·照片·电话（``request:<kind>``）/ 停联词
              → 走人设政策：Q-2 委婉延后 / Q-15 soft_reply / O-1 A stop_contact 冻结；只打会话标签
              ``risk:medium``，**不进** needs_human。
  低（low）   隐私词、照片 / 钱 / 电话的**叙述性**提及 → 只落一行 ``[risk] low conv= category= hits=``。

接线（与 Q-15 ``adult_grader`` 同式）：``drafts.auto_generate_draft`` 在 ``quick_analyze`` +
``keyword_risk_hits(direction="in")`` + Q-2 / Q-15 之后、``policy_decide`` 之前调 ``regrade_inbound`` 一行；
原判定为 high 且**只**由可降因子（privacy 叙述 / 承诺强短语兜底）撑起时才降，真高风险因子
（self_harm / stop_contact / credential_or_payment_request / money / adult / 支付词表）在场一律不动。
人设级覆写 ``boundaries.risk_overrides: {category: level}`` 只对中 / 低类别生效（高类别有地板，
adult 沿用 Q-15 ``adult_policy``，stop_contact 沿用 O-1 A）。任何异常 → 原判定放行。

Q-27（#301 追加 AFD2CD / #296，2026-09-12）**最小硬拦集**——高级只留 ``self_harm / minor / threat /
money_request（含验证码 / 凭据索要句式）/ scam``（+ ``stop_contact`` 硬停由 O-1 A 冻结，不在 L1 集合）：
  · ``payment_keyword``（refund / password / OTP 裸词）→ 中级「只标记 + 人审候选」，不再强制 L1；
  · ``adult``：``pressure``（露骨 + 施压）才高；``explicit`` → 中级（人设政策接住 / Q-23 软回应，
    不 risk_hold、不 needs_human）；``mention / flirt`` 单点成人词 → 只落 ``[risk] low`` 日志；
  · quick_analyze 的 ``money`` 单词（bank card / paypal / 转账 提及）无索要句式 → 低（``money_mention``）；
  · **任何 keyword-only 命中不得单独把全自动改 L1**：高级必须是「类别 + 句式 / 施压第二信号」，
    ``TRUE_HIGH_REASONS`` 只剩 self_harm / stop_contact / credential_or_payment_request（句式）+
    Q-15 ``adult:pressure`` 标记；原 high 若无这些因子撑着一律降到分级结论。

R88（2026-09-17，老板拍板「规则太严、全是误报」）**默认只记录、按需锁定**：
  · 三级只表示敏感度（红 / 橙 / 绿），**不再自动决定拦不拦**；所有类别缺省都「继续回复 + 记一笔」
    （会话标签 ``risk:high`` / ``risk:medium`` + 拦截台账 ``risk_recorded`` + ``[risk]`` 日志）；
  · 拦截动作（needs_human / hard_stop / freeze）只对**运营显式锁定**的类别执行——
    ``inbox.risk_grading.locked: [self_harm, stop_contact, …]``（overlay，回复设置页「锁定」开关写入）；
    可锁的只有本表 ``action ∈ {hard_stop, needs_human, freeze}`` 的六类（:data:`LOCKABLE`），
    成人走 adult_policy 卡、索要类走人设边界政策、其余本来就不拦；
  · 未锁定类别命中时把 quick_analyze 的硬停主因改名 ``<reason>_recorded``（``self_harm`` /
    ``stop_contact`` / ``credential_or_payment_request``），下游 ``hard_stop_reason`` / 人审判定
    看不到它们 → 档位按 medium 走 L2；影子台账照常记「本会被扣」。不分昼夜 / 时区——全球客户同一规则。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LEVELS: Tuple[str, ...] = ("low", "medium", "high")
_RANK = {"low": 1, "medium": 2, "high": 3}
MEDIUM_TAG = "risk:medium"           #: 中级命中只打这一枚会话标签（不进 needs_human）
HIGH_TAG = "risk:high"               #: R88：高敏命中但类别**未锁定** → 只打这一枚标签 + 台账（AI 照常回）
LOCK_CFG_PATH = "inbox.risk_grading.locked"   #: overlay 键：显式锁定的类别 id 列表（缺省空＝全部只记录）
RECORDED_SUFFIX = "_recorded"        #: 未锁定类别的硬停主因改名后缀（self_harm → self_harm_recorded）
LEDGER_CODE_RECORDED = "risk_recorded"        #: 拦截台账原因码：敏感话题已记录、未拦（abort_ledger.REASONS）
REASON_PREFIX = "risk:"              #: 本模块补进 peer_reasons 的细因前缀（risk:<category>）
PRIVACY_MENTION_REASON = "privacy_mention"   #: 被降为叙述的 quick_analyze ``privacy`` 主因改名，防下游按 privacy 处置
MONEY_MENTION_REASON = "money_mention"       #: Q-27：quick_analyze ``money`` 单词（无索要句式）降为叙述后的改名，同上

#: 真高风险因子（quick_analyze / Q-15 已判）——在场一律不降。
#: Q-27（#301）：``money`` / ``adult`` 单词不再在列——它们是 keyword-only；索要句式走
#: ``credential_or_payment_request`` / ``detect_request(money)``，露骨施压走 ``adult:pressure``（见 ``_downgradable``）。
TRUE_HIGH_REASONS = frozenset({"self_harm", "stop_contact", "credential_or_payment_request"})
ADULT_PRESSURE_REASON = "adult:pressure"     #: Q-15 施压标记（adult_grader 出）——唯一让 adult 撑起 high 的因子

_LB = r"(?<![A-Za-z0-9_])"
_RB = r"(?![A-Za-z0-9_])"


def _rx(p: str) -> "re.Pattern[str]":
    return re.compile(p, re.IGNORECASE | re.DOTALL)


# ── 本模块新增三张高风险表（现状没有，指令 A 段点名）──────────────────────────
_THREAT = [_rx(p) for p in (
    _LB + r"i(?:'ll| will|'m going to| am going to|ma)\s+(?:find|hunt|track)\s+you(?:\s+down)?" + _RB,
    _LB + r"i\s+know\s+where\s+you\s+(?:live|work|are)" + _RB,
    _LB + r"i(?:'ll| will|'m going to| am going to)\s+(?:kill|hurt|beat|destroy|ruin|expose|dox+|leak|report)\s+(?:you|u|your\s+(?:photos?|pics?|nudes?|address|family))" + _RB,
    _LB + r"(?:leak|post|share|expose)\s+your\s+(?:nudes?|photos?|pics?|videos?)\s+(?:online|everywhere|to\s+everyone)" + _RB,
    _LB + r"(?:you|u)(?:'ll| will)\s+(?:regret|pay)\s+(?:this|for\s+this|it)" + _RB,
    _LB + r"or\s+else\s+i(?:'ll| will)" + _RB,
    r"我(会|要|一定)(找到|杀|弄死|搞死|曝光|人肉|报警抓)你|我知道你住哪|我知道你家|不然我就(曝光|发你|把你|报)|你(会|等着)(后悔|付出代价)|把你(的)?(照片|裸照|视频)发(到|给|出去)",
    r"殺す|見つけ出す|晒す|住所知ってる",
)]
_MINOR = [_rx(p) for p in (
    # 「I'm 16」「I am 16」「im 16」「I'm 17yo / 17 y/o / 17yrs / 17 years old」（yo 紧贴数字也算）；
    # 排除计量 / 相对年龄：「I'm 15 minutes away」「I'm 16 years older than you」「I'm 17k in debt」
    _LB + r"i(?:['’]m| am|m)\s+(?:only\s+|just\s+)?1[0-7]"
    r"(?!\s*(?:years?\s+(?:older|younger|ago|later|apart|into)|minutes?|mins?|hours?|hrs?|days?|weeks?|months?"
    r"|km|miles?|kg|lbs?|cm|bucks|dollars|percent|%|k|th|st|nd|rd)\b)"
    r"(?:\s*(?:years?\s+old|years?|yo|y/o|yrs?))?" + _RB,
    # 拼写年龄「I'm sixteen」
    _LB + r"i(?:['’]m| am|m)\s+(?:only\s+|just\s+)?(?:thirteen|fourteen|fifteen|sixteen|seventeen)" + _RB,
    _LB + r"i(?:['’]m| am|m)\s+(?:a\s+)?minor" + _RB,
    _LB + r"i(?:['’]m| am|m)\s+(?:still\s+)?under\s*(?:age|18)" + _RB,
    _LB + r"(?:not|ain'?t)\s+(?:even\s+)?18\s+(?:yet|till|until)" + _RB,
    _LB + r"i(?:['’]m| am|m)\s+still\s+1[0-7]" + _RB,
    _LB + r"(?:turning|turn)\s+1[0-7]\s+(?:next|this|in)" + _RB,
    _LB + r"(?:just\s+)?turned\s+1[0-7]\s+(?:last|this|a|yesterday|today|recently)" + _RB,
    _LB + r"i(?:['’]m| am|m)\s+(?:still\s+)?in\s+(?:middle|junior\s+high|high|8th|9th|10th|11th)\s*(?:school|grade)" + _RB,
    _LB + r"i(?:['’]m| am|m)\s+(?:still\s+)?(?:a\s+)?(?:high\s*school(?:er)?|highschooler|middle\s*schooler)" + _RB,
    r"我(今年|才|刚|只有|还)?(?:1[0-7])岁|我今年(?:才|刚)?1[0-7](?![0-9号点月日])|我未成年|我(还)?是未成年|我(还)?没成年|我(还)?不到18"
    r"|我(还)?在(读|上)(初中|初一|初二|初三|高一|高二|高中)|我(还)?是(初中生|小学生|高中生|高一|高二)",
    r"(?:1[0-7])歳(です|なんだ|だよ)|未成年(です|なんだ|だよ)|中学生(です|なんだ|だよ)|高校生(です|なんだ|だよ)",
)]
_SCAM = [_rx(p) for p in (
    _LB + r"guaranteed\s+(?:returns?|profits?|income)" + _RB,
    _LB + r"(?:double|triple)\s+your\s+money" + _RB,
    _LB + r"invest(?:ment)?\s+(?:with\s+me|in\s+my|opportunity|platform)" + _RB,
    _LB + r"(?:buy|get|send)\s+(?:me\s+)?(?:a\s+|some\s+)?(?:gift\s*cards?|steam\s+cards?|itunes\s+cards?|google\s+play\s+cards?)" + _RB,
    _LB + r"(?:code|numbers?)\s+on\s+the\s+back\s+of\s+the\s+card" + _RB,
    _LB + r"(?:click|open)\s+(?:this|the)\s+link\s+(?:to|and)\s+(?:claim|verify|unlock|withdraw)" + _RB,
    _LB + r"(?:my|our)\s+(?:trading|mining|forex|crypto)\s+(?:platform|mentor|group|signal)" + _RB,
    r"稳赚|保本|带你赚|跟我投|投资(我|这个|平台)|刷单|返利|礼品卡(的)?(卡密|密码|码)|点(这个|下面)(的)?链接(领取|验证|提现)|内部消息.{0,6}(赚|涨)",
    r"必ず儲か|投資(しよう|の話)|ギフトカード(を)?買って",
)]

# ── 承诺兜底正则（drafts._SENSITIVE_PATTERNS[2]）客户侧命中 → 猜 kind ──────────
_GUESS_KIND = (
    ("money", _rx(r"send\s+(?:me\s+)?money|cash\s*app|打钱给你")),
    ("gift", _rx(r"寄给你|收货地址")),
    ("contact", _rx(r"i'?ll\s+text\s+you\s+my\s+address|我地址是")),
    ("media", _rx(r"视频通话")),
)


# ── 公开类别表（回复设置页「风控分级」卡直读；顺序＝同级别时的主因优先）─────────
# words：词表摘要（不是完整正则，公开给运营看「大概什么词会中」）；source：真判定所在。
CATEGORIES: List[Dict[str, Any]] = [
    {"id": "self_harm", "level": "high", "floor": "high", "overridable": False, "action": "hard_stop",
     "source": "quick_analyze._RISK_TERMS.self_harm → O-1 A",
     "words": {"en": ["suicide", "kill myself", "end my life", "want to die"], "zh": ["自杀", "自残", "不想活了", "活不下去"]}},
    {"id": "minor", "level": "high", "floor": "high", "overridable": False, "action": "needs_human",
     "source": "risk_grader._MINOR",
     "words": {"en": ["I'm 15", "I'm a minor", "I'm under 18", "in middle school"], "zh": ["我才16岁", "我未成年", "我在读初中"]}},
    {"id": "threat", "level": "high", "floor": "high", "overridable": False, "action": "needs_human",
     "source": "risk_grader._THREAT",
     "words": {"en": ["I'll find you", "I know where you live", "leak your photos", "you'll regret this"], "zh": ["我知道你住哪", "曝光你", "弄死你", "你等着后悔"]}},
    {"id": "money_request", "level": "high", "floor": "high", "overridable": False, "action": "needs_human",
     "source": "quick_analyze._RISK_TERMS.money / _CRED_REQUEST + commitment_guard.detect_request(money)",
     "words": {"en": ["send me money", "cash app me", "can you lend me $", "bank card", "give me your password / OTP"], "zh": ["转账给我", "借点钱", "发个红包", "银行卡", "把验证码发我"]}},
    {"id": "scam", "level": "high", "floor": "high", "overridable": False, "action": "needs_human",
     "source": "risk_grader._SCAM",
     "words": {"en": ["guaranteed returns", "double your money", "invest with me", "gift card code"], "zh": ["稳赚", "带你赚", "刷单返利", "礼品卡卡密"]}},
    {"id": "payment_keyword", "level": "medium", "floor": "medium", "overridable": False, "action": "mark_review",
     "source": "drafts._SENSITIVE_PATTERNS[0]（Q-27：裸词只标记 + 人审候选，不强制 L1；索要句式走 money_request）",
     "words": {"en": ["refund", "payment", "wire transfer", "password", "OTP", "deposit"], "zh": ["退款", "付款", "转账", "银行卡", "密码", "验证码"]}},
    {"id": "adult", "level": "medium", "floor": "medium", "overridable": False, "action": "adult_policy",
     "source": "adult_grader（Q-27：pressure→high；explicit→中级·人设接住 / Q-23 软回应，不持有不打标；mention/flirt→只记日志）",
     "words": {"en": ["nudes", "sex", "naked", "porn"], "zh": ["裸照", "成人视频", "约炮"]}},
    {"id": "stop_contact", "level": "medium", "floor": "medium", "overridable": False, "action": "freeze",
     "source": "quick_analyze 停联意图 → O-1 A（一条告别 + 冻结）",
     "words": {"en": ["stop messaging me", "leave me alone", "don't contact me"], "zh": ["别再联系我", "不要再发了", "拉黑"]}},
    {"id": "request_contact", "level": "medium", "floor": "low", "overridable": True, "action": "persona_policy",
     "source": "commitment_guard.detect_request(contact) → Q-2 meeting_policy",
     "words": {"en": ["what's your number", "send me your address", "where do you live", "add me on whatsapp"], "zh": ["你住哪", "你电话多少", "加个微信", "把地址发我"]}},
    {"id": "request_media", "level": "medium", "floor": "low", "overridable": True, "action": "persona_policy",
     "source": "commitment_guard.detect_request(media) → Q-2 / P-3 照片支线",
     "words": {"en": ["send me a pic", "show me yours", "can we video call"], "zh": ["发张照片", "看看你的脸", "打个视频"]}},
    {"id": "request_meet", "level": "medium", "floor": "low", "overridable": True, "action": "persona_policy",
     "source": "commitment_guard.detect_request(meet) → Q-2 meeting_policy",
     "words": {"en": ["can we meet up", "wanna come over", "let's grab coffee"], "zh": ["要不要见个面", "来我家", "约个饭"]}},
    {"id": "request_gift", "level": "medium", "floor": "low", "overridable": True, "action": "persona_policy",
     "source": "commitment_guard.detect_request(gift) → Q-2 meeting_policy",
     "words": {"en": ["can I send you a gift", "mail you a package, need your address"], "zh": ["寄给你个礼物", "给你寄点东西"]}},
    {"id": "complaint", "level": "medium", "floor": "low", "overridable": True, "action": "shadow_log",
     "source": "drafts._SENSITIVE_PATTERNS[1]",
     "words": {"en": ["discount", "complaint", "lawyer", "scam (mention)", "police"], "zh": ["优惠", "投诉", "律师", "骗子（提及）", "报警"]}},
    # Q-36（#313 2JK95C）：客户**提议发自己的图** / 已发图问看到没 / 要不要看我的——request_media 的兄弟意图。
    # 处置 = 接受 + 期待（「发来看看」），**不走** 拒发模板 / media_promise / claim_guard；只落 [risk] low 日志。
    # 带「你的照片 / your pic / 发我」领属的混合句仍按 request_media（commitment_guard._OFFER_EXCLUDE 先判）。
    {"id": "offer_media", "level": "low", "floor": "low", "overridable": False, "action": "accept_expect",
     "source": "commitment_guard.detect_offer_media（客户领属：我的 / 我拍 / my / 私の / 내 / mía / minha）→ inbound_enrich.build_offer_media_hint",
     "words": {"en": ["I'll send you a pic", "wanna see my dinner?", "I sent a photo, did you see it?", "can I send you a picture of me"],
               "zh": ["一会我拍照片给你看", "我发张我的给你看", "看到我发的照片了吗", "要不要看我做的菜"]}},
    {"id": "privacy", "level": "low", "floor": "low", "overridable": True, "action": "log_only",
     "source": "quick_analyze._RISK_TERMS.privacy（叙述；索要走 money_request / request_contact）",
     "words": {"en": ["password", "passport", "my address", "ID number"], "zh": ["密码", "护照", "住址", "身份证"]}},
    {"id": "narrative", "level": "low", "floor": "low", "overridable": True, "action": "log_only",
     "source": "drafts._SENSITIVE_PATTERNS[3]（your address / phone number / my address is 叙述）",
     "words": {"en": ["your address", "phone number", "my address is", "pictures they sent me"], "zh": ["我地址是", "电话号码（叙述）", "他们发我的照片"]}},
]
_CAT_BY_ID: Dict[str, Dict[str, Any]] = {c["id"]: c for c in CATEGORIES}
_CAT_ORDER: Dict[str, int] = {c["id"]: i for i, c in enumerate(CATEGORIES)}
OVERRIDABLE: Tuple[str, ...] = tuple(c["id"] for c in CATEGORIES if c.get("overridable"))
#: R88：会真的让 AI 停下的动作——只有这些类别有「锁定」开关可打；其余动作本来就是继续回复。
_BLOCKING_ACTIONS = frozenset({"hard_stop", "needs_human", "freeze"})
LOCKABLE: Tuple[str, ...] = tuple(c["id"] for c in CATEGORIES if c.get("action") in _BLOCKING_ACTIONS)
#: quick_analyze 硬停主因 → 所属类别（未锁定时改名 ``<reason>_recorded``，让 hard_stop_reason / 人审判定看不到）
_HARD_REASON_CATEGORY = {"self_harm": "self_harm", "stop_contact": "stop_contact",
                         "credential_or_payment_request": "money_request"}


def category_def(cid: str) -> Dict[str, Any]:
    return dict(_CAT_BY_ID.get(str(cid or ""), {}))


# ── R88 锁定（默认只记录）───────────────────────────────────────────────────

def _cfg_dict(cfg: Any) -> Dict[str, Any]:
    """``cfg`` 可能是 dict / 带 ``.config`` 的 ConfigManager / None（None → 进程全局配置）。绝不抛。"""
    try:
        if isinstance(cfg, dict):
            return cfg
        inner = getattr(cfg, "config", None)
        if isinstance(inner, dict):
            return inner
        if cfg is None:
            from src.utils.config_manager import config_manager
            inner = getattr(config_manager, "config", None)
            return inner if isinstance(inner, dict) else {}
    except Exception:
        pass
    return {}


def normalize_locked(raw: Any) -> List[str]:
    """``inbox.risk_grading.locked`` 原值（list / 逗号串 / None）→ 去重、只保留 :data:`LOCKABLE`，按表序。"""
    if isinstance(raw, str):
        items: Iterable[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = ()
    seen = set()
    for it in items:
        cid = str(it or "").strip().lower()
        if cid in LOCKABLE and cid not in seen:
            seen.add(cid)
    return [c for c in LOCKABLE if c in seen]


def locked_categories(cfg: Any = None) -> List[str]:
    """运营显式锁定（命中就执行拦截动作）的类别。缺省 **空**＝全部只记录、AI 照常回复。绝不抛。"""
    try:
        d = _cfg_dict(cfg)
        sec = d.get("inbox") if isinstance(d.get("inbox"), dict) else {}
        rg = (sec or {}).get("risk_grading") if isinstance((sec or {}).get("risk_grading"), dict) else {}
        return normalize_locked((rg or {}).get("locked"))
    except Exception:
        return []


def is_locked(category: str, cfg: Any = None) -> bool:
    """该类别是否被锁定（命中要停 AI）。不可锁 / 未知类别 / 空 → False。"""
    cid = str(category or "").strip().lower()
    return bool(cid) and cid in LOCKABLE and cid in locked_categories(cfg)


#: 成人不设限（adult_policy=open）时**隐含锁定**的类别：``minor``。
#: 放开成人的代价是未成年必停——不依赖运营记得去回复设置页勾「锁定」，也不依赖 prompt 里那句
#: 「疑似未成年立刻停」（那只是给模型的提示，不是闸）。
IMPLIED_LOCKED_WHEN_ADULT_OPEN: Tuple[str, ...] = ("minor",)


def implied_locked(cfg: Any = None, persona: Any = None) -> List[str]:
    """按有效成人政策推出的隐含锁定类别（当前只有 open → minor）。绝不抛。"""
    try:
        from src.inbox.adult_grader import adult_open
        if adult_open(persona, cfg):
            return [c for c in IMPLIED_LOCKED_WHEN_ADULT_OPEN if c in LOCKABLE]
    except Exception:
        pass
    return []


def is_locked_effective(category: str, cfg: Any = None, persona: Any = None) -> Tuple[bool, str]:
    """``(locked, by)``：``by`` ∈ ``config``（运营显式锁定）/ ``adult_open``（成人不设限隐含）/ ``""``。
    判定钩子用这个；UI 锁定开关仍读 :func:`is_locked`（显式那份）。"""
    cid = str(category or "").strip().lower()
    if not cid or cid not in LOCKABLE:
        return False, ""
    if is_locked(cid, cfg):
        return True, "config"
    if cid in implied_locked(cfg, persona):
        return True, "adult_open"
    return False, ""


def outcome_of(category: str, *, locked: Optional[bool] = None, cfg: Any = None) -> str:
    """一个类别命中后「AI 会怎么做」的结果码（回复设置页 chip 文案键，不是判定）：

    ``stop``（锁定：AI 停下交给人）/ ``freeze``（锁定的停联：停下并提醒坐席，不回客户）/
    ``adult``（按成人政策卡）/ ``persona``（继续回复，按人设边界委婉答）/ ``accept``（发来看看）/
    ``review``（继续回复 + 进人审候选）/ ``record``（继续回复，只记一笔）。
    """
    c = _CAT_BY_ID.get(str(category or ""), {})
    act = str(c.get("action") or "")
    if not c:
        return "record"
    if act in _BLOCKING_ACTIONS:
        lk = is_locked(c["id"], cfg) if locked is None else bool(locked)
        if not lk:
            return "record"
        return "freeze" if act == "freeze" else "stop"
    return {"adult_policy": "adult", "persona_policy": "persona", "accept_expect": "accept",
            "mark_review": "review"}.get(act, "record")


def public_table(persona: Any = None, cfg: Any = None) -> List[Dict[str, Any]]:
    """回复设置页「敏感话题」卡数据：类别 / 词表摘要 / 级别 / 动作 / 是否可覆写 / 人设覆写后的有效级别
    + R88 ``lockable`` / ``locked`` / ``outcome``。"""
    ov = risk_overrides_of(persona)
    locked = set(locked_categories(cfg))
    implied = set(implied_locked(cfg, persona))
    out: List[Dict[str, Any]] = []
    for c in CATEGORIES:
        row = dict(c)
        row["words"] = {k: list(v) for k, v in (c.get("words") or {}).items()}
        row["effective_level"] = ov.get(c["id"], c["level"]) if c.get("overridable") else c["level"]
        row["override"] = ov.get(c["id"], "") if c.get("overridable") else ""
        row["lockable"] = c["id"] in LOCKABLE
        # ``locked`` = 运营显式勾的那份（开关状态）；``locked_by`` 说明真正让它停的来源——
        # 成人不设限隐含锁定的类别开关显示为「强制锁定」，chip 按会停 AI 画。
        row["locked"] = c["id"] in locked
        row["locked_by"] = "config" if c["id"] in locked else ("adult_open" if c["id"] in implied else "")
        row["outcome"] = outcome_of(c["id"], locked=bool(row["locked_by"]))
        out.append(row)
    return out


# ── 人设覆写 ────────────────────────────────────────────────────────────────

def normalize_level(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    aliases = {"high": "high", "h": "high", "高": "high", "medium": "medium", "mid": "medium", "m": "medium",
               "中": "medium", "low": "low", "l": "low", "低": "low"}
    return aliases.get(s, "")


def risk_overrides_of(persona: Any) -> Dict[str, str]:
    """``boundaries.risk_overrides: {category: level}`` → 只保留可覆写类别与合法级别（地板以下裁到地板）。"""
    out: Dict[str, str] = {}
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        raw = (b or {}).get("risk_overrides")
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            cid = str(k or "").strip().lower()
            lv = normalize_level(v)
            c = _CAT_BY_ID.get(cid)
            if not c or not c.get("overridable") or not lv:
                continue
            floor = str(c.get("floor") or "low")
            if _RANK[lv] < _RANK.get(floor, 1):
                lv = floor
            out[cid] = lv
    except Exception:
        return {}
    return out


def resolve_persona(conv: Dict[str, Any], cfg: Any = None) -> Any:
    """与 adult_grader / commitment_guard 同路：账号 / 会话有效人设。拿不到 → None。"""
    try:
        from src.inbox.adult_grader import resolve_persona as _rp
        return _rp(conv, cfg)
    except Exception:
        return None


# ── 分级 ────────────────────────────────────────────────────────────────────

def _hits(pats: Sequence["re.Pattern[str]"], text: str, limit: int = 4) -> List[str]:
    out: List[str] = []
    for p in pats:
        for m in p.finditer(text):
            h = re.sub(r"\s+", " ", m.group(0)).strip().lower()
            if h and h not in out:
                out.append(h)
            if len(out) >= limit:
                return out
    return out


def _max_level(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def grade(text: str, direction: str = "in", persona: Any = None, *,
          lang: Optional[str] = None, reasons: Optional[Iterable[str]] = None,
          hits: Optional[Iterable[str]] = None, cfg: Any = None) -> Dict[str, Any]:
    """``{level: high|medium|low, category, hits, action, found: [{category, level, hit}]}``。

    ``direction="in"``（客户入站）：quick_analyze 主因（``reasons``）映射 + 本模块三张新表 +
    ``detect_request`` 索要句式 + drafts 词表分层；``direction="out"``（AI 稿 / 出站）：
    ``detect_commitment_claim`` 答应语 + 词表。无命中 → ``level=low, category=""``。绝不抛。
    """
    t = str(text or "").strip()
    wl = t          # Q-27 D：白名单改写后的文本（入站分支赋值；出站分支 = 原文）
    found: List[Dict[str, str]] = []

    def _add(cat: str, hit: str, level: Optional[str] = None) -> None:
        c = _CAT_BY_ID.get(cat)
        if not c:
            return
        found.append({"category": cat, "level": str(level or c["level"]), "hit": str(hit or cat)[:60]})

    try:
        from src.inbox.drafts import _SENSITIVE_PATTERNS as _SP
    except Exception:
        _SP = []
    try:
        if not t:
            return {"level": "low", "category": "", "hits": [], "action": "", "found": [], "direction": direction}
        if str(direction or "in") == "out":
            try:
                from src.inbox.commitment_guard import detect_commitment_claim
                k = detect_commitment_claim(t)
            except Exception:
                k = None
            if k:
                _add("money_request" if k == "money" else "request_" + k, "commitment:" + k, "high")
            for idx, (pat, lvl) in enumerate(_SP):
                hs = _hits([pat], t)
                if not hs:
                    continue
                cat = ("payment_keyword", "complaint", "request_meet", "narrative")[idx] if idx < 4 else "narrative"
                _add(cat, hs[0], lvl if idx != 2 else "high")
        else:
            rs = [str(r) for r in (reasons or [])]
            in_hits = [str(h) for h in (hits or [])]
            # Q-27 D：通用短语白名单（cum 消歧 + phone number / your address 叙述 + 配置加白短语）——
            # 只作用于**词表层**（支付 / 投诉 / 叙述词表、privacy 单词）；句式层（detect_request /
            # _CRED_REQUEST / threat / minor / scam / 承诺兜底 money）仍看原文，配置碰不到硬拦。
            try:
                from src.inbox.adult_grader import phrase_whitelist as _pwl
                wl = _pwl(t, cfg)
            except Exception:
                wl = t
            if "self_harm" in rs:
                _add("self_harm", next((h for h in in_hits if h), "self_harm"))
            if "stop_contact" in rs:
                _add("stop_contact", "stop_contact")
            try:
                from src.inbox.commitment_guard import detect_request
                rk = detect_request(t, lang)
            except Exception:
                rk = None
            # Q-36（#313）：客户提议发自己的图 → 低级兄弟意图 offer_media（detect_request 内已对 media 排除）
            try:
                from src.inbox.commitment_guard import detect_offer_media
                if detect_offer_media(t, lang):
                    _add("offer_media", "offer:media", "low")
            except Exception:
                pass
            _money_guess = bool(_SP) and len(_SP) > 2 and bool(_hits([_SP[2][0]], t)) \
                and next((k for k, p in _GUESS_KIND if p.search(t)), "") == "money"
            if "credential_or_payment_request" in rs:
                # 索要凭据 / 付款信息**句式**（give me your password / 把验证码发我）→ 高
                _add("money_request", next((h for h in in_hits if h), "money"))
            elif "money" in rs and (rk == "money" or _money_guess):
                _add("money_request", next((h for h in in_hits if h), "money"))
            elif "money" in rs:
                # Q-27：钱相关单词（bank card / paypal / 转账 …）无索要句式 = 叙述性提及 → 低
                _add("narrative", "money:" + next((h for h in in_hits if h), "money"), "low")
            _adult_hit = next((r.split(":", 1)[1] for r in rs if r.startswith("adult_hit:")), "")
            if "adult" in rs and ADULT_PRESSURE_REASON in rs:
                # Q-15 pressure（露骨 + 施压）→ 高，本模块只记类别不改判
                _add("adult", _adult_hit or next((h for h in in_hits if h.startswith("adult") or h), "adult"), "high")
            elif "adult" in rs or "adult_mark" in rs or "adult_soft" in rs:
                # Q-27：explicit（任何政策）→ 中；adult_grader 缺席 / 异常留下的裸 adult 主因也最多到中
                _add("adult", _adult_hit or "adult", "medium")
            elif "adult_flirt" in rs:
                # Q-27 D：单点成人词 / 玩笑（mention / flirt）无第二信号 → 只落 [risk] low 日志
                _add("adult", _adult_hit or next((r.split(":", 1)[1] for r in rs if r.startswith("adult:")), "adult"), "low")
            for pats, cat in ((_THREAT, "threat"), (_MINOR, "minor"), (_SCAM, "scam")):
                hs = _hits(pats, t)
                if hs:
                    _add(cat, hs[0])
            if rk:
                _add("money_request" if rk == "money" else "request_" + rk, "request:" + rk)
            if _SP:
                if _hits([_SP[0][0]], wl):
                    _add("payment_keyword", _hits([_SP[0][0]], wl)[0])
                if len(_SP) > 1 and _hits([_SP[1][0]], wl):
                    _add("complaint", _hits([_SP[1][0]], wl)[0])
                if len(_SP) > 2:
                    hs = _hits([_SP[2][0]], t)
                    if hs:
                        guess = next((k for k, p in _GUESS_KIND if p.search(t)), "meet")
                        if guess == "money":
                            _add("money_request", hs[0])
                        else:
                            _add("request_" + guess, hs[0])
                if len(_SP) > 3 and _hits([_SP[3][0]], wl):
                    _add("narrative", _hits([_SP[3][0]], wl)[0])
            if "privacy" in rs and not rk:
                _ph = next((h for h in in_hits if h), "privacy")
                # 白名单短语（your address 叙述…）被改写后原文里的 privacy 命中词消失 → 视为 L0 不记类别
                if _ph == "privacy" or _ph.lower() in wl.lower():
                    _add("privacy", _ph)
        # 人设覆写（只对可覆写类别）
        ov = risk_overrides_of(persona)
        for f in found:
            if f["category"] in ov:
                f["level"] = ov[f["category"]]
        if not found:
            return {"level": "low", "category": "", "hits": [], "action": "", "found": [], "direction": direction,
                    "whitelisted": bool(str(direction or "in") != "out" and wl != t)}
        found.sort(key=lambda f: (-_RANK.get(f["level"], 0), _CAT_ORDER.get(f["category"], 99)))
        top = found[0]
        cdef = _CAT_BY_ID.get(top["category"], {})
        out_hits: List[str] = []
        for f in found:
            tag = f["hit"] if f["hit"].startswith(("request:", "commitment:", "offer:")) else f"{f['category']}:{f['hit']}"
            if tag not in out_hits:
                out_hits.append(tag)
        return {"level": top["level"], "category": top["category"], "hits": out_hits[:6],
                "action": str(cdef.get("action") or ""), "found": found, "direction": direction}
    except Exception:
        logger.debug("[risk] grade 异常（按无命中放行）", exc_info=True)
        return {"level": "low", "category": "", "hits": [], "action": "", "found": [], "direction": direction}


def classify_reason(reason: Any) -> Tuple[str, str]:
    """「需人工」原因码 → ``(category, level)``（冷却「同类」与 handoff_meta 的兜底口径）。

    ``adult:<level>[:hit]`` → adult / pressure→high 否则 medium；``commitment:<kind>`` → request_<kind>
    （money→money_request）/ high；``crisis*`` → self_harm / high；``high_risk`` → ("", high)；
    ``risk:<category>`` / 裸类别 id → 该类别；其余 → ("", "")。
    """
    r = str(reason or "").strip().lower()
    if not r:
        return "", ""
    if r.startswith("adult"):
        lv = r.split(":")[1] if ":" in r else ""
        return "adult", ("high" if lv == "pressure" else "medium")
    if r.startswith("commitment:"):
        k = r.split(":", 1)[1].split(":")[0]
        return ("money_request" if k == "money" else "request_" + k), "high"
    if r.startswith("crisis") or r == "self_harm":
        return "self_harm", "high"
    if r.startswith(REASON_PREFIX):
        cat = r[len(REASON_PREFIX):].split(":")[0]
        return (cat if cat in _CAT_BY_ID else ""), str(_CAT_BY_ID.get(cat, {}).get("level") or "high")
    if r in _CAT_BY_ID:
        return r, str(_CAT_BY_ID[r]["level"])
    if r == "high_risk":
        return "", "high"
    return "", ""


def first_locked_hit(reasons: Iterable[str], cfg: Any = None, persona: Any = None) -> str:
    """第一条已锁定的可锁类别（显式锁定或成人不设限隐含锁定）。未锁定 / 无命中 → 空串。绝不抛。"""
    try:
        for r in reasons or []:
            key = str(r or "").strip().lower()
            if not key:
                continue
            cat = _HARD_REASON_CATEGORY.get(key, "")
            if not cat:
                cat, _ = classify_reason(key)
            if cat and is_locked_effective(cat, cfg, persona)[0]:
                return cat
    except Exception:
        return ""
    return ""


def _downgradable(reasons: Sequence[str], text: str) -> bool:
    """原 high 是否**只**由可降因子撑起：无真高风险主因（``TRUE_HIGH_REASONS``）、无 Q-15 施压标记。

    Q-27（#301）：支付词表（refund / password / OTP 裸词）不再是「不可降」——那是 keyword-only；
    索要句式由 ``credential_or_payment_request`` / ``detect_request(money)`` 在 :func:`grade` 里判高。
    """
    for r in reasons:
        s = str(r)
        if s in TRUE_HIGH_REASONS or s.startswith(ADULT_PRESSURE_REASON):
            return False
    return True


#: Q-27 C：本条入站刚设的持有（commitment_guard / adult_grader 同一条链里 set）不算「旧 hold」，低级分级不清它
_FRESH_HOLD_SEC = 60.0


def release_hold_on_low(store: Any, cid: str, *, category: str = "", hits: Any = (),
                        now: Optional[float] = None) -> str:
    """Q-27 C（#301）：本条入站分级为**低**（叙述 / 单点提及 / 无类别）而会话上还挂着一个旧 risk_hold
    → 低级不能续命一个 hold：解除持有 + 摘「需人工」标（系统动作，不登记冷却），新入站按本条重评、
    不继承旧 shadow。返回被释放的持有原因码（空＝没释放）。绝不抛。

    不释放：stop_contact（O-1 A 冻结，另有解冻入口）；持有 set_ts 距今 < 60s（本条链自己刚设的）。
    """
    cid = str(cid or "").strip()
    if store is None or not cid:
        return ""
    try:
        from src.inbox import risk_hold as _rh
        ts = float(now if now is not None else time.time())
        reason = str(_rh.active(store, cid, now=ts) or "")
        if not reason or reason == "stop_contact":
            return ""
        rec = _rh.record(store, cid) or {}
        age = ts - float(rec.get("set_ts") or ts)
        if age < _FRESH_HOLD_SEC:
            return ""
        try:
            from src.integrations.protocol_autoreply import clear_needs_human
            clear_needs_human(store, cid, actor="system:low_inbound")
        except Exception:
            logger.debug("[risk] release_hold_on_low 摘标失败（忽略）", exc_info=True)
        _rh.clear(store, cid, by="low_inbound", now=ts)
        hs = [str(h) for h in (hits or [])][:4]
        logger.info("[risk_hold] release conv=%s hold=%s trigger=low_inbound level=low category=%s hits=%s held_min=%.1f"
                    "（低级不能续命 hold；本条按重评档位走）", cid, reason, category or "-", "|".join(hs) or "-", age / 60.0)
        return reason
    except Exception:
        logger.debug("[risk] release_hold_on_low 异常（忽略）", exc_info=True)
        return ""


def _tag_conv(store: Any, cid: str, tag: str) -> bool:
    try:
        if store is None or not cid or not hasattr(store, "get_conv_tags"):
            return False
        tags = list(store.get_conv_tags(cid) or [])
        if tag in tags:
            return False
        tags.append(tag)
        store.set_conv_tags(cid, tags)
        return True
    except Exception:
        logger.debug("[risk] %s 打标失败（忽略）", tag, exc_info=True)
        return False


def _tag_medium(store: Any, cid: str) -> bool:
    return _tag_conv(store, cid, MEDIUM_TAG)


def rename_unlocked_hard_reasons(reasons: Sequence[str], cfg: Any = None) -> Tuple[List[str], List[str]]:
    """R88：quick_analyze 的硬停主因（self_harm / stop_contact / credential_or_payment_request）所属类别
    **未锁定** → 改名 ``<reason>_recorded``。返回 ``(新 reasons, 被改名的原主因)``。

    下游 ``autosend_policy.hard_stop_reason`` / ``_downgradable`` 只认原名，改名后：不再硬停、不再撑 high、
    档位按分级结论走；原主因仍可从 ``_recorded`` 后缀追溯（日志 / 影子台账）。绝不抛。
    """
    out: List[str] = []
    renamed: List[str] = []
    try:
        for r in reasons:
            s = str(r)
            cat = _HARD_REASON_CATEGORY.get(s)
            # 停联 / 自伤是同意与安全硬停（O-1 A），不靠运营去回复设置里勾锁定。
            # 凭证索要仍按 R88：未锁定就改名、只记录。
            if cat and s not in ("stop_contact", "self_harm") and not is_locked(cat, cfg):
                out.append(s + RECORDED_SUFFIX)
                renamed.append(s)
            else:
                out.append(s)
    except Exception:
        return [str(r) for r in reasons], []
    return out, renamed


def _record_only(store: Any, cid: str, category: str, hits: Sequence[str], *, level: str = "high") -> bool:
    """R88 未锁定的高敏命中：会话标签 ``risk:high`` + 拦截台账一行 ``risk_recorded``（AI 照常回）。绝不抛。"""
    tagged = _tag_conv(store, cid, HIGH_TAG if level == "high" else MEDIUM_TAG)
    try:
        from src.inbox import abort_ledger as _al
        _al.record(store, conversation_id=cid, code=LEDGER_CODE_RECORDED, stage="grade",
                   hit=list(hits or [])[:4], source="risk_grader", reason=str(category or ""))
    except Exception:
        logger.debug("[risk] risk_recorded 台账写入失败（忽略）", exc_info=True)
    return tagged


def regrade_inbound(svc: Any, conv: Dict[str, Any], text: str, lang: str, risk_level: str,
                    peer_reasons: Sequence[str], risk_hits: Any, *,
                    automation_mode: str = "auto_ai", cfg: Any = None, persona: Any = None,
                    now: Optional[float] = None,
                    ctx: Any = None) -> Tuple[str, List[str], Optional[Dict[str, Any]]]:
    """``(risk_level, peer_reasons, info)``。drafts.py 钩子入口（Q-15 同式）。任何异常 → 原判定放行。

    - 分级 high：``max(原, high)`` + reasons 追加 ``risk:<category>``（新表 threat / minor / scam /
      索钱句式才会新增；原已 high 的只补细因）。打标仍由 decide→review_required→tag_needs_human 走现状路。
    - 分级 medium：原 high 且只由可降因子撑起 → 降 medium（``privacy``→``privacy_mention``）；会话打
      ``risk:medium`` 标签；日志 ``[risk] medium``。
    - 分级 low：同上降 low；只落 ``[risk] low conv= category= hits=``。
    - ``ctx``（Q-23 #303 ``GuardContext``，None = 逐字旧行为）：群 / 非客户发送方 → **不评估、不打标**，
      原判定原样返回，``info={"skipped": <reason>}``。
    """
    reasons = [str(r) for r in (peer_reasons or [])]
    try:
        cid = str((conv or {}).get("conversation_id") or "")
        if ctx is not None:
            _skip = ""
            try:
                _skip = str(ctx.skip_reason() or "")
            except Exception:
                _skip = ""
            if _skip:
                logger.info("[guard-ctx] skip=%s stage=risk conv=%s %s", _skip, cid or "-", ctx.log_tag())
                return risk_level, reasons, {"level": "", "category": "", "hits": [], "action": "",
                                             "downgraded": False, "from": str(risk_level or "low"),
                                             "skipped": _skip}
        store = getattr(svc, "_store", None)
        if cfg is None:
            cfg = getattr(svc, "_cfg", None)
        if persona is None:
            persona = resolve_persona(conv, cfg)
        g = grade(text, "in", persona, lang=lang, reasons=reasons, hits=list(risk_hits or []), cfg=cfg)
        level = str(g.get("level") or "low")
        cat = str(g.get("category") or "")
        ghits = [str(h) for h in (g.get("hits") or [])]
        # R88：未锁定类别的硬停主因改名（grade 已消费过原名；下游 hard_stop_reason / _downgradable 只认原名）
        reasons, _renamed = rename_unlocked_hard_reasons(reasons, cfg)
        # 显式锁定（inbox.risk_grading.locked）或隐含锁定（成人不设限 → minor 必停）
        locked, locked_by = is_locked_effective(cat, cfg, persona) if cat else (False, "")
        info: Dict[str, Any] = {"level": level, "category": cat, "hits": ghits, "action": g.get("action") or "",
                                "downgraded": False, "from": str(risk_level or "low"),
                                "locked": locked, "locked_by": locked_by,
                                "outcome": outcome_of(cat, locked=locked) if cat else "record",
                                "unlocked_reasons": _renamed}
        if isinstance(risk_hits, list):
            for h in ghits:
                if h not in risk_hits:
                    risk_hits.append(h)
        cur = str(risk_level or "low").lower()
        if not cat:
            if cur == "high" and _downgradable(reasons, text):
                # Q-27：原 high 却没有任何类别撑着（keyword-only / 未知主因）→ 不得单独把全自动改 L1
                info["downgraded"] = True
                reasons = [PRIVACY_MENTION_REASON if r == "privacy" else (MONEY_MENTION_REASON if r == "money" else r)
                           for r in reasons]
                logger.info("[risk] low conv=%s category=- hits=%s risk=low from=high（无类别·keyword-only 不停自动）",
                            cid or "-", "|".join(str(h) for h in list(risk_hits or [])[:4]) or "-")
                cur = "low"
                risk_level = "low"
            if cur == "low":
                # Q-27 C：本条无风险而会话挂着旧 hold → 释放（policy_decide 在本钩子之后，本条即按 L2 走）
                info["hold_released"] = release_hold_on_low(store, cid, category="", hits=list(risk_hits or []), now=now)
            return risk_level, reasons, info
        if level == "high":
            tag = REASON_PREFIX + cat
            if tag not in reasons:
                reasons.append(tag)
            if locked or cat not in LOCKABLE:
                # 锁定（运营要停 AI）或不可锁的高敏（adult:pressure 走成人政策卡）→ 原行为：撑 high、人审 / 硬停
                new = _max_level(cur, "high")
                logger.info("[risk] high conv=%s category=%s hits=%s action=%s from=%s locked=%s%s",
                            cid or "-", cat, "|".join(ghits[:4]) or "-", info["action"], cur, locked,
                            (" by=" + locked_by) if locked_by else "")
                return new, reasons, info
            # R88 未锁定：只记录（标签 risk:high + 台账 risk_recorded），档位按 medium 走 L2、AI 照常回。
            # 原 high 若仍被别的真高因子（adult:pressure / 锁定类别的主因）撑着 → 不降，那是它们的决定。
            if cur == "high" and _downgradable(reasons, text):
                new = "medium"
                info["downgraded"] = True
                reasons = [PRIVACY_MENTION_REASON if r == "privacy" else (MONEY_MENTION_REASON if r == "money" else r)
                           for r in reasons]
            elif cur == "high":
                new = "high"
            else:
                new = "medium"
            info["recorded"] = True
            tagged = _record_only(store, cid, cat, ghits, level="high")
            logger.info("[risk] high conv=%s category=%s hits=%s action=record from=%s risk=%s tagged=%s"
                        "（未锁定：继续回复，只记录；要停 AI 到回复设置「敏感话题」卡打开该类锁定）",
                        cid or "-", cat, "|".join(ghits[:4]) or "-", cur, new, tagged)
            return new, reasons, info
        new = cur
        if cur == "high" and _downgradable(reasons, text):
            new = level
            info["downgraded"] = True
            reasons = [PRIVACY_MENTION_REASON if r == "privacy" else (MONEY_MENTION_REASON if r == "money" else r)
                       for r in reasons]
        elif level == "medium":
            new = _max_level(cur, "medium")
        if level == "medium":
            tagged = _tag_medium(store, cid)
            tag = REASON_PREFIX + cat
            if tag not in reasons:
                reasons.append(tag)
            logger.info("[risk] medium conv=%s category=%s hits=%s action=%s tagged=%s risk=%s from=%s",
                        cid or "-", cat, "|".join(ghits[:4]) or "-", info["action"], tagged, new, cur)
        else:
            logger.info("[risk] low conv=%s category=%s hits=%s risk=%s from=%s",
                        cid or "-", cat, "|".join(ghits[:4]) or "-", new, cur)
            if new == "low":
                # Q-27 C：低级（叙述 / 单点提及）不能续命一个旧 hold
                info["hold_released"] = release_hold_on_low(store, cid, category=cat, hits=ghits, now=now)
        return new, reasons, info
    except Exception:
        logger.debug("[risk] regrade 异常（原判定放行）", exc_info=True)
        return risk_level, reasons, None


__all__ = [
    "LEVELS", "MEDIUM_TAG", "HIGH_TAG", "REASON_PREFIX", "PRIVACY_MENTION_REASON", "MONEY_MENTION_REASON",
    "TRUE_HIGH_REASONS", "ADULT_PRESSURE_REASON", "LOCK_CFG_PATH", "RECORDED_SUFFIX", "LEDGER_CODE_RECORDED",
    "CATEGORIES", "OVERRIDABLE", "LOCKABLE", "category_def", "public_table", "normalize_level",
    "normalize_locked", "locked_categories", "is_locked", "outcome_of", "first_locked_hit",
    "IMPLIED_LOCKED_WHEN_ADULT_OPEN", "implied_locked", "is_locked_effective",
    "rename_unlocked_hard_reasons",
    "risk_overrides_of", "resolve_persona", "grade", "classify_reason", "regrade_inbound",
    "release_hold_on_low",
]
