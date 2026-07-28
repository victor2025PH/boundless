# -*- coding: utf-8 -*-
"""出站事实声明守卫（P15，纯函数）——「AI 说的」必须对得上「登记的」。

与同族守卫的分工：
- ``offer_guard``  管**承诺措辞**（折扣/券码/赠送/客户数），白名单是目录文案；
- ``link_guard``   管**链接纪律**（当日 CTA 档外的本域链）；
- 本模块          管**事实声明与渠道卫生**：报价对不对得上目录、试用时长对不对得上
  登记事实、有没有把 gated 产品线抖出来、有没有把内部指令当文本发出去。

五个检查器全部来自 2026-07-28 本地×云端 AI 对练实录（`scripts/duel_runner.py`
六场 51 轮），每一个都有真实事故句：

- ``catalog_price``   实录：「我那套智聊AI团队版198美金一个月…算下来一个月168」
  ——同句先报 198 再报 168（≈8.5 折），嘴上拒绝打折、数字上偷偷打了。
  ``offer_guard`` 只认「N折/N%/立减N」措辞，**裸价格数字它管不着**。
- ``trial_duration`` 实录：「官网有14天客户端试用」——ChatX 真实是 7 天，
  14 天属 AvatarHub 线。``offer_guard`` 的赠送正则要求「免费」紧贴数字，
  这句语序它漏了（已实测）；本轴按**事实**（目录 ``claims.free_days``）比对，
  与措辞语序无关。
- ``gated_line``     实录：向 ChatX 客户推「免费图片换脸」。``site_catalog``
  铁律：ReachX/FaceX 类绝不入目录、绝不在聊天外发。
- ``directive_leak`` 实录：回复末尾直接出现 ``[PHOTO object shopping bag ...]``
  ——出图指令没被消费就当字面文本发给客户了。
- ``reply_language`` 回复语种漂移。**只检测不在此处纠正**——根因在上游语言决策
  （``lang_policy.classify_evidence`` 的独有脚本碎片被判强证据，已于同日修复），
  出站层再翻一次只会雪上加霜。保留检测供对练裁判与观测。

评测侧金标语料与门禁见 ``src/eval/outbound_claim_eval.py`` /
``tests/test_outbound_claim_eval.py``（含探测器有效性自证）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── 1. 报价与目录不符 ────────────────────────────────────────────────────────
# 产品/套餐词：只有它们出现时，消息里的价格数字才按「我方报价」校验。
# （人设自家咖啡店/代购报价——「芒果干450比索」——没有这些词，天然不误伤。）
_PLAN_TOKENS = (
    "智聊", "chatx", "通译", "lingox",
    "入门版", "团队版", "旗舰版", "专业版", "字符包",
    "autochat-entry", "autochat-team", "autochat-flagship",
    "translate-team", "translate-pro", "translate-charpack",
    "starter", "flagship",
)
# 价格数字：带货币符/单位的数才算报价（「三个客服」这种纯数量不抓）
_PRICE_RE = re.compile(
    r"(?:[$¥￥]\s*(\d{1,5}(?:\.\d{1,2})?)"
    r"|(\d{1,5}(?:\.\d{1,2})?)\s*(?:美金|美元|块|元|刀|usd|rmb))",
    re.IGNORECASE,
)
# 周期报价（无货币符也算）：「一个月198」「198/月」
_PRICE_PERIOD_RE = re.compile(
    r"(?:一个月|每月|月付|/\s*月|per\s*month|/\s*mo)\s*(\d{1,5})"
    r"|(\d{1,5})\s*(?:/\s*月|每月|一个月|per\s*month|/\s*mo)",
    re.IGNORECASE,
)
# 周期/报价语境词：小句里有它（或套餐词）才把数字当「我方报价」读
_PERIOD_CTX_RE = re.compile(
    r"一个月|每月|月付|年付|/\s*月|per\s*month|/\s*mo|起步价|定价|报价|多少钱",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_KEEP_RE = re.compile(r"([，,。！!？?；;\n])")


def catalog_prices(catalog: Optional[Dict[str, Any]]) -> List[float]:
    """目录里登记的合法价格数值（products.price_from + plans[].price）。"""
    out: List[float] = []
    try:
        for prod in ((catalog or {}).get("products") or []):
            if not isinstance(prod, dict):
                continue
            for raw in [prod.get("price_from")] + [
                    p.get("price") for p in (prod.get("plans") or [])
                    if isinstance(p, dict)]:
                for m in re.finditer(r"(\d{1,5}(?:\.\d{1,2})?)", str(raw or "")):
                    try:
                        out.append(float(m.group(1)))
                    except ValueError:
                        continue
    except Exception:  # noqa: BLE001 — 守卫绝不因目录脏形状崩
        return out
    return out


def _clauses(text: str) -> List[str]:
    return [c for c in re.split(r"[，,。！!？?；;\n]", str(text or "")) if c.strip()]


# 裸数字（仅 product_context + 周期语境时启用）：2~5 位，允许千分位
_BARE_NUM_RE = re.compile(r"(\d{2,3}(?:,\d{3})+|\d{2,5})")
# 紧跟这些量词的数字**不是**报价（200单 / 3个客服 / 5天 / 12人 / 10万块）
_NON_MONEY_UNIT_RE = re.compile(
    r"[单人家个次天条位台间款种份笔号年月日小时分秒万千亿%％倍]")
# 小句谈的是**别人的钱**（客户成本/收益、人设自家开销）→ 整个小句不按我方报价读。
# 2026-07-28 开生产开关前实测：不加这一层，跨轮模式对销售最常用的 ROI 话术
# （「一个月省下2000块人力成本」「客服月薪4500块」）误报 6/9——`块` 是货币单位，
# 现有正则一律当报价。宁漏勿误伤：真报价若碰巧带这些词（「一个月成本才168」）
# 会被放过，代价远小于把每句 ROI 都剥掉。
_OTHER_MONEY_CTX_RE = re.compile(
    r"省下|省出|节省|省\d|能省|赚|挣|成本|工资|月薪|薪水|社保|房租|租金|水电|"
    r"流水|营收|营业额|利润|毛利|预算|开销|花掉|亏|投入产出|回本")


# 「这段在谈我方商业事项」的判据（供**无目标会话**收窄折扣轴，见
# ``mentions_our_commerce``）。刻意分两组：我方给出动作 + 我方商业语境。
_OUR_OFFER_ACT_RE = re.compile(
    r"给你|给您|帮你|帮您|我给|我们给|我这边|我们这边|私下|走我(?:的)?渠道"
    r"|(?:can|could|will)\s+(?:give|offer)\s+you|for\s+you")
_OUR_COMMERCE_CTX_RE = re.compile(
    r"官网|下单|付款|支付|续费|订阅|套餐|入门版|团队版|旗舰版|专业版|完整版"
    r"|字符包|字符|注册领|注册就|年付|月付|底价|定价|报价|优惠码|折扣码|激活码"
    r"|试用|演示|部署|开通|我们公司|公司(?:政策|规定|底价|定价)|bd2026"
    r"|checkout|subscribe|pricing|trial|demo|coupon|sign\s*up", re.IGNORECASE)


def mentions_our_commerce(text: str) -> bool:
    """这段文字是否在谈**我方**的商业事项（产品/定价/试用/下单）。

    存在理由（2026-07-28 覆盖扩大后实测的误报面）：出站守卫从「只管有漏斗目标
    的会话」扩到全会话后，**措辞轴**（``offer_guard`` 抓 N 折/券码/赠送）开始
    误伤纯陪聊——「楼下那家奶茶店今天打八折」被剥成「我下班去买一杯」，
    「我看中的裙子五折了」剥成「终于降价啦」。措辞轴按「有没有 N 折」判，
    不问**谁在打折**；在带货会话里这没问题（说折扣几乎一定是我方承诺），
    放到陪聊就成了语义破坏。

    口径（与 ``_OTHER_MONEY_CTX_RE`` 同哲学：词表排除、宁漏勿误伤）＝
    我方给出动作（「我给你打个八折」）**或** 我方商业语境（套餐/年付/底价/
    官网/试用…，覆盖「年付85折已经是公司底价了」这类无第二人称的我方定价陈述）
    **或** 产品名（``_PLAN_TOKENS``）。三者皆无 → 判为在聊别人家的事，放行。

    已知边界（刻意接受）：段级判定，所以「同一段里既提官网又提奶茶店打八折」
    会被当我方语境。真实回复里这种混合极少，而反过来（逐句判）会漏掉跨句的
    我方定价陈述——那类才是真事故。
    """
    body = str(text or "")
    if not body.strip():
        return False
    low = body.lower()
    if any(tok in low for tok in _PLAN_TOKENS):
        return True
    return bool(_OUR_OFFER_ACT_RE.search(body)
                or _OUR_COMMERCE_CTX_RE.search(body))


def find_price_mismatch(
    text: str, *, allowed_prices: Iterable[float] = (),
    product_context: bool = False,
) -> List[str]:
    """报价与目录不符 → 返回命中片段。

    两级作用域（2026-07-28 实录校准）：**整条消息**须出现产品/套餐词（人设自家
    生意报价天然不进判定），**小句**须有套餐词或周期/报价语境词才校验其中数字
    ——真实事故「团队版198美金一个月，算下来一个月168」里越权数字落在第二小句，
    只看同句会漏；但放宽到整条又会误伤「我店里咖啡一杯30块」，故加周期闸。

    ``product_context=True``（同日**二次**实录校准）：跳过消息级产品词要求，只靠
    小句级周期闸。修复验证跑里 LLM 把产品词与越权数字**拆到两轮**说——T2「团队版
    198美金」、T3「一个月摊下来也就168」——单条口径把 T3 整条跳过，168 照样发给
    客户。调用方在「已确知本会话在谈我方产品」（会话里出现过套餐词 / 已注入产品
    目录 / 存在带货目标）时置 True；人设自家报价仍由周期闸兜住（「一杯38块」
    无周期语境不判）。
    """
    allowed = {round(float(v), 2) for v in (allowed_prices or ())}
    if not allowed:
        return []
    body = str(text or "").lower()
    if not product_context and not any(tok in body for tok in _PLAN_TOKENS):
        return []
    hits: List[str] = []
    for clause in _clauses(text):
        low = clause.lower()
        has_period = bool(_PERIOD_CTX_RE.search(clause))
        if not (any(tok in low for tok in _PLAN_TOKENS) or has_period):
            continue
        # 小句在谈客户成本/收益或人设自家开销 → 里面的钱不是我方报价，整句跳过
        if product_context and _OTHER_MONEY_CTX_RE.search(clause):
            continue
        rxs = [_PRICE_RE, _PRICE_PERIOD_RE]
        # 已确知产品语境 + 小句有周期词 → 连**裸数字**也当报价读。实录二次逃逸
        # 「一个月摊下来也就168」里周期词离数字 5 个字，紧邻式正则一个都抓不到。
        # 非货币量词（200单/三个人/5天）由 _NON_MONEY_UNIT_RE 排除，防误伤。
        if product_context and has_period:
            rxs.append(_BARE_NUM_RE)
        for rx in rxs:
            for m in rx.finditer(clause):
                raw = next((g for g in m.groups() if g), None)
                if raw is None:
                    continue
                if rx is _BARE_NUM_RE:
                    tail = clause[m.end():m.end() + 1]
                    if tail and _NON_MONEY_UNIT_RE.match(tail):
                        continue
                try:
                    val = round(float(raw.replace(",", "")), 2)
                except ValueError:
                    continue
                if val not in allowed:
                    hits.append(m.group(0).strip())
    return hits


# ── 2. 试用时长与登记事实不符 ────────────────────────────────────────────────
_TRIAL_CTX_RE = re.compile(r"试用|体验|免费用|免费使用|白嫖|trial|free\s+for", re.I)
_TRIAL_DUR_RE = re.compile(
    r"(\d{1,3}|[一两二三四五六七八九十半]{1,3})\s*(个月|月|周|天|days?|weeks?|months?)",
    re.IGNORECASE,
)
_CN_DIGITS = {"半": 0.5, "一": 1.0, "两": 2.0, "二": 2.0, "三": 3.0, "四": 4.0,
              "五": 5.0, "六": 6.0, "七": 7.0, "八": 8.0, "九": 9.0, "十": 10.0}
_UNIT_DAYS = (("个月", 30.0), ("月", 30.0), ("周", 7.0), ("天", 1.0),
              ("month", 30.0), ("week", 7.0), ("day", 1.0))


def _to_days(qty_raw: str, unit_raw: str) -> Optional[float]:
    try:
        qty = float(qty_raw)
    except ValueError:
        t = str(qty_raw)
        if len(t) == 1:
            qty = _CN_DIGITS.get(t, -1.0)
        elif "十" in t:
            head, _, tail = t.partition("十")
            tens = 1.0 if not head else _CN_DIGITS.get(head, -1.0)
            ones = 0.0 if not tail else _CN_DIGITS.get(tail, -1.0)
            qty = tens * 10.0 + ones if tens >= 0 and ones >= 0 else -1.0
        else:
            qty = -1.0
    if qty <= 0:
        return None
    unit = str(unit_raw).lower().rstrip("s")
    for key, days in _UNIT_DAYS:
        if unit.startswith(key) or key.startswith(unit):
            return qty * days
    return None


def find_trial_mismatch(
    text: str, *, allowed_free_days: Iterable[float] = (),
) -> List[str]:
    """试用语境里的时长若不等于登记时长 → 命中（与措辞语序无关）。

    未登记任何授权时长 → 不判（交由 ``offer_guard`` 赠送轴处理，避免双重口径）。
    """
    allowed = {round(float(v), 4) for v in (allowed_free_days or ()) if float(v) > 0}
    if not allowed:
        return []
    hits: List[str] = []
    for clause in _clauses(text):
        if not _TRIAL_CTX_RE.search(clause):
            continue
        for m in _TRIAL_DUR_RE.finditer(clause):
            days = _to_days(m.group(1), m.group(2))
            if days is None:
                continue
            if not any(abs(days - a) < 1e-6 for a in allowed):
                hits.append(m.group(0).strip())
    return hits


# ── 3. gated 产品线泄漏 ──────────────────────────────────────────────────────
# 只收**高置信**词；「声音克隆」等本栈自用能力刻意不抓，避免把人设自己的
# 语音能力误判成在卖 gated 线。
_GATED_RE = re.compile(
    r"换脸|face\s*swap|faceswap|facex|reachx|变脸直播|实时换声",
    re.IGNORECASE,
)


def find_gated_leak(text: str) -> List[str]:
    return [m.group(0).strip() for m in _GATED_RE.finditer(str(text or ""))]


# ── 4. 回复语种漂移（只检测，不在出站层纠正）──────────────────────────────────
_SCRIPT_RANGES = (
    ("th", (0x0E00, 0x0E7F)),
    ("ko", (0xAC00, 0xD7AF)),
    ("ja", (0x3040, 0x30FF)),          # 假名（汉字归 zh，够用）
    ("zh", (0x4E00, 0x9FFF)),
)


def script_profile(text: str) -> Dict[str, float]:
    """字符占比画像（只计有语种意义的字符；标点/emoji/数字不计）。"""
    counts: Dict[str, int] = {}
    total = 0
    for ch in str(text or ""):
        code = ord(ch)
        tag = None
        for name, (lo, hi) in _SCRIPT_RANGES:
            if lo <= code <= hi:
                tag = name
                break
        if tag is None and ch.isalpha() and code < 0x0250:
            tag = "latin"
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
            total += 1
    if not total:
        return {}
    return {k: v / total for k, v in counts.items()}


def dominant_language(texts: Sequence[str], *, min_share: float = 0.6) -> str:
    """多条文本合并后的主体语种；无明显主体 → ""（不判，宁漏勿误）。"""
    merged: Dict[str, float] = {}
    total = 0.0
    for t in texts or ():
        prof = script_profile(t)
        n = sum(1 for ch in str(t or "") if not ch.isspace())
        for k, share in prof.items():
            merged[k] = merged.get(k, 0.0) + share * n
            total += share * n
    if total <= 0:
        return ""
    top, val = max(merged.items(), key=lambda kv: kv[1])
    return top if (val / total) >= min_share else ""


def find_language_drift(
    reply: str, customer_msgs: Sequence[str], *,
    min_reply_chars: int = 12, min_share: float = 0.6,
) -> List[str]:
    """回复主体语种 ≠ 会话主体语种 → 命中（返回 ``["zh->th"]`` 形）。

    只在两侧都有**明确主体**时判：客户语种含糊、回复太短一律放过。
    """
    if len(re.sub(r"\s", "", str(reply or ""))) < min_reply_chars:
        return []
    want = dominant_language(list(customer_msgs or ()), min_share=min_share)
    got = dominant_language([reply], min_share=min_share)
    if not want or not got or want == got:
        return []
    return [f"{want}->{got}"]


# ── 5. 内部指令语法泄漏 ──────────────────────────────────────────────────────
_DIRECTIVE_RE = re.compile(
    r"\[\s*(?:PHOTO|IMAGE|IMG|VOICE|AUDIO|VIDEO|STICKER|MEDIA)\b[^\]]*\]"
    r"|\{\{\s*\w+\s*\}\}"
    r"|<\s*(?:photo|image|voice|media)\s*:[^>]*>",
    re.IGNORECASE,
)


def find_directive_leak(text: str) -> List[str]:
    return [m.group(0).strip()[:60] for m in _DIRECTIVE_RE.finditer(str(text or ""))]


# ── 汇总检测入口（探测器；不改文本）─────────────────────────────────────────
CHECKS = ("catalog_price", "trial_duration", "gated_line",
          "reply_language", "directive_leak")


def check_outbound_claims(
    reply: str, *,
    allowed_prices: Iterable[float] = (),
    allowed_free_days: Iterable[float] = (),
    customer_msgs: Sequence[str] = (),
    product_context: bool = False,
) -> List[Tuple[str, str]]:
    """一次跑全部检查，返回 ``[(kind, 命中片段), ...]``（绝不抛）。

    ``product_context``：已确知本会话在谈我方产品 → 报价轴跳过消息级产品词要求
    （见 ``find_price_mismatch``；默认 False＝旧行为，零影响）。
    """
    out: List[Tuple[str, str]] = []
    try:
        for frag in find_price_mismatch(reply, allowed_prices=allowed_prices,
                                        product_context=product_context):
            out.append(("catalog_price", frag))
        for frag in find_trial_mismatch(reply, allowed_free_days=allowed_free_days):
            out.append(("trial_duration", frag))
        for frag in find_gated_leak(reply):
            out.append(("gated_line", frag))
        for frag in find_language_drift(reply, customer_msgs):
            out.append(("reply_language", frag))
        for frag in find_directive_leak(reply):
            out.append(("directive_leak", frag))
    except Exception:  # noqa: BLE001
        return out
    return out


# ── 守卫动作（改文本）───────────────────────────────────────────────────────
_HUSK_RE = re.compile(r"^[\s\-–—·:：,，.。;；!！?？()（）\[\]【】\"'「」~～]*$")
# 剥空时的兜底话术（与 offer_guard.compliant_fallback 同族：整条即错误声明时，
# 回退原文＝把错的价格/试用照发，等于没守）
_FALLBACK_ZH = "具体价格和试用政策都以官网为准哦，我怕记错说错，你直接看官网最准"
_FALLBACK_EN = ("Pricing and trial terms follow the official site — I'd rather "
                "you check there than risk me quoting it wrong.")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def compliant_fallback(text: str = "") -> str:
    return _FALLBACK_ZH if _CJK_RE.search(str(text or "")) else _FALLBACK_EN


# 出站层实际处置的类别：语种漂移不在此列（根因在上游语言决策，出站再翻更糟）
_STRIP_KINDS = ("catalog_price", "trial_duration", "gated_line")


def sanitize_outbound_claims(
    text: str, *,
    allowed_prices: Iterable[float] = (),
    allowed_free_days: Iterable[float] = (),
    product_context: bool = False,
) -> Tuple[str, int, List[str]]:
    """剥掉「与登记事实不符」的小句 + 摘掉泄漏的内部指令。

    返回 ``(text, 处置条数, 命中片段)``。与 ``offer_guard`` 同口径：
    只删命中所在的**小句**；整条被剥空 → 换合规兜底话术（绝不回退原文——
    原文就是那句错价格/错试用）；解析异常 → 原文（守卫故障不该吃掉回复）。

    ``product_context``：本会话已确知在谈我方产品 → 报价轴放宽到跨轮
    （见 ``find_price_mismatch``）。带货会话（有 ``_goal_cta``）天然满足。
    """
    t = str(text or "")
    if not t:
        return t, 0, []
    try:
        handled: List[str] = []
        # 指令泄漏：整体摘除标记本身即可，不必牵连整句语义
        leaks = find_directive_leak(t)
        if leaks:
            t = _DIRECTIVE_RE.sub("", t)
            handled.extend(leaks)

        bad_frags: List[str] = []
        for frag in find_price_mismatch(t, allowed_prices=allowed_prices,
                                        product_context=product_context):
            bad_frags.append(frag)
        for frag in find_trial_mismatch(t, allowed_free_days=allowed_free_days):
            bad_frags.append(frag)
        for frag in find_gated_leak(t):
            bad_frags.append(frag)

        if bad_frags:
            parts = _CLAUSE_SPLIT_KEEP_RE.split(t)
            out: List[str] = []
            for part in parts:
                if not part:
                    continue
                if _CLAUSE_SPLIT_KEEP_RE.fullmatch(part):
                    if out and out[-1] == "\x00":
                        continue
                    out.append(part)
                    continue
                hit = next((f for f in bad_frags if f and f in part), "")
                if hit:
                    handled.append(hit)
                    out.append("\x00")
                else:
                    out.append(part)
            t = "".join(p for p in out if p != "\x00")

        if not handled:
            return str(text or ""), 0, []
        lines: List[str] = []
        for line in t.split("\n"):
            line = re.sub(r"[ \t\u3000]{2,}", " ", line).strip()
            if _HUSK_RE.match(line):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        if not cleaned:
            return compliant_fallback(text), len(handled), handled
        return cleaned, len(handled), handled
    except Exception:  # noqa: BLE001
        return str(text or ""), 0, []


__all__ = [
    "CHECKS", "catalog_prices", "check_outbound_claims", "compliant_fallback",
    "dominant_language", "find_directive_leak", "find_gated_leak",
    "find_language_drift", "find_price_mismatch", "find_trial_mismatch",
    "mentions_our_commerce",
    "sanitize_outbound_claims", "script_profile",
]
