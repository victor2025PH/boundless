"""出站「优惠承诺」守卫（P14，纯函数）——把「不发明折扣」从叮嘱变成硬约束。

P13 把优惠收敛成目录里运营授权的活动，但那只是 prompt 里的纪律：LLM 仍可能
自己编「给你打个8折」「优惠码 SUWAN50」「免费用一个月」——官网收全价 = 当场
翻车，且这是**承诺类**事故（比链接越纪律更贵）。与 ``link_guard`` /
``media_promise_guard`` 同族的确定性兜底：出站文本里出现**未授权**的折扣/券码/
赠送承诺 → 剥掉那一小句。

保守取向（宁可漏拦，不可误伤）：
- 只在带货会话生效（调用方有 ``_goal_cta`` 暂存才跑）；
- 命中文本落在**当日授权活动文案**里 → 放行（那正是许诺得起的话）；
- 句中带否认/婉拒语气（「暂时没有折扣」「不能私自打折」）→ 放行——这恰是
  我们希望 LLM 说的话，剥了反而把合规回答毁掉；
- 免费时长类命中另按**授权时长**放行（``allowed_free_days``，见下）；
- 只删命中所在的**小句**（逗号级），不是整句更不是整段；剥空则原样返回。

免费时长为何不走子串白名单（2026-07-28）：客户端真有「注册领 7 天完整版」，
但子串口径要求白名单里逐字出现「免费试用7天」才放行——运营得去猜 LLM 的语序
（实测「免费领 7 天」放行而「免费试用 7 天」被剥，同一事实两种命运），且漏一种
写法就把真事实换成「我这边不能私自给折扣」的合规兜底＝对客户说谎。改为把命中
解析成天数与目录登记的授权时长比对：登记 7 → 「7 天」「一周」放行，「14 天」
「一个月」照剥（14 天试用属 AvatarHub 线，本目录刻意不收）。授权的是**事实**
不是措辞。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Set, Tuple

# 未授权即算承诺的表达（各自都要求「数字/码」，纯「优惠」二字不抓）
_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    # 打折：8折 / 打8.5折 / 八折（数字紧邻「折」，不会误伤「转折/折腾」）
    ("discount", re.compile(
        r"(?:打)?\s*(?:[0-9](?:\.[0-9])?|[一二两三四五六七八九](?:点[0-9])?)\s*折")),
    # 百分比：30% off / 优惠30% / 减20%
    ("percent", re.compile(
        r"(?:\d{1,2}\s*%\s*(?:off|discount|优惠|折扣)"
        r"|(?:优惠|减免|立减|降|便宜)\s*\d{1,2}\s*%)", re.I)),
    # 直降/立减/返现金额
    ("cashback", re.compile(
        r"(?:立减|直降|返现|返)\s*[¥$￥]?\s*\d{1,5}(?:\s*(?:元|块|美金|刀))?")),
    # 券码：优惠码 SUWAN50 / promo code XX
    ("coupon", re.compile(
        r"(?:优惠码|折扣码|兑换码|promo\s*code|coupon(?:\s*code)?)\s*"
        r"[:：]?\s*[A-Za-z0-9_\-]{3,16}", re.I)),
    # 白送时长：免费用一个月 / 送你 30 天 / free 14 days（汉字数量词同抓）；
    # 反序「开14天免费试用」（数字在前）同抓——P15 评测轨首跑抓出的盲区，
    # 反序侧要求「免费」后紧跟使用类动词，防误伤「三天免费市集」这类闲聊
    ("freebie", re.compile(
        r"(?:免费(?:试用|使用|用|送)?|白送|送你|多送|加送)\s*"
        r"(?:[0-9]{1,3}|[一两二三四五六七八九十半]{1,3})\s*(?:天|周|个月|月)"
        r"|(?:[0-9]{1,3}|[一两二三四五六七八九十半]{1,3})\s*(?:天|周|个月|月)"
        r"\s*的?\s*免费(?:试用|使用|体验|用)"
        r"|free\s+\d{1,3}\s*(?:days?|weeks?|months?)", re.I)),
    # 编客户数（P15）：已有500家在用 / trusted by 800+ businesses——带具体数字的
    # 社会证明是最常见的销售幻觉；真实数据运营登记进 claims.texts 才许说。
    # 只抓「动词前缀 + 阿拉伯数字 + 客户类名词」：汉字数量词（"帮了三个朋友"）、
    # 无数字（"很多客户"）、百分比（"超过30%的客户"）都刻意不抓，宁漏不误伤。
    ("traction", re.compile(
        r"(?:已有|已经有|已服务|服务了|服务过|帮过|帮了|超过|累计(?:服务)?)\s*"
        r"\d[\d,，.]*\s*[万wkK]?\s*[+＋]?\s*(?:多|余)?\s*[家位个名]?\s*"
        r"(?:客户|用户|商家|企业|团队|老板|门店|店家|品牌)"
        r"|(?:trusted|used)\s+by\s+(?:over\s+)?\d[\d,]*\+?\s*"
        r"(?:businesses|customers|users|merchants|teams|stores|brands)"
        r"|(?:over|more\s+than)\s+\d[\d,]*\s*"
        r"(?:businesses|customers|users|merchants|teams|stores|brands)\s+"
        r"(?:use|trust|rely)", re.I)),
)

# 否认/婉拒语气：句中带这些词说明 AI 在**拒绝**给折扣，是合规回答，放行
_REFUSAL_RE = re.compile(
    r"不能|不可以|没法|无法|不敢|不方便|没有|暂时没|目前没|不打折|不搞|"
    r"不提供|不做|做不了|说了不算|得问|要问|问一下|不是我能|统一价|官网价|"
    r"can't|cannot|no discount|not able")

# 小句切分（保留分隔符，剥完好还原节奏）
_CLAUSE_SPLIT_RE = re.compile(r"([，,。！!？?；;\n])")
# 剥完只剩标点/空白 → 整块丢弃
_HUSK_RE = re.compile(r"^[\s\-–—·:：,，.。;；!！?？()（）\[\]【】\"'「」~～]*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _authorized(fragment: str, allowed_norm: str) -> bool:
    """命中片段是否落在授权活动文案里（归一化后子串包含）。"""
    frag = _norm(fragment)
    return bool(frag and allowed_norm and frag in allowed_norm)


# ── 免费时长解析（把「免费试用 7 天」「免费用一个月」折成天数）─────────────
_CN_DIGITS = {"半": 0.5, "一": 1.0, "两": 2.0, "二": 2.0, "三": 3.0, "四": 4.0,
              "五": 5.0, "六": 6.0, "七": 7.0, "八": 8.0, "九": 9.0, "十": 10.0}
# 月按 30 天折算：「免费用一个月」vs 授权 7 天要判不等，量级对得上就够
_UNIT_DAYS = (("个月", 30.0), ("月", 30.0), ("周", 7.0), ("天", 1.0),
              ("month", 30.0), ("week", 7.0), ("day", 1.0))


def _cn_number(s: str) -> Optional[float]:
    """中文数量词 → 数值（半/一/两/十/十五/二十/二十三）。认不出 → None。"""
    t = str(s or "").strip()
    if not t:
        return None
    if len(t) == 1:
        return _CN_DIGITS.get(t)
    if "十" in t:
        head, _, tail = t.partition("十")
        tens = 1.0 if not head else _CN_DIGITS.get(head, -1.0)
        ones = 0.0 if not tail else _CN_DIGITS.get(tail, -1.0)
        if tens < 0 or ones < 0:
            return None
        return tens * 10.0 + ones
    return None


_QTY_UNIT_RE = re.compile(
    r"(\d{1,3}|[一两二三四五六七八九十半]{1,3})\s*"
    r"(个月|月|周|天|months?|weeks?|days?)", re.I)


def freebie_days(fragment: str) -> Optional[float]:
    """免费时长命中片段 → 折算天数（解析不出 → None，按未授权处理）。"""
    m = _QTY_UNIT_RE.search(str(fragment or ""))
    if not m:
        return None
    raw, unit = m.group(1), m.group(2).lower().rstrip("s")
    try:
        qty = float(raw)
    except ValueError:
        qty = _cn_number(raw)
    if qty is None or qty <= 0:
        return None
    for key, days in _UNIT_DAYS:
        if unit.startswith(key) or key.startswith(unit):
            return qty * days
    return None


def _authorized_days(fragment: str, allowed_days: Set[float]) -> bool:
    """免费时长是否等于目录登记的授权时长（7 天 ≡ 一周）。"""
    if not allowed_days:
        return False
    d = freebie_days(fragment)
    return d is not None and any(abs(d - a) < 1e-6 for a in allowed_days)


def _norm_days(allowed_free_days: Iterable[float]) -> Set[float]:
    """授权时长集合归一（脏值忽略，绝不抛——守卫宁可全拦不可崩）。"""
    out: Set[float] = set()
    for v in allowed_free_days or ():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.add(f)
    return out


def _passes(kind: str, fragment: str, allowed_norm: str,
            allowed_days: Set[float]) -> bool:
    """命中片段是否许诺得起（子串白名单 或 免费时长授权）。"""
    if _authorized(fragment, allowed_norm):
        return True
    return kind == "freebie" and _authorized_days(fragment, allowed_days)


def find_offer_claims(
    text: str, *, allowed_texts: Iterable[str] = (),
    allowed_free_days: Iterable[float] = (),
) -> List[Tuple[str, str]]:
    """列出未授权的优惠承诺 ``[(kind, 命中片段), ...]``（不改文本，供门禁/观测）。"""
    t = str(text or "")
    if not t:
        return []
    allowed_norm = _norm(" ".join(str(x or "") for x in allowed_texts))
    allowed_days = _norm_days(allowed_free_days)
    hits: List[Tuple[str, str]] = []
    for part in _CLAUSE_SPLIT_RE.split(t):
        if not part or _CLAUSE_SPLIT_RE.fullmatch(part):
            continue
        if _REFUSAL_RE.search(part):
            continue
        for kind, rx in _PATTERNS:
            for m in rx.finditer(part):
                if not _passes(kind, m.group(0), allowed_norm, allowed_days):
                    hits.append((kind, m.group(0).strip()))
    return hits


# 整条回复都是承诺时的兜底话术（与 media_promise_guard 的「撤回+圆场」同族：
# 这里绝不能像 link_guard 那样回退原文——原文就是那句要命的折扣承诺）
_FALLBACK_ZH = "价格和优惠都以官网为准哦，我这边不能私自给折扣～需要的话我帮你问问官方客服"
_FALLBACK_EN = ("Pricing and promos follow the official site — I can't offer "
                "discounts on my own, but I can ask our support team for you.")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def compliant_fallback(text: str = "") -> str:
    """整条被剥空时的合规替代话术（按原文语种取中/英）。"""
    return _FALLBACK_ZH if _CJK_RE.search(str(text or "")) else _FALLBACK_EN


def sanitize_offer_claims(
    text: str, *, allowed_texts: Iterable[str] = (),
    allowed_free_days: Iterable[float] = (),
) -> Tuple[str, int, List[str]]:
    """剥掉未授权优惠承诺所在的小句。返回 ``(text, 剥离条数, 命中片段)``。

    整条都是承诺（剥完为空）→ **换成合规兜底话术**，而不是像 link_guard 那样
    回退原文：短消息「给你打个8折」正是最典型的事故形态，放行等于没守。
    解析异常 → 原文（守卫自身故障不该吃掉回复）。
    """
    t = str(text or "")
    if not t:
        return t, 0, []
    try:
        allowed_norm = _norm(" ".join(str(x or "") for x in allowed_texts))
        allowed_days = _norm_days(allowed_free_days)
        parts = _CLAUSE_SPLIT_RE.split(t)
        stripped: List[str] = []
        out: List[str] = []
        for part in parts:
            if not part:
                continue
            if _CLAUSE_SPLIT_RE.fullmatch(part):
                # 分隔符：前一块被剥掉时不留悬空标点
                if out and out[-1] == "\x00":
                    continue
                out.append(part)
                continue
            bad = ""
            if not _REFUSAL_RE.search(part):
                for _kind, rx in _PATTERNS:
                    m = rx.search(part)
                    if m and not _passes(
                            _kind, m.group(0), allowed_norm, allowed_days):
                        bad = m.group(0).strip()
                        break
            if bad:
                stripped.append(bad)
                out.append("\x00")
            else:
                out.append(part)
        if not stripped:
            return t, 0, []
        merged = "".join(p for p in out if p != "\x00")
        lines: List[str] = []
        for line in merged.split("\n"):
            line = re.sub(r"[ \t\u3000]{2,}", " ", line).strip()
            if _HUSK_RE.match(line):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        if not cleaned:
            return compliant_fallback(t), len(stripped), stripped
        return cleaned, len(stripped), stripped
    except Exception:
        return t, 0, []


__all__ = ["compliant_fallback", "find_offer_claims", "freebie_days",
           "sanitize_offer_claims"]
