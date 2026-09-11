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
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LEVELS: Tuple[str, ...] = ("low", "medium", "high")
_RANK = {"low": 1, "medium": 2, "high": 3}
MEDIUM_TAG = "risk:medium"           #: 中级命中只打这一枚会话标签（不进 needs_human）
REASON_PREFIX = "risk:"              #: 本模块补进 peer_reasons 的细因前缀（risk:<category>）
PRIVACY_MENTION_REASON = "privacy_mention"   #: 被降为叙述的 quick_analyze ``privacy`` 主因改名，防下游按 privacy 处置

#: 真高风险因子（quick_analyze / Q-15 已判）——在场一律不降
TRUE_HIGH_REASONS = frozenset({"self_harm", "stop_contact", "credential_or_payment_request", "money", "adult"})

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
    _LB + r"i(?:'m| am)\s+(?:only\s+|just\s+)?1[0-7](?:\s+(?:years?\s+old|yo|y/o|yrs?))?" + _RB,
    _LB + r"i(?:'m| am)\s+(?:a\s+)?minor" + _RB,
    _LB + r"i(?:'m| am)\s+under\s*(?:age|18)" + _RB,
    _LB + r"(?:turning|turn)\s+1[0-7]\s+(?:next|this)" + _RB,
    _LB + r"i(?:'m| am)\s+(?:still\s+)?in\s+(?:middle|junior\s+high|8th|9th|10th)\s+(?:school|grade)" + _RB,
    r"我(今年|才|刚|只有|还)?(?:1[0-7])岁|我未成年|我(还)?是未成年|我(还)?在(读|上)(初中|初一|初二|初三)|我(还)?是(初中生|小学生)",
    r"(?:1[0-7])歳(です|なんだ)|未成年(です|なんだ)|中学生(です|なんだ)",
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
    {"id": "payment_keyword", "level": "high", "floor": "high", "overridable": False, "action": "needs_human",
     "source": "drafts._SENSITIVE_PATTERNS[0]",
     "words": {"en": ["refund", "payment", "wire transfer", "password", "OTP", "deposit"], "zh": ["退款", "付款", "转账", "银行卡", "密码", "验证码"]}},
    {"id": "adult", "level": "medium", "floor": "medium", "overridable": False, "action": "adult_policy",
     "source": "adult_grader（Q-15：pressure→high；explicit→人设 adult_policy；mention/flirt→放行）",
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


def category_def(cid: str) -> Dict[str, Any]:
    return dict(_CAT_BY_ID.get(str(cid or ""), {}))


def public_table(persona: Any = None, cfg: Any = None) -> List[Dict[str, Any]]:
    """回复设置页「风控分级」卡数据：类别 / 词表摘要 / 级别 / 动作 / 是否可覆写 / 人设覆写后的有效级别。"""
    ov = risk_overrides_of(persona)
    out: List[Dict[str, Any]] = []
    for c in CATEGORIES:
        row = dict(c)
        row["words"] = {k: list(v) for k, v in (c.get("words") or {}).items()}
        row["effective_level"] = ov.get(c["id"], c["level"]) if c.get("overridable") else c["level"]
        row["override"] = ov.get(c["id"], "") if c.get("overridable") else ""
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
            if "self_harm" in rs:
                _add("self_harm", next((h for h in in_hits if h), "self_harm"))
            if "stop_contact" in rs:
                _add("stop_contact", "stop_contact")
            if "credential_or_payment_request" in rs or "money" in rs:
                _add("money_request", next((h for h in in_hits if h), "money"))
            if "adult" in rs:
                # Q-15 已判 explicit×(human|soft_reply) / pressure → high，本模块只记类别不改判
                _add("adult", next((h for h in in_hits if h.startswith("adult") or h), "adult"), "high")
            elif "adult_mark" in rs:
                _add("adult", "adult", "medium")      # explicit × mark_only：中，只标
            # adult_flirt（mention / flirt）＝Q-15 放行档，不进本模块类别（不打 risk:medium）
            for pats, cat in ((_THREAT, "threat"), (_MINOR, "minor"), (_SCAM, "scam")):
                hs = _hits(pats, t)
                if hs:
                    _add(cat, hs[0])
            try:
                from src.inbox.commitment_guard import detect_request
                rk = detect_request(t, lang)
            except Exception:
                rk = None
            if rk:
                _add("money_request" if rk == "money" else "request_" + rk, "request:" + rk)
            if _SP:
                if _hits([_SP[0][0]], t):
                    _add("payment_keyword", _hits([_SP[0][0]], t)[0])
                if len(_SP) > 1 and _hits([_SP[1][0]], t):
                    _add("complaint", _hits([_SP[1][0]], t)[0])
                if len(_SP) > 2:
                    hs = _hits([_SP[2][0]], t)
                    if hs:
                        guess = next((k for k, p in _GUESS_KIND if p.search(t)), "meet")
                        if guess == "money":
                            _add("money_request", hs[0])
                        else:
                            _add("request_" + guess, hs[0])
                if len(_SP) > 3 and _hits([_SP[3][0]], t):
                    _add("narrative", _hits([_SP[3][0]], t)[0])
            if "privacy" in rs and not rk:
                _add("privacy", next((h for h in in_hits if h), "privacy"))
        # 人设覆写（只对可覆写类别）
        ov = risk_overrides_of(persona)
        for f in found:
            if f["category"] in ov:
                f["level"] = ov[f["category"]]
        if not found:
            return {"level": "low", "category": "", "hits": [], "action": "", "found": [], "direction": direction}
        found.sort(key=lambda f: (-_RANK.get(f["level"], 0), _CAT_ORDER.get(f["category"], 99)))
        top = found[0]
        cdef = _CAT_BY_ID.get(top["category"], {})
        out_hits: List[str] = []
        for f in found:
            tag = f["hit"] if f["hit"].startswith(("request:", "commitment:")) else f"{f['category']}:{f['hit']}"
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


def _downgradable(reasons: Sequence[str], text: str) -> bool:
    """原 high 是否**只**由可降因子撑起：无真高风险主因、且支付词表未中。"""
    for r in reasons:
        if str(r) in TRUE_HIGH_REASONS:
            return False
    try:
        from src.inbox.drafts import _SENSITIVE_PATTERNS as _SP
        if _SP and _SP[0][0].search(str(text or "")):
            return False
    except Exception:
        return False
    return True


def _tag_medium(store: Any, cid: str) -> bool:
    try:
        if store is None or not cid or not hasattr(store, "get_conv_tags"):
            return False
        tags = list(store.get_conv_tags(cid) or [])
        if MEDIUM_TAG in tags:
            return False
        tags.append(MEDIUM_TAG)
        store.set_conv_tags(cid, tags)
        return True
    except Exception:
        logger.debug("[risk] risk:medium 打标失败（忽略）", exc_info=True)
        return False


def regrade_inbound(svc: Any, conv: Dict[str, Any], text: str, lang: str, risk_level: str,
                    peer_reasons: Sequence[str], risk_hits: Any, *,
                    automation_mode: str = "auto_ai", cfg: Any = None, persona: Any = None,
                    now: Optional[float] = None) -> Tuple[str, List[str], Optional[Dict[str, Any]]]:
    """``(risk_level, peer_reasons, info)``。drafts.py 钩子入口（Q-15 同式）。任何异常 → 原判定放行。

    - 分级 high：``max(原, high)`` + reasons 追加 ``risk:<category>``（新表 threat / minor / scam /
      索钱句式才会新增；原已 high 的只补细因）。打标仍由 decide→review_required→tag_needs_human 走现状路。
    - 分级 medium：原 high 且只由可降因子撑起 → 降 medium（``privacy``→``privacy_mention``）；会话打
      ``risk:medium`` 标签；日志 ``[risk] medium``。
    - 分级 low：同上降 low；只落 ``[risk] low conv= category= hits=``。
    """
    reasons = [str(r) for r in (peer_reasons or [])]
    try:
        cid = str((conv or {}).get("conversation_id") or "")
        store = getattr(svc, "_store", None)
        if cfg is None:
            cfg = getattr(svc, "_cfg", None)
        if persona is None:
            persona = resolve_persona(conv, cfg)
        g = grade(text, "in", persona, lang=lang, reasons=reasons, hits=list(risk_hits or []), cfg=cfg)
        level = str(g.get("level") or "low")
        cat = str(g.get("category") or "")
        ghits = [str(h) for h in (g.get("hits") or [])]
        info: Dict[str, Any] = {"level": level, "category": cat, "hits": ghits, "action": g.get("action") or "",
                                "downgraded": False, "from": str(risk_level or "low")}
        if isinstance(risk_hits, list):
            for h in ghits:
                if h not in risk_hits:
                    risk_hits.append(h)
        cur = str(risk_level or "low").lower()
        if not cat:
            return risk_level, reasons, info
        if level == "high":
            new = _max_level(cur, "high")
            tag = REASON_PREFIX + cat
            if tag not in reasons:
                reasons.append(tag)
            logger.info("[risk] high conv=%s category=%s hits=%s action=%s from=%s",
                        cid or "-", cat, "|".join(ghits[:4]) or "-", info["action"], cur)
            return new, reasons, info
        new = cur
        if cur == "high" and _downgradable(reasons, text):
            new = level
            info["downgraded"] = True
            reasons = [PRIVACY_MENTION_REASON if r == "privacy" else r for r in reasons]
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
        return new, reasons, info
    except Exception:
        logger.debug("[risk] regrade 异常（原判定放行）", exc_info=True)
        return risk_level, reasons, None


__all__ = [
    "LEVELS", "MEDIUM_TAG", "REASON_PREFIX", "PRIVACY_MENTION_REASON", "TRUE_HIGH_REASONS",
    "CATEGORIES", "OVERRIDABLE", "category_def", "public_table", "normalize_level",
    "risk_overrides_of", "resolve_persona", "grade", "classify_reason", "regrade_inbound",
]
