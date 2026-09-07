"""客户画像槽位注册表 + 确定性采集（纯数据 + 纯函数，零 IO）。

双轨画像（acquire_and_convert 漏斗的「摸底」事实源）：
- **relation 轨**（关系向）：称呼/坐标/职业/兴趣——聊得像朋友的基础。
- **bant 轨**（商机向，B2B 资格四要素扩展）：痛点 Need / 在用渠道 Channel /
  团队规模 Size / 预算 Budget / 决策角色 Authority / 上线时间 Timeline。
- **personal 轨**（N-3 #241 陪伴域，D-N1）：家庭状况 / 婚恋状态 / 收入水平 /
  当前居住地 / 个人资产——陪伴运营要摸的是「TA 是个什么处境的人」，不是商机资格。
  收入 / 资产标 ``sensitive``：建目标默认不勾，勾了也只许 AI 多轮自然带出、禁止
  直接问 / 连问（``resolve_inject_gap`` 给缺口短语追加硬约束）。
- **lifecycle 轨**（P8，采集专用）：流失原因 churn_reason——只在留存/挽回/
  回流会话里确定性采集（service 按 goal.created_by 门控），**不进** TRACKS
  枚举 → 不计填充率、不进 missing_slots("")/LLM 抽取/摸底缺口提示（新客户
  永远不会被问「为什么没续」）；填上后画像卡自动显示、坐席可改。
- **custom 轨**（N-3 #241）：运营自己加的标签（「+ 自定义标签」），键
  ``x_<sha1(label)[:10]>``，配置 ``companion.goals.custom_slots`` 持久化，进程内
  :func:`register_custom_slots` 登记；两个业务域都可用，同步出现在摸底 chips 与画像卡。

**按业务域给槽位表**（N-3 #241）：``SLOTS`` 仍是销售域登记表（relation + bant +
lifecycle，旧契约不动）；陪伴域 = relation + personal（+ custom）。消费方用
:func:`slots_for_domain` / :func:`tracks_for` / :func:`secondary_track`，缺省读
``business_domain.active_business_domain()``。**存量值不丢**：不在当前域表里但已有值
的槽（如陪伴机器升级前填过的「预算档」）由 ``goal_routes._profile_view`` 以「其他已
记录」附带显示；``get_slot`` / ``parse_selected_slots`` 对全表都认，旧目标照跑。

设计原则：
- 槽位登记制：新增槽位只加一条 dict（label/ask_hint/weight），fill_rates /
  missing_slots / UI 自动生效。
- ``capture_from_text`` 确定性正则，**宁可漏采不错采**（错的画像比空画像更毒——
  会被 ledger 当推进信号、被选品当依据）；模糊场景交给坐席画像卡人工补录。
- 存储形状（customer_profiles.fields JSON）：``{slot: {"v": str, "src":
  "auto|agent", "ts": float}}``——auto 只填空、绝不覆盖坐席手录（agent 覆盖一切）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

# C2（I-3）：摸底注入硬约束——LLM 经常把软提示当可忽略。文案钉死这句，
# 一轮只丢一个未填槽；连问三个字段比不问更糟（用户会当问卷机器人）。
HARD_ASK_DISCIPLINE = "像朋友闲聊，不要像查户口，一轮只问一个"
# N-3 #241（D-N1）：敏感槽（收入 / 资产）的追加硬约束——只能顺着对方主动提起的话头
# 自然带出，绝不直接问、绝不连问、本轮带不出就算（接 I-3 #38「一轮只问一个空槽」）。
SENSITIVE_ASK_DISCIPLINE = (
    "这是敏感话题：只能顺着对方自己提起的话头多轮自然带出，绝不直接问、绝不追问，"
    "本轮带不出就算")
_ASKED_PARAM = "_gap_asked"
_ASKED_FP_PARAM = "_gap_asked_fp"
_ASKED_LAST_PARAM = "_gap_asked_last"

TRACKS = ("relation", "bant")
# 业务域 → 计填充率 / 进缺口枚举的轨（lifecycle 永不进；custom 随两域）
TRACKS_BY_DOMAIN: Dict[str, Tuple[str, ...]] = {
    "sales": ("relation", "bant"),
    "companion": ("relation", "personal"),
}
# 业务域 → 「第二轨」（画像卡缺口 chips / gap_hint 缺省轨）
SECONDARY_TRACK: Dict[str, str] = {"sales": "bant", "companion": "personal"}
CUSTOM_TRACK = "custom"
# 可被勾选 / 枚举为缺口的轨（lifecycle 采集专用，永不进）
_ENUMERABLE_TRACKS = ("relation", "bant", "personal", CUSTOM_TRACK)

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

# ── personal 轨（N-3 #241 陪伴域摸底集；老板决策 D-N1）──────────────────────
# 与 relation 五槽合成陪伴域的摸底标签：称呼 / 坐标 / 职业 / 年龄 / 兴趣 + 家庭状况 /
# 婚恋状态 / 收入水平 / 当前居住地 / 个人资产。收入 / 资产 sensitive=True：建目标默认
# 不勾（模板 default 不含），勾了 resolve_inject_gap 追加 SENSITIVE_ASK_DISCIPLINE。
PERSONAL_SLOTS: Tuple[Dict[str, Any], ...] = (
    {"key": "family_status", "track": "personal", "weight": 2,
     "label_zh": "家庭状况", "label_en": "Family",
     "ask_zh": "家里都有谁、平时跟家人怎么相处", "ask_en": "who is in their family"},
    {"key": "marital_status", "track": "personal", "weight": 2,
     "label_zh": "婚恋状态", "label_en": "Relationship status",
     "ask_zh": "现在是单身还是有伴", "ask_en": "whether they are single or attached"},
    {"key": "income_level", "track": "personal", "weight": 1, "sensitive": True,
     "label_zh": "收入水平", "label_en": "Income level",
     "ask_zh": "收入大概什么水平", "ask_en": "roughly their income level"},
    {"key": "residence", "track": "personal", "weight": 1,
     "label_zh": "当前居住地", "label_en": "Current residence",
     "ask_zh": "现在住在哪、住得怎么样", "ask_en": "where they live now"},
    {"key": "assets", "track": "personal", "weight": 1, "sensitive": True,
     "label_zh": "个人资产", "label_en": "Assets",
     "ask_zh": "有没有房车之类的资产", "ask_en": "whether they own property or a car"},
)

# 全表（两域 + lifecycle）：get_slot / 入库校验 / facts_line / 存量值回显都认它
ALL_SLOTS: Tuple[Dict[str, Any], ...] = SLOTS + PERSONAL_SLOTS

_SLOT_BY_KEY = {s["key"]: s for s in ALL_SLOTS}

# ── 自定义标签（N-3 #241 「+ 自定义标签」）──────────────────────────────────
_CUSTOM_KEY_PREFIX = "x_"
_CUSTOM_MAX = 12
_CUSTOM: Dict[str, Dict[str, Any]] = {}


def custom_slot_key(label: str) -> str:
    """标签文案 → 稳定键 ``x_<sha1[:10]>``（同名同键；大小写 / 首尾空白不敏感）。"""
    lab = str(label or "").strip().casefold()
    if not lab:
        return ""
    return _CUSTOM_KEY_PREFIX + hashlib.sha1(lab.encode("utf-8")).hexdigest()[:10]


def is_custom_slot_key(key: str) -> bool:
    return str(key or "").startswith(_CUSTOM_KEY_PREFIX)


def normalize_custom_slot(item: Any) -> Optional[Dict[str, Any]]:
    """配置项（字符串标签 / dict）→ 槽位 dict；空标签 → None。"""
    if isinstance(item, dict):
        label = str(item.get("label_zh") or item.get("label") or "").strip()
        label_en = str(item.get("label_en") or label).strip()
        ask_zh = str(item.get("ask_zh") or item.get("ask") or "").strip()
        ask_en = str(item.get("ask_en") or ask_zh or label_en).strip()
        sensitive = bool(item.get("sensitive", False))
    else:
        label = str(item or "").strip()
        label_en, ask_zh, ask_en, sensitive = label, "", "", False
    label = label[:24]
    if not label:
        return None
    return {
        "key": custom_slot_key(label), "track": CUSTOM_TRACK, "weight": 1,
        "label_zh": label, "label_en": label_en[:32] or label,
        "ask_zh": ask_zh[:60] or f"了解一下TA的「{label}」",
        "ask_en": ask_en[:80] or f"learn their {label_en or label}",
        "sensitive": sensitive, "custom": True,
    }


def register_custom_slots(items: Any, *, replace: bool = True) -> List[Dict[str, Any]]:
    """登记自定义槽位（进程级）。``replace=True`` 整表替换（配置是唯一事实源）。
    上限 ``_CUSTOM_MAX``，超出忽略；键与内置槽撞名不可能（前缀 x_）。返回当前表。"""
    new: Dict[str, Dict[str, Any]] = {} if replace else dict(_CUSTOM)
    for it in (items or []) if isinstance(items, (list, tuple)) else []:
        s = normalize_custom_slot(it)
        if s is None or s["key"] in new:
            continue
        if len(new) >= _CUSTOM_MAX:
            break
        new[s["key"]] = s
    _CUSTOM.clear()
    _CUSTOM.update(new)
    return list(_CUSTOM.values())


def custom_slots() -> Tuple[Dict[str, Any], ...]:
    return tuple(_CUSTOM.values())


def custom_slot_labels() -> List[str]:
    """持久化形状（写回配置 ``companion.goals.custom_slots``）。"""
    return [str(s.get("label_zh") or "") for s in _CUSTOM.values()]


def clear_custom_slots() -> None:
    _CUSTOM.clear()


def load_custom_slots_from_config(cfg_root: Any) -> List[Dict[str, Any]]:
    """``companion.goals.custom_slots``（标签字符串 / dict 列表）→ 登记。坏配置 → 空表。"""
    try:
        comp = (cfg_root or {}).get("companion") if isinstance(cfg_root, dict) else None
        goals = (comp or {}).get("goals") if isinstance(comp, dict) else None
        raw = (goals or {}).get("custom_slots") if isinstance(goals, dict) else None
    except Exception:
        raw = None
    return register_custom_slots(raw if isinstance(raw, list) else [])


# ── 按业务域取槽位表 ─────────────────────────────────────────────────────────

def _bd(business_domain: Optional[str] = None) -> str:
    """规范化业务域；缺省读进程级 active（装配层登记；测试 / 无配置回落 env 推导）。"""
    v = str(business_domain or "").strip().lower()
    if v in TRACKS_BY_DOMAIN:
        return v
    try:
        from src.utils.business_domain import active_business_domain
        v = active_business_domain()
    except Exception:
        v = "sales"
    return v if v in TRACKS_BY_DOMAIN else "sales"


def tracks_for(business_domain: Optional[str] = None) -> Tuple[str, ...]:
    """该域计填充率 / 进缺口枚举的轨（不含 lifecycle / custom）。"""
    return TRACKS_BY_DOMAIN[_bd(business_domain)]


def secondary_track(business_domain: Optional[str] = None) -> str:
    """该域的第二轨：销售 bant / 陪伴 personal。"""
    return SECONDARY_TRACK[_bd(business_domain)]


def slots_for_domain(business_domain: Optional[str] = None, *,
                     include_lifecycle: bool = True,
                     include_custom: bool = True) -> List[Dict[str, Any]]:
    """该域的摸底 / 画像槽位表（顺序即展示序）。销售 = relation + bant（+ lifecycle）；
    陪伴 = relation + personal。custom 两域都附在末尾。"""
    tracks = set(tracks_for(business_domain))
    out = [dict(s) for s in ALL_SLOTS if s["track"] in tracks]
    if include_lifecycle and _bd(business_domain) == "sales":
        out.extend(dict(s) for s in ALL_SLOTS if s["track"] == "lifecycle")
    if include_custom:
        out.extend(dict(s) for s in _CUSTOM.values())
    return out


def slot_is_sensitive(key: str) -> bool:
    s = get_slot(key)
    return bool(s and s.get("sensitive"))


def slot_keys(track: str = "") -> List[str]:
    t = str(track or "").strip().lower()
    return [s["key"] for s in ALL_SLOTS if not t or s["track"] == t]


def get_slot(key: str) -> Optional[Dict[str, Any]]:
    k = str(key or "").strip().lower()
    return _SLOT_BY_KEY.get(k) or _CUSTOM.get(k)


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


def fill_rates(fields: Optional[Dict[str, Any]], *,
               business_domain: Optional[str] = None) -> Dict[str, Any]:
    """按域双轨加权填充率。返回 ``{relation, bant, personal: 0..1, filled, total,
    tracks: [该域的两轨]}``——三条轨的键**恒在**（不在该域的轨为 0.0），消费方
    ``rates.get("bant")`` 永不 KeyError；``filled / total`` 只数该域槽位表。"""
    f = fields or {}
    bd = _bd(business_domain)
    domain_slots = slots_for_domain(bd, include_lifecycle=False, include_custom=False)
    out: Dict[str, Any] = {"filled": 0, "total": len(domain_slots),
                           "tracks": list(TRACKS_BY_DOMAIN[bd])}
    for track in ("relation", "bant", "personal"):
        got = tot = 0
        for s in ALL_SLOTS:
            if s["track"] != track:
                continue
            w = int(s.get("weight") or 1)
            tot += w
            if _filled(f, s["key"]):
                got += w
        out[track] = round(got / tot, 3) if tot else 0.0
    out["filled"] = sum(1 for s in domain_slots if _filled(f, s["key"]))
    return out


def missing_slots(
    fields: Optional[Dict[str, Any]], *, track: str = "bant", limit: int = 2,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    business_domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按登记顺序取还没填的槽位定义（画像缺口 → 采集提示/UI chips）。

    ``track=""``（全轨枚举，LLM 抽取用）只枚举**该业务域**两轨 + custom 槽位——
    lifecycle 采集专用槽（churn_reason）不进缺口，防被当「该问的问题」推给所有客户。
    ``track`` 显式给轨名（bant / personal / lifecycle / custom）→ 只看该轨，不分域。
    ``include``（P26）：显式槽位键列表（摸底目标按坐席勾选出缺口，顺序即
    优先级）；给了 include 时 track 忽略；可勾选轨之外的键（lifecycle）忽略。
    ``exclude``（C2）：已问过但仍空的槽——跳过以免连轮追问同一句；
    全被排除但仍有缺口时回落第一空槽（第二圈，防问完就哑火）。
    """
    skip = {str(x).strip().lower() for x in (exclude or []) if str(x).strip()}
    out: List[Dict[str, Any]] = []
    fallback: List[Dict[str, Any]] = []
    cap = max(1, int(limit))

    def _take(s: Dict[str, Any]) -> bool:
        if _filled(fields or {}, s["key"]):
            return False
        item = dict(s)
        fallback.append(item)
        if s["key"] in skip:
            return False
        out.append(item)
        return len(out) >= cap

    if include:
        for k in include:
            s = get_slot(k)
            if s is None or s["track"] not in _ENUMERABLE_TRACKS:
                continue
            if _take(s):
                break
    else:
        if track:
            pool = [s for s in ALL_SLOTS if s["track"] == track]
            if track == CUSTOM_TRACK:
                pool = list(_CUSTOM.values())
        else:
            pool = slots_for_domain(business_domain, include_lifecycle=False)
        for s in pool:
            if _take(s):
                break
    if out:
        return out
    return fallback[:cap]


def gap_hint(fields: Optional[Dict[str, Any]], *, lang: str = "zh",
             limit: int = 2, include: Optional[List[str]] = None,
             exclude: Optional[List[str]] = None,
             business_domain: Optional[str] = None) -> str:
    """摸底段注入用的缺口短语（如「业务痛点、预算档」/ 陪伴域「家庭状况、婚恋状态」）；
    全齐 → ""。缺省轨 = 该域第二轨（销售 bant / 陪伴 personal）。

    ``include``：摸底目标的勾选槽位（缺口只在其中取，配 ``limit=1`` 实现
    「每轮只带一个最高优先缺口」——列表越长 LLM 越想一口气问完）。
    敏感槽的短语带 :data:`SENSITIVE_ASK_DISCIPLINE`（prompt 硬约束）。
    """
    miss = missing_slots(
        fields, track=("" if include else secondary_track(business_domain)),
        limit=limit, include=include, exclude=exclude,
        business_domain=business_domain)
    if not miss:
        return ""
    k = "ask_en" if str(lang).lower().startswith("en") else "ask_zh"
    return "、".join(_ask_with_sensitivity(m, k) for m in miss)


def _ask_with_sensitivity(slot: Dict[str, Any], ask_key: str) -> str:
    phrase = str(slot.get(ask_key) or slot.get("label_zh") or slot["key"])
    if slot.get("sensitive"):
        return f"{phrase}（{SENSITIVE_ASK_DISCIPLINE}）"
    return phrase


def slot_ask(key: str, lang: str = "zh") -> str:
    """建议问法（UI 用，**不带**敏感约束——那是给 prompt 的，见 :func:`inject_ask`）。"""
    s = get_slot(key)
    if not s:
        return ""
    k = "ask_en" if str(lang).lower().startswith("en") else "ask_zh"
    return str(s.get(k) or s.get("label_zh") or key)


def inject_ask(key: str, lang: str = "zh") -> str:
    """注入链用的缺口短语：敏感槽追加 :data:`SENSITIVE_ASK_DISCIPLINE`。"""
    s = get_slot(key)
    if not s:
        return ""
    k = "ask_en" if str(lang).lower().startswith("en") else "ask_zh"
    return _ask_with_sensitivity(s, k)


def inbound_gap_fp(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return ""
    return hashlib.sha1(t.encode("utf-8", errors="ignore")).hexdigest()[:12]


def asked_slot_keys(params: Optional[Dict[str, Any]]) -> List[str]:
    raw = (params or {}).get(_ASKED_PARAM) or []
    if isinstance(raw, str):
        raw = re.split(r"[,，、;\s]+", raw)
    out: List[str] = []
    for it in raw:
        k = str(it or "").strip().lower()
        if k and k not in out and k in _SLOT_BY_KEY:
            out.append(k)
    return out


def next_unfilled_slot(
    fields: Optional[Dict[str, Any]], *,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    track: str = "",
) -> Optional[Dict[str, Any]]:
    """登记序（或勾选序）第一个未填且不在 exclude 里的槽；全被问过仍空
    → 回落第一空槽。全齐 → None。"""
    miss = missing_slots(
        fields, track=track, limit=32, include=include, exclude=exclude)
    return dict(miss[0]) if miss else None


def resolve_inject_gap(
    fields: Optional[Dict[str, Any]],
    params: Optional[Dict[str, Any]],
    *,
    inbound_text: str = "",
    include: Optional[List[str]] = None,
    lang: str = "zh",
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """注入链单缺口决议（C2 硬约束）。

    返回 ``(ask_phrase, slot_key, params_patch)``。全齐 → ``("", "", None)``。
    同一条入站指纹复用上轮槽，不轮转（A/B 双链同条不连耗两个槽）。
    入站为空 → 只取第一空槽、不写 params（预览/旧测试保持稳定）。
    新入站 → 跳过已问仍空的槽，并把本轮槽记进 ``params._gap_asked``。
    """
    p = dict(params or {})
    asked = asked_slot_keys(p)
    fp = inbound_gap_fp(inbound_text)
    last = str(p.get(_ASKED_LAST_PARAM) or "").strip().lower()
    if fp and str(p.get(_ASKED_FP_PARAM) or "") == fp and last:
        if not _filled(fields or {}, last):
            return inject_ask(last, lang), last, None
    slot = next_unfilled_slot(
        fields, include=include, exclude=asked, track="")
    if not slot:
        return "", "", None
    key = str(slot.get("key") or "")
    # 敏感槽（收入 / 资产）：短语自带「只能多轮自然带出、禁直接问禁连问」硬约束
    phrase = inject_ask(key, lang)
    if not fp:
        return phrase, key, None
    new_asked = list(asked)
    if key and key not in new_asked:
        new_asked.append(key)
    patch = dict(p)
    patch[_ASKED_PARAM] = new_asked
    patch[_ASKED_FP_PARAM] = fp
    patch[_ASKED_LAST_PARAM] = key
    return phrase, key, patch


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
        s = get_slot(k)
        # 全表都认（不按当前域过滤）：陪伴机器升级前建的 BANT 摸底目标照跑，存量不丢
        if s is not None and s["track"] in _ENUMERABLE_TRACKS and k not in out:
            out.append(k)
    return out


def selected_fill_rate(
    fields: Optional[Dict[str, Any]], keys: Optional[List[str]],
) -> float:
    """按坐席勾选槽位算的等权填充率（0..1）；勾选为空 → -1.0（未知，调用方
    按「信号缺失」处理而非当 0 分）。摸底目标（profile_discovery）的
    结算信号源——填一格进一格，全填即达成。"""
    ks = [k for k in (keys or []) if get_slot(k) is not None]
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


def slot_src(fields: Optional[Dict[str, Any]], key: str) -> str:
    """读槽位值来源（``auto`` 正则 / ``llm`` 抽取 / ``agent`` 人工）。

    裸值（旧行）/无值/未知来源 → ""——消费方（目标卡打勾清单）据此标注
    「AI 猜的还是人核实的」；空串=不标注，绝不猜。"""
    cell = (fields or {}).get(str(key or ""))
    if isinstance(cell, dict) and str(cell.get("v") or "").strip():
        s = str(cell.get("src") or "").strip().lower()
        return s if s in ("auto", "llm", "llm_pending", "agent") else ""
    return ""


# 时效感知（P1 2026-08-29，借业界记忆层 validity-window 思想的轻量版）：
# 画像单元本就带 ts——超窗的值该顺口再确认，而不是拿 90 天前的旧值继续推。
SLOT_STALE_DAYS = 90.0


def slot_age_days(
    fields: Optional[Dict[str, Any]], key: str, *, now: Optional[float] = None,
) -> float:
    """槽位值年龄（天）。无值 / 旧行裸值无 ts → -1（未知，不判陈旧）。"""
    cell = (fields or {}).get(str(key or ""))
    if not (isinstance(cell, dict) and str(cell.get("v") or "").strip()):
        return -1.0
    try:
        ts = float(cell.get("ts") or 0)
    except (TypeError, ValueError):
        return -1.0
    if ts <= 0:
        return -1.0
    import time as _t
    n = float(now if now is not None else _t.time())
    return max(0.0, (n - ts) / 86400.0)


def slot_is_stale(
    fields: Optional[Dict[str, Any]], key: str, *,
    now: Optional[float] = None, days: float = SLOT_STALE_DAYS,
) -> bool:
    """值超过 ``days`` 天未更新 → 陈旧（未知年龄绝不误标）。"""
    age = slot_age_days(fields, key, now=now)
    return age >= 0 and age >= max(1.0, float(days))


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
    # 全表 + 自定义：已采的事实不分域都进 prompt（陪伴机器上升级前采的「预算档」也算事实）
    for s in list(ALL_SLOTS) + list(_CUSTOM.values()):
        key = s["key"]
        cell = f.get(key)
        v = str((cell or {}).get("v") or "").strip() if isinstance(
            cell, dict) else str(cell or "").strip()
        if not v:
            continue
        src = ""
        if isinstance(cell, dict):
            src = str(cell.get("src") or "").strip().lower()
        piece = f"{s.get('label_zh') or key}:{v[:14]}"
        if src in ("llm", "llm_pending"):
            piece += "(待确认)"
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
# 职业自述（#108 实施91，0831 实锤「对方已经明确说自己是学生，职业槽仍空」）：
# 「我(是)做X的」句形罩不住身份名词自述——「我是学生 / I'm a student」。
# 闭集名词白名单（宁可漏采不错采）：只收无歧义的身份词，开放式「我是X」
# 绝不采（「我是觉得…/我是认真的」误采面）。
_OCC_SELF_ZH_RE = re.compile(
    r"我(?:现在|目前)?(?:还)?是(?:一名|一个|个)?"
    r"(大?学生|研究生|留学生|高中生|上班族|自由职业|宝妈|全职妈妈|"
    r"老师|教师|护士|医生|程序员|工程师|设计师|会计|律师|司机|厨师|"
    r"公务员|销售|导游|主播)"
    r"(?=[，。,.!！?？~～\s]|$)")
_OCC_SELF_EN_RE = re.compile(
    r"\bI(?:'m|\s+am)\s+(?:still\s+)?a\s+"
    r"((?:college |university |high school )?student|teacher|nurse|doctor|"
    r"engineer|designer|accountant|lawyer|driver|chef|freelancer)\b",
    re.IGNORECASE)

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

# 兴趣（P3 2026-08-18）：此前四个 relation 槽独缺 interests 正则——出厂默认
# （profile_llm 关）下摸底目标的兴趣槽永远只能人工补录。只认第一人称爱好
# 自述，三道防线（宁可漏采）：
# ① 锚定「我…喜欢/爱好是/热爱」且副词白名单（很/最/特别…都是正向词；
#    「不/没/别」不在白名单 → 「我不喜欢X」整体不命中）；
# ② 宾语含人称代词/疑问指代（含首字碎片「的/了/上…」）整条放弃——陪聊语境
#    「我喜欢你/听你说话/和你聊天」高频，是关系话术不是兴趣；刻意不收裸
#    「爱」触发词（「我爱你/爱死你了」变体太多，漏采兴趣好过错把情话入档）；
# ③ 宾语 2..12 字、截断于标点，尾部语气词剥离。
_INTERESTS_RE = re.compile(
    r"我(?:平时|周末|闲下来|没事(?:的时候)?|一直|比较|挺|很|最|特别|超|真的|蛮)*"
    r"(?:喜欢|爱好是|热爱)"
    r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fff A-Za-z0-9]{1,11})(?=[，。,.!！?？;；~～\s]|$)")
_INTERESTS_EN_RE = re.compile(
    r"\bI\s+(?:really\s+|just\s+)?(?:love|enjoy)\s+"
    r"([a-z][a-z ]{2,18}?)(?=[,.!?;]|$)", re.IGNORECASE)
# 宾语任意位置的人称/指代（中英）——命中即整条放弃（关系话术拦截网）
_INTERESTS_PRONOUN_RE = re.compile(
    r"[你妳您我他她它]|\b(?:you|your|him|her|me|us|them)\b", re.IGNORECASE)
# 宾语首字碎片黑名单：疑问/指代/虚词开头=截出来的不是爱好
_INTERESTS_STOP = re.compile(
    r"^(?:谁|啥|什么|这|那|上|的|了|就|不|没|it\b|this\b|that\b)",
    re.IGNORECASE)
_INTERESTS_TRAIL_RE = re.compile(r"(?:啦|哈|呢|哦|喔|嘛|呀|吧|了)+$")

# ── personal 轨确定性采集（N-3 #241）：只收闭集自述，存分类标签不存原文 ─────────
# 婚恋：第一人称 + 闭集状态词。「我朋友单身」不含「我」紧邻锚不命中；「我不是单身」
# 含否定不命中（否定不在白名单副词里）。英文只收 I'm single / married / divorced。
_MARITAL_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("已婚", re.compile(r"我(?:已经|已)?(?:结婚|结了婚|已婚)|我(?:老公|老婆|太太|先生|丈夫|妻子)(?=[，。,.!！?？\s]|$|[^们])|\bI(?:'m|\s+am)\s+married\b", re.IGNORECASE)),
    ("离异", re.compile(r"我(?:已经|已)?(?:离婚|离了婚|离异)(?:了)?|\bI(?:'m|\s+am)\s+divorced\b", re.IGNORECASE)),
    ("恋爱中", re.compile(r"我(?:有|谈了|在谈)(?:个|一个)?(?:男朋友|女朋友|对象|男友|女友)|我(?:在)?恋爱(?:中|了)|\bI(?:'m|\s+am)\s+(?:in a relationship|taken)\b", re.IGNORECASE)),
    ("单身", re.compile(r"我(?:现在|目前|还|一直)?(?:是)?(?:单身|没(?:有)?(?:男朋友|女朋友|对象)|未婚)(?=[，。,.!！?？~～\s]|$|[^狗])|\bI(?:'m|\s+am)\s+(?:still\s+)?single\b", re.IGNORECASE)),
)
# 家庭：孩子 / 独居 / 和父母住——闭集，存标签
_FAMILY_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("有孩子", re.compile(r"我(?:有|家有|生了)(?:个|一个|两个|三个|俩)?(?:孩子|小孩|儿子|女儿|娃|宝宝)|我(?:儿子|女儿)(?:今年|都|已经)|\bI have (?:a|two|three|\d) (?:kid|kids|child|children|son|daughter)s?\b", re.IGNORECASE)),
    ("独居", re.compile(r"我(?:一个人|自己)(?:住|生活|在这)|我独居|\bI live (?:alone|by myself)\b", re.IGNORECASE)),
    ("和父母住", re.compile(r"我(?:和|跟)(?:爸妈|父母|我妈|我爸|家人)(?:一起)?住|\bI live with my (?:parents|mom|dad|family)\b", re.IGNORECASE)),
)


# ── 槽位值语义体检（#108 实施91，0831 实锤「坐标槽被填 English」）──────────
# LLM 摘录轨的接地护栏只验「出处」（值确实在原文里）不验「语义」——客户说
# "Can we talk in English?"，LLM 把语言词塞进 location，接地照过 → 坐标=English。
# 本体检按槽位收窄：语言名绝不是 坐标/称呼/职业（interests 不拦——「喜欢学
# English」是合法兴趣）。deny-list 制（宁可漏拦不误拦），正则/LLM 两轨同吃。
_LANG_NAME_WORDS = (
    "english", "chinese", "mandarin", "cantonese", "japanese", "korean",
    "tagalog", "filipino", "spanish", "french", "german", "russian",
    "thai", "vietnamese", "indonesian", "malay", "hindi", "arabic",
    "英语", "英文", "中文", "汉语", "普通话", "粤语", "日语", "日文",
    "韩语", "韩文", "泰语", "越南语", "菲语", "他加禄语", "西语", "西班牙语",
    "法语", "德语", "俄语", "印尼语", "马来语",
)
_LANG_NAME_SET = {w.casefold() for w in _LANG_NAME_WORDS}
_LANG_GUARDED_SLOTS = ("location", "name", "occupation")


def slot_value_suspect(key: str, value: str) -> str:
    """槽位值语义体检：返回不合格原因（空串=通过）。纯函数绝不抛。

    当前唯一规则＝语言名不得进 坐标/称呼/职业（#108 实锤形态）。新增规则
    往这里加——两条采集轨（正则/LLM）与人工回填校验共用单点。
    """
    try:
        k = str(key or "").strip().lower()
        v = str(value or "").strip()
        if not v:
            return ""
        if k in _LANG_GUARDED_SLOTS:
            vn = v.casefold().strip(" .。!！?？~～")
            if vn in _LANG_NAME_SET:
                return "lang_name"
            # 「English语/英语课」这类带缀形态也拦（坐标/称呼语境下无合法解释）
            if k == "location" and any(
                    w in vn for w in _LANG_NAME_SET if len(w) >= 4):
                return "lang_name"
        return ""
    except Exception:
        return ""


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
    if "occupation" not in seen:
        m = _OCC_SELF_ZH_RE.search(t) or _OCC_SELF_EN_RE.search(t)
        if m:
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

    m = _INTERESTS_RE.search(t) or _INTERESTS_EN_RE.search(t)
    if m:
        hobby = _INTERESTS_TRAIL_RE.sub("", m.group(1).strip()).strip()
        if (len(hobby) >= 2 and not _INTERESTS_STOP.match(hobby)
                and not _INTERESTS_PRONOUN_RE.search(hobby)):
            _add("interests", hobby)

    # personal 轨（N-3 #241）：婚恋 / 家庭闭集标签。收入 / 资产是敏感槽，**永不**自动采
    # ——只许坐席手录或 LLM 摘录轨在对方自己说出来时接地。
    for label, pat in _MARITAL_PATTERNS:
        if pat.search(t):
            _add("marital_status", label)
            break
    fam = [label for label, pat in _FAMILY_PATTERNS if pat.search(t)]
    if fam:
        _add("family_status", "、".join(fam[:2]))

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
    "ALL_SLOTS",
    "CUSTOM_TRACK",
    "PERSONAL_SLOTS",
    "SECONDARY_TRACK",
    "SENSITIVE_ASK_DISCIPLINE",
    "SLOTS",
    "TRACKS",
    "TRACKS_BY_DOMAIN",
    "capture_churn_reason",
    "capture_from_text",
    "churn_offer_steer",
    "churn_strategy_hint",
    "clear_custom_slots",
    "custom_slot_key",
    "custom_slot_labels",
    "custom_slots",
    "facts_line",
    "fill_rates",
    "HARD_ASK_DISCIPLINE",
    "gap_hint",
    "get_slot",
    "inject_ask",
    "is_custom_slot_key",
    "load_custom_slots_from_config",
    "missing_slots",
    "next_unfilled_slot",
    "normalize_custom_slot",
    "register_custom_slots",
    "resolve_inject_gap",
    "secondary_track",
    "slot_ask",
    "slot_is_sensitive",
    "slots_for_domain",
    "parse_selected_slots",
    "selected_fill_rate",
    "slot_keys",
    "slot_label",
    "slot_src",
    "slot_value",
    "tracks_for",
]
