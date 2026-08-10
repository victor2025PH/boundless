"""客户画像槽位注册表 + 确定性采集（纯数据 + 纯函数，零 IO）。

双轨画像（acquire_and_convert 漏斗的「摸底」事实源）：
- **relation 轨**（关系向）：称呼/坐标/职业/兴趣——聊得像朋友的基础。
- **bant 轨**（商机向，B2B 资格四要素扩展）：痛点 Need / 在用渠道 Channel /
  团队规模 Size / 预算 Budget / 决策角色 Authority / 上线时间 Timeline。
- **lifecycle 轨**（P8，采集专用）：流失原因 churn_reason——只在留存/挽回/
  回流会话里确定性采集（service 按 goal.created_by 门控），**不进** TRACKS
  枚举 → 不计填充率、不进 missing_slots("")/LLM 抽取/摸底缺口提示（新客户
  永远不会被问「为什么没续」）；填上后画像卡自动显示、坐席可改。

设计原则：
- 槽位登记制：新增槽位只加一条 dict（label/ask_hint/weight），fill_rates /
  missing_slots / UI 自动生效。
- ``capture_from_text`` 确定性正则，**宁可漏采不错采**（错的画像比空画像更毒——
  会被 ledger 当推进信号、被选品当依据）；模糊场景交给坐席画像卡人工补录。
- 存储形状（customer_profiles.fields JSON）：``{slot: {"v": str, "src":
  "auto|agent", "ts": float}}``——auto 只填空、绝不覆盖坐席手录（agent 覆盖一切）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

TRACKS = ("relation", "bant")

# 槽位登记表（顺序即采集/展示优先级；weight 参与 fill 率加权）
SLOTS: Tuple[Dict[str, Any], ...] = (
    # ── relation 轨 ─────────────────────────────────────────────────────────
    {"key": "name", "track": "relation", "weight": 1,
     "label_zh": "称呼", "label_en": "Name",
     "ask_zh": "怎么称呼", "ask_en": "what to call them"},
    {"key": "location", "track": "relation", "weight": 1,
     "label_zh": "坐标", "label_en": "Location",
     "ask_zh": "人在哪个城市", "ask_en": "which city they are in"},
    {"key": "occupation", "track": "relation", "weight": 2,
     "label_zh": "职业/生意", "label_en": "Occupation",
     "ask_zh": "做什么生意/工作", "ask_en": "what business they run"},
    # P26（2026-08-05）：age 槽——「获取客户年龄」是摸底类目标高频诉求，
    # 此前全系统无此槽（坐席只能写进自定义 note 软文案）。采集正则见
    # _AGE_RE/_AGE_BAND_RE（自述+岁/了 锚定，宁可漏采不错采）。
    {"key": "age", "track": "relation", "weight": 1,
     "label_zh": "年龄", "label_en": "Age",
     "ask_zh": "大概哪个年龄段", "ask_en": "roughly their age range"},
    {"key": "interests", "track": "relation", "weight": 1,
     "label_zh": "兴趣", "label_en": "Interests",
     "ask_zh": "平时喜欢做什么", "ask_en": "what they enjoy"},
    # ── bant 轨 ─────────────────────────────────────────────────────────────
    {"key": "need", "track": "bant", "weight": 3,
     "label_zh": "业务痛点", "label_en": "Pain point",
     "ask_zh": "生意上最头疼什么", "ask_en": "their biggest operational pain"},
    {"key": "channel", "track": "bant", "weight": 2,
     "label_zh": "在用平台", "label_en": "Channels",
     "ask_zh": "客户都在哪些平台上", "ask_en": "which platforms they use"},
    {"key": "team_size", "track": "bant", "weight": 1,
     "label_zh": "团队规模", "label_en": "Team size",
     "ask_zh": "几个人在做", "ask_en": "team headcount"},
    {"key": "budget", "track": "bant", "weight": 2,
     "label_zh": "预算档", "label_en": "Budget",
     "ask_zh": "在工具上愿意花多少", "ask_en": "tool budget range"},
    {"key": "authority", "track": "bant", "weight": 1,
     "label_zh": "决策角色", "label_en": "Authority",
     "ask_zh": "事情TA能不能拍板", "ask_en": "whether they decide"},
    {"key": "timeline", "track": "bant", "weight": 2,
     "label_zh": "上线时间", "label_en": "Timeline",
     "ask_zh": "什么时候想用起来", "ask_en": "when they want it live"},
    # ── lifecycle 轨（采集专用，不在 TRACKS → 不进填充率/缺口枚举）─────────
    {"key": "churn_reason", "track": "lifecycle", "weight": 1,
     "label_zh": "流失原因", "label_en": "Churn reason",
     "ask_zh": "当初为什么没续", "ask_en": "why they churned"},
)

_SLOT_BY_KEY = {s["key"]: s for s in SLOTS}


def slot_keys(track: str = "") -> List[str]:
    t = str(track or "").strip().lower()
    return [s["key"] for s in SLOTS if not t or s["track"] == t]


def get_slot(key: str) -> Optional[Dict[str, Any]]:
    return _SLOT_BY_KEY.get(str(key or "").strip().lower())


def slot_label(key: str, lang: str = "zh") -> str:
    s = get_slot(key)
    if not s:
        return str(key or "")
    k = "label_en" if str(lang).lower().startswith("en") else "label_zh"
    return str(s.get(k) or s.get("label_zh") or key)


def _filled(fields: Dict[str, Any], key: str) -> bool:
    v = (fields or {}).get(key)
    if isinstance(v, dict):
        return bool(str(v.get("v") or "").strip())
    return bool(str(v or "").strip())


def fill_rates(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """双轨加权填充率。返回 ``{relation, bant: 0..1, filled, total}``。"""
    f = fields or {}
    out: Dict[str, Any] = {"filled": 0, "total": len(SLOTS)}
    for track in TRACKS:
        got = tot = 0
        for s in SLOTS:
            if s["track"] != track:
                continue
            w = int(s.get("weight") or 1)
            tot += w
            if _filled(f, s["key"]):
                got += w
        out[track] = round(got / tot, 3) if tot else 0.0
    out["filled"] = sum(1 for s in SLOTS if _filled(f, s["key"]))
    return out


def missing_slots(
    fields: Optional[Dict[str, Any]], *, track: str = "bant", limit: int = 2,
    include: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """按登记顺序取还没填的槽位定义（画像缺口 → 采集提示/UI chips）。

    ``track=""``（全轨枚举，LLM 抽取用）只枚举 TRACKS 内槽位——lifecycle
    采集专用槽（churn_reason）不进缺口，防被当「该问的问题」推给所有客户。
    ``include``（P26）：显式槽位键列表（摸底目标按坐席勾选出缺口，顺序即
    优先级）；给了 include 时 track 忽略。
    """
    out: List[Dict[str, Any]] = []
    if include:
        for k in include:
            s = _SLOT_BY_KEY.get(str(k or "").strip().lower())
            if s is None or s["track"] not in TRACKS:
                continue
            if not _filled(fields or {}, s["key"]):
                out.append(dict(s))
                if len(out) >= max(1, int(limit)):
                    break
        return out
    for s in SLOTS:
        if track:
            if s["track"] != track:
                continue
        elif s["track"] not in TRACKS:
            continue
        if not _filled(fields or {}, s["key"]):
            out.append(dict(s))
            if len(out) >= max(1, int(limit)):
                break
    return out


def gap_hint(fields: Optional[Dict[str, Any]], *, lang: str = "zh",
             limit: int = 2, include: Optional[List[str]] = None) -> str:
    """摸底段注入用的缺口短语（如「业务痛点、预算档」）；全齐 → ""。

    ``include``：摸底目标的勾选槽位（缺口只在其中取，配 ``limit=1`` 实现
    「每轮只带一个最高优先缺口」——列表越长 LLM 越想一口气问完）。
    """
    miss = missing_slots(
        fields, track=("" if include else "bant"), limit=limit,
        include=include)
    if not miss:
        return ""
    k = "ask_en" if str(lang).lower().startswith("en") else "ask_zh"
    return "、".join(str(m.get(k) or m.get("label_zh") or m["key"]) for m in miss)


def parse_selected_slots(raw: Any) -> List[str]:
    """目标 ``params.slots``（逗号/顿号/空白分隔字符串或列表）→ 合法槽位键
    （保序去重；未知键/lifecycle 专用槽忽略）。空 → []。"""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,，、;\s]+", str(raw or ""))
    out: List[str] = []
    for it in items:
        k = it.strip().lower()
        s = _SLOT_BY_KEY.get(k)
        if s is not None and s["track"] in TRACKS and k not in out:
            out.append(k)
    return out


def selected_fill_rate(
    fields: Optional[Dict[str, Any]], keys: Optional[List[str]],
) -> float:
    """按坐席勾选槽位算的等权填充率（0..1）；勾选为空 → -1.0（未知，调用方
    按「信号缺失」处理而非当 0 分）。摸底目标（profile_discovery）的
    结算信号源——填一格进一格，全填即达成。"""
    ks = [k for k in (keys or []) if k in _SLOT_BY_KEY]
    if not ks:
        return -1.0
    got = sum(1 for k in ks if _filled(fields or {}, k))
    return round(got / len(ks), 3)


def slot_value(fields: Optional[Dict[str, Any]], key: str) -> str:
    """读槽位现值（兼容 ``{"v":...}`` 单元与裸值）；空 → ""。"""
    cell = (fields or {}).get(str(key or ""))
    if isinstance(cell, dict):
        return str(cell.get("v") or "").strip()
    return str(cell or "").strip()


def facts_line(
    fields: Optional[Dict[str, Any]], *, limit: int = 5, max_chars: int = 88,
) -> str:
    """已采画像 → 一行紧凑事实串（「称呼:阿龙｜业务痛点:客服人手｜预算档:500刀」）。

    P9 前已采值只驱动选品、LLM 从没见过原值——「TA是做民宿的」这类事实
    进 prompt 才能让获客/留存对话真正个性化。注册表顺序取前 ``limit`` 个
    已填槽（含 lifecycle 的流失原因——它存在=老客户，语境重要）；单值
    截 14 字、整行字符预算截断。全空 → ""。"""
    f = fields or {}
    parts: List[str] = []
    used = 0
    for s in SLOTS:
        key = s["key"]
        cell = f.get(key)
        v = str((cell or {}).get("v") or "").strip() if isinstance(
            cell, dict) else str(cell or "").strip()
        if not v:
            continue
        piece = f"{s.get('label_zh') or key}:{v[:14]}"
        sep = 1 if parts else 0
        if used + sep + len(piece) > max(20, int(max_chars)):
            break
        parts.append(piece)
        used += sep + len(piece)
        if len(parts) >= max(1, int(limit)):
            break
    return "｜".join(parts)


# 流失原因 → 应对策略（P9b，纯话术方向不涉价格权限；键=_CHURN_PATTERNS 标签）
_CHURN_STRATEGY: Dict[str, str] = {
    "太贵": "聊性价比与入门档位，先算省下的人力账，别硬推",
    "没用起来": "先帮TA把工具真用起来（带教/给用法），别急着推",
    "效果不佳": "先问哪里没达预期，给具体解法，重建信任再推进",
    "出了问题": "先认问题、讲清已修复改进了什么，再邀请再试",
    "换了别家": "别贬低对方在用的，问哪里更顺手，聊差异化价值",
    "业务变动": "生意变了先重新摸底现状，按新需求聊，别翻旧账",
    "预算紧张": "共情手头紧，聊轻量档位或先缓一缓，绝不催单",
}


def churn_strategy_hint(reason: str) -> str:
    """在档流失原因（可能多标签「太贵、没用起来」）→ 主因（首标签）的
    应对策略一句。未知标签/空 → ""。"""
    primary = str(reason or "").split("、", 1)[0].strip()
    return _CHURN_STRATEGY.get(primary, "")


# 流失原因 → 选品/CTA 转向（P11，**不发明折扣**——只在现有公开货架内
# 换档位/换收口方式；定价权限仍归运营/官网）
# plan_pref: entry=入门档深链（非 hot 团队/旗舰）；hot/default=原行为
# cta_bias:  roi=soft 期提前给试算器；cs=direct 改客服收口（信任未重建）；
#            ""=不改 CTA 分级
_CHURN_OFFER: Dict[str, Dict[str, str]] = {
    "太贵": {"plan_pref": "entry", "cta_bias": "roi"},
    "预算紧张": {"plan_pref": "entry", "cta_bias": "roi"},
    "没用起来": {"plan_pref": "default", "cta_bias": "cs"},
    "效果不佳": {"plan_pref": "default", "cta_bias": "cs"},
    "出了问题": {"plan_pref": "default", "cta_bias": "cs"},
    "换了别家": {"plan_pref": "entry", "cta_bias": "roi"},
    "业务变动": {"plan_pref": "entry", "cta_bias": "roi"},
}


def churn_offer_steer(reason: str) -> Dict[str, str]:
    """在档流失原因 → ``{plan_pref, cta_bias}``。多标签取主因；未知/空 →
    两字段都是空串（调用方按旧行为）。纯函数，不涉价格改写。"""
    primary = str(reason or "").split("、", 1)[0].strip()
    hit = _CHURN_OFFER.get(primary) or {}
    return {
        "plan_pref": str(hit.get("plan_pref") or ""),
        "cta_bias": str(hit.get("cta_bias") or ""),
    }


# ── 确定性采集（宁可漏采不错采）──────────────────────────────────────────────

# 平台/渠道词表（需伴随使用动词才算「在用」，光提到不算）
_CHANNEL_WORDS = (
    "whatsapp", "telegram", "line", "messenger", "facebook", "instagram",
    "tiktok", "shopee", "lazada", "viber", "wechat",
    "微信", "抖音", "快手", "小红书", "飞机", "电报",
)
_CHANNEL_RE = re.compile(
    r"(?:在用|在做|主要用|都用|一直用|用的?是|跑的?是|开着|在|做|用)\s*"
    r"(" + "|".join(_CHANNEL_WORDS) + r")",
    re.IGNORECASE,
)

# 痛点分类词表（need 槽位存分类标签，供选品匹配；一次可命中多类）
_NEED_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("客服人手", re.compile(
        r"客服.{0,6}(?:不够|不足|太累|忙不过|请不起|成本|贵)|人手不够|招人难|请人贵|人力成本")),
    ("回复不过来", re.compile(
        r"(?:消息|咨询|询盘|客服|客户).{0,8}(?:回不过来|回不完|太多|爆了)|回复不及时|漏回|半夜.{0,6}(?:消息|咨询|客户)")),
    ("语言不通", re.compile(
        r"语言不通|翻译.{0,4}(?:麻烦|头疼|烦|累)|(?:英文|英语|外语|菲语|他加禄).{0,6}(?:不好|不行|看不懂|听不懂)")),
    ("获客难", re.compile(r"获客|拉新|引流|没(?:什么)?(?:客户|流量)|客源少|询盘少")),
    ("直播带货", re.compile(r"直播|带货|主播|短视频矩阵")),
    ("成交转化", re.compile(r"转化(?:率)?(?:低|差|上不去)|(?:成交|下单)(?:率)?(?:低|少|难)|跟进不及时")),
)

# 预算：必须出现「预算/花/成本」语境 + 金额（防把年龄/日期当预算）
_BUDGET_RE = re.compile(
    r"(?:预算|budget|愿意(?:花|出)|能接受|每个?月(?:大概|最多)?(?:花|出|投))"
    r"[^\d]{0,8}(\d[\d,.]{0,8})\s*"
    r"(万|千|k|K|USD|usd|美金|美元|刀|块钱?|元|peso|比索|p)?",
)

# 团队规模：数字 + 人/客服/坐席/员工，且句内有团队语境词
_TEAM_CTX_RE = re.compile(r"团队|客服|坐席|员工|人手|雇|请了|我们|店里|公司")
_TEAM_RE = re.compile(r"(\d{1,4})\s*(?:个|名|位)?\s*(?:人|客服|坐席|员工)")

# 上线时间：明确的时间意愿表达
_TIMELINE_RE = re.compile(
    r"(这个?月|下个?月|月底|年底|下周|这周|两三?(?:周|个月)内|尽快|越快越好|马上|现在就|"
    r"asap|this month|next month|next week)",
    re.IGNORECASE,
)

# 决策角色：明确的拍板/上报表达
_AUTHORITY_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("拍板人", re.compile(r"我(?:说了算|自己(?:的店|做主|决定)|是老板|就是老板|一个人(?:的店|在做))")),
    ("需上报", re.compile(r"(?:要|得|需要)(?:问|跟|和).{0,4}(?:老板|合伙人|上面|领导)(?:商量|汇报|确认)?|不是我(?:能)?(?:决定|拍板)")),
)

# 称呼：「叫我X / 我叫X」（2-8 字，截断于标点；尾部客套词「就行/就好/吧」剥离）
_NAME_RE = re.compile(r"(?:叫我|我叫|我是)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z ·]{0,7})(?=[，。,.!！?？\s]|$)")
_NAME_STOP = re.compile(r"觉得|认为|想|说|做|干|在|一个|这样|那样")  # 「我是觉得…」防误采
_NAME_TRAIL_RE = re.compile(r"(?:就行|就好|就可以|好了|吧|啦|哈)+$")

# 坐标：已知地名白名单（在菲华人业务面 + 常见跨境城市；开放式「我在X」不采）
_PLACES = (
    "马尼拉", "宿务", "达沃", "长滩", "克拉克", "碧瑶", "马卡蒂", "帕赛", "奎松",
    "菲律宾", "曼谷", "胡志明", "河内", "金边", "西港", "吉隆坡", "新加坡", "雅加达", "迪拜",
    "manila", "cebu", "davao", "makati", "bgc", "clark", "quezon",
)
_LOCATION_RE = re.compile(
    r"(?:我?(?:人)?在|我住在?|坐标|base在?)\s*(" + "|".join(_PLACES) + r")",
    re.IGNORECASE,
)

# 职业/生意：「我(是)做X的 / 我开了家X」
_OCCUPATION_RE = re.compile(
    r"(?:我(?:是|们)?做|我开(?:了|着)?(?:一?[家个间])?)"
    r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 ]{1,11}?)"
    r"(?:的|生意|平台|店|铺|公司|工作室)?(?=[，。,.!！?？\s]|$)")
_OCCUPATION_STOP = re.compile(r"^(?:什么|啥|梦|不了|不到|完)$")

# 年龄（P26）：只认明确自述——「我28岁 / 我今年28了 / I'm 28 years old」。
# 「我今年28」不带 岁/了 刻意不采（可能是 28 号出发/28 楼）；「我住28楼」
# 「28号见」都不含自述锚不会命中。数值 14..90 夹界（超界=玩笑/误写不采）。
_AGE_RE = re.compile(
    r"我(?:今年|都|现在)?\s*(\d{1,2})\s*岁|"
    r"我今年\s*(\d{1,2})\s*了|"
    r"I(?:'m|\s+am)\s+(\d{1,2})\s+years?\s*old",
    re.IGNORECASE)
# 年龄段：「我(是)90后/00后」——存段标签，诚实不虚构具体岁数
_AGE_BAND_RE = re.compile(r"我(?:是)?\s*((?:[5-9]0|00)后)")


def capture_from_text(text: str) -> List[Tuple[str, str]]:
    """从一条入站消息确定性抽画像槽位。返回 ``[(slot_key, value), ...]``。

    只认高置信表达；每槽位最多一条；value 已消毒截断（≤40 字）。
    """
    t = str(text or "").strip()
    if not t or len(t) > 2000:
        return []
    out: List[Tuple[str, str]] = []
    seen: set = set()

    def _add(key: str, value: str) -> None:
        v = re.sub(r"\s+", " ", str(value or "").strip())[:40]
        if v and key not in seen:
            seen.add(key)
            out.append((key, v))

    # need：多类命中合并（分类标签，不存原文）
    needs = [label for label, pat in _NEED_PATTERNS if pat.search(t)]
    if needs:
        _add("need", "、".join(needs[:3]))

    m = _CHANNEL_RE.search(t)
    if m:
        _add("channel", m.group(1).lower() if m.group(1).isascii() else m.group(1))

    m = _BUDGET_RE.search(t)
    if m:
        unit = (m.group(2) or "").strip()
        _add("budget", f"{m.group(1)}{unit}" if unit else m.group(1))

    if _TEAM_CTX_RE.search(t):
        m = _TEAM_RE.search(t)
        if m:
            try:
                size = int(m.group(1))
                if 1 <= size <= 5000:
                    _add("team_size", f"{size}人")
            except ValueError:
                pass

    m = _TIMELINE_RE.search(t)
    if m:
        _add("timeline", m.group(1))

    for label, pat in _AUTHORITY_PATTERNS:
        if pat.search(t):
            _add("authority", label)
            break

    m = _NAME_RE.search(t)
    if m and not _NAME_STOP.match(m.group(1)):
        nm = _NAME_TRAIL_RE.sub("", m.group(1).strip()).strip()
        if nm:
            _add("name", nm)

    m = _LOCATION_RE.search(t)
    if m:
        _add("location", m.group(1))

    m = _OCCUPATION_RE.search(t)
    if m and not _OCCUPATION_STOP.match(m.group(1).strip()):
        _add("occupation", m.group(1).strip())

    m = _AGE_RE.search(t)
    if m:
        raw_n = next((g for g in m.groups() if g), "")
        try:
            n = int(raw_n)
            if 14 <= n <= 90:
                _add("age", f"{n}岁")
        except ValueError:
            pass
    if "age" not in seen:
        m = _AGE_BAND_RE.search(t)
        if m:
            _add("age", m.group(1))

    return out


# ── 流失原因采集（P8，lifecycle 轨；只由 service 在留存/挽回/回流会话调用）──

# 分类词表（与 need 同哲学：存分类标签不存原文——低毒、可聚合看板化）
_CHURN_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("太贵", re.compile(
        r"太贵|价格.{0,4}(?:高|贵)|贵了|用不起|不划算|性价比(?:低|不高|太低)")),
    ("没用起来", re.compile(
        r"没(?:怎么|太)?用(?:起来|上|过几次)|用得(?:少|不多)|闲置|"
        r"没时间(?:用|弄|搞|管|研究)|忘(?:了|记)用")),
    ("效果不佳", re.compile(
        r"没(?:什么|啥)?效果|效果(?:不好|一般|不明显|差)|没见效|不好用|"
        r"难用|不会用|太复杂|学不会")),
    ("出了问题", re.compile(
        r"老(?:是)?(?:掉线|封号|出问题|报错|卡)|不稳定|bug(?:太|很)?多|"
        r"被?封(?:号|了)", re.IGNORECASE)),
    ("换了别家", re.compile(
        r"换(?:了|成)?(?:别|其他|另)(?:的|一)?(?:家|个)|用(?:了)?别家|竞品|"
        r"换(?:去|到)了|别的(?:工具|软件|平台)")),
    ("业务变动", re.compile(
        r"不做(?:了|那块|这块)|转行|生意(?:不做|收了|停了|黄了)|"
        r"团队(?:散|解散)了?|裁员|关(?:店|了店)|回国(?:了|发展)")),
    ("预算紧张", re.compile(
        r"预算(?:砍|没|削|紧)|降本|资金(?:紧张|周转)|没钱|亏(?:损|了不少)")),
)

# 续费语境锚词：retention 会话是日常闲聊，句内须带锚才认（「这家餐厅太贵」
# 不采）；winback/reconvert 会话本身就在聊流失，调用方免锚。
# 只认复合词——裸「续」会被「连续加班/继续」误触
_CHURN_ANCHOR_RE = re.compile(
    r"续费|续订|续约|不续|没续|再续|续上|到期|过期|取消|退订|"
    r"停(?:用|了)|不用(?:了|它)|没(?:再)?(?:开|充|买|订)|"
    r"renew|subscri|cancel", re.IGNORECASE)


def capture_churn_reason(text: str, *, require_anchor: bool = False) -> str:
    """从一条入站消息确定性抽流失原因分类标签（≤3 类、顿号连接）。

    ``require_anchor=True``（留存会话档）：句内还须命中续费语境锚词。
    无命中 → ""。宁可漏采不错采；auto 入库只填空，坐席可改可清。
    """
    t = str(text or "").strip()
    if not t or len(t) > 2000:
        return ""
    if require_anchor and not _CHURN_ANCHOR_RE.search(t):
        return ""
    hits = [label for label, pat in _CHURN_PATTERNS if pat.search(t)]
    return "、".join(hits[:3])


__all__ = [
    "SLOTS",
    "TRACKS",
    "capture_churn_reason",
    "capture_from_text",
    "churn_offer_steer",
    "churn_strategy_hint",
    "facts_line",
    "fill_rates",
    "gap_hint",
    "get_slot",
    "missing_slots",
    "parse_selected_slots",
    "selected_fill_rate",
    "slot_keys",
    "slot_label",
    "slot_value",
]
