# -*- coding: utf-8 -*-
"""人设「工作类型」（kind）单一真值（P0-1，2026-09-29）。

背景（8E56 机实锤）：客户用「专业客服」模板建了人设 q567，客户问「网站有什么活动」，
草稿链按**机器级**陪聊判定（域包 conversion + 闲聊意图 + 无支付词）在检索前整段跳过
知识库，AI 只能答「等确认」。人设向导五个模板只写语气（role / personality / speaking），
没有任何字段表达「这个号是做客服、做销售还是陪聊」——知识库该不该查、查无了怎么办，
全靠机器级 ``business_domain`` 猜，一台机器一个陪聊号加一个客服号无法并存。

本模块把「这个人设做什么」落成 persona 顶层键 ``kind``：

- 值域：``companion``（陪聊）/ ``support``（客服）/ ``sales``（销售）。
- 解析序（:func:`persona_kind`）：显式 ``kind`` → 按 tags / role / name 关键词推断
  （存量人设零改写即生效，Studio 显示「自动推断」）→ 机器 ``business_domain`` 缺省
  （sales → sales；companion → companion）。
- 消费方：``kb_policy``（客服 / 销售 kind 必查知识库、事实优先；陪聊维持旧闸）、
  ``ai_client`` 注入措辞、人设工作室 kind 选择器与卡片。

``kind`` 不进人设 prompt 块（登记在 ``persona_manager.PROMPT_EXEMPT_FIELDS``）——它是
策略开关，措辞由消费方各自决定，人设块里说「我是客服类型」只会穿帮。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

KIND_COMPANION = "companion"
KIND_SUPPORT = "support"
KIND_SALES = "sales"
KINDS: Tuple[str, ...] = (KIND_COMPANION, KIND_SUPPORT, KIND_SALES)

_ALIASES = {
    "companion": KIND_COMPANION, "companionship": KIND_COMPANION, "chat": KIND_COMPANION,
    "陪聊": KIND_COMPANION, "陪伴": KIND_COMPANION, "情感": KIND_COMPANION,
    "support": KIND_SUPPORT, "service": KIND_SUPPORT, "cs": KIND_SUPPORT,
    "customer_service": KIND_SUPPORT, "helpdesk": KIND_SUPPORT, "aftersales": KIND_SUPPORT,
    "客服": KIND_SUPPORT, "售后": KIND_SUPPORT,
    "sales": KIND_SALES, "sale": KIND_SALES, "business": KIND_SALES, "consultant": KIND_SALES,
    "销售": KIND_SALES, "导购": KIND_SALES, "商务": KIND_SALES, "转化": KIND_SALES,
}

# 推断词表：客服优先于销售优先于陪聊——客服 / 销售都要查库，误判成陪聊才是事故方向。
_SUPPORT_HINTS = (
    "客服", "售后", "支持专员", "技术支持", "客户支持", "工单", "support", "helpdesk",
    "customer service", "service agent", "after-sales", "aftersales",
)
_SALES_HINTS = (
    "导购", "销售", "成交", "商务", "顾问", "转化", "带货", "电商", "客户成功",
    "sales", "consultant", "advisor", "conversion", "e-commerce", "ecommerce",
)
_COMPANION_HINTS = (
    "陪聊", "陪伴", "闺蜜", "女友", "男友", "治愈", "情感", "私聊对象", "恋人",
    "companion", "bestie", "girlfriend", "boyfriend", "soulmate", "healing",
)

_LABELS = {
    KIND_COMPANION: {"zh": "陪聊", "en": "Companion"},
    KIND_SUPPORT: {"zh": "客服", "en": "Support"},
    KIND_SALES: {"zh": "销售", "en": "Sales"},
}


def normalize_kind(raw: Any) -> str:
    """任意写法 → ``companion`` / ``support`` / ``sales``；认不出 → ""。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    return _ALIASES.get(s, s if s in KINDS else "")


def explicit_kind(persona: Any) -> str:
    """只看人设显式 ``kind``；缺省 / 非法 → ""。"""
    if not isinstance(persona, dict):
        return ""
    return normalize_kind(persona.get("kind"))


def _blob(persona: Dict[str, Any]) -> str:
    parts = []
    for key in ("role", "name"):
        v = persona.get(key)
        if isinstance(v, str):
            parts.append(v)
    tags = persona.get("tags")
    if isinstance(tags, (list, tuple)):
        parts.extend(str(t) for t in tags if t is not None)
    elif isinstance(tags, str):
        parts.append(tags)
    return " ".join(parts).lower()


def infer_kind(persona: Any) -> Tuple[str, str]:
    """按 tags / role / name 关键词推断 → (kind, source)；推不出 → ("", "")。

    tags 里直接写了 kind 别名（如 ``客服`` / ``sales``）优先于自由文本关键词。
    """
    if not isinstance(persona, dict):
        return "", ""
    tags = persona.get("tags")
    if isinstance(tags, (list, tuple)):
        for t in tags:
            k = normalize_kind(t)
            if k:
                return k, "tag"
    blob = _blob(persona)
    if not blob:
        return "", ""
    if any(h in blob for h in _SUPPORT_HINTS):
        return KIND_SUPPORT, "keyword"
    if any(h in blob for h in _SALES_HINTS):
        return KIND_SALES, "keyword"
    if any(h in blob for h in _COMPANION_HINTS):
        return KIND_COMPANION, "keyword"
    return "", ""


def default_kind(config: Any = None) -> str:
    """机器级缺省：``business_domain`` sales → sales，其余 → companion。绝不抛。"""
    try:
        from src.utils.business_domain import resolve_business_domain
        return KIND_SALES if resolve_business_domain(config) == "sales" else KIND_COMPANION
    except Exception:
        return KIND_COMPANION


def resolve_kind(persona: Any, config: Any = None) -> Tuple[str, str]:
    """→ (kind, source)，source ∈ {explicit, tag, keyword, default}。永远返回合法 kind。"""
    k = explicit_kind(persona)
    if k:
        return k, "explicit"
    k, src = infer_kind(persona)
    if k:
        return k, src
    return default_kind(config), "default"


def persona_kind(persona: Any, config: Any = None) -> str:
    """人设的生效工作类型（显式 > 推断 > 机器缺省）。"""
    return resolve_kind(persona, config)[0]


def kind_label(kind: str, lang: str = "zh") -> str:
    k = normalize_kind(kind) or KIND_COMPANION
    row = _LABELS.get(k) or _LABELS[KIND_COMPANION]
    return row["en"] if str(lang or "").lower().startswith("en") else row["zh"]


def kind_requires_kb(kind: str) -> bool:
    """客服 / 销售 kind：知识库是事实源，每轮必查。陪聊：按旧闸（可选）。"""
    return normalize_kind(kind) in (KIND_SUPPORT, KIND_SALES)


def ident_kb_code(persona: Any, *, unselected: bool = False, config: Any = None) -> str:
    """账号身份行的知识库状态码（P1-4）：``unselected`` / ``must`` / ``open`` / ``skip``。

    账号级一句话，不看单轮意图——「这个号正在做客服，知识库必查」。未选人设 /
    空人设 → unselected；``kb_access`` → open；客服 / 销售（人设自己表态）→ must；
    其余（陪聊 / 机器缺省）→ skip（闲聊不查）。绝不抛。
    """
    try:
        if unselected or not isinstance(persona, dict) or not persona:
            return "unselected"
        if bool(persona.get("kb_access")):
            return "open"
        k, src = resolve_kind(persona, config)
        if src != "default" and kind_requires_kb(k):
            return "must"
        return "skip"
    except Exception:
        return "unselected"


def kinds_in_use(config: Any = None) -> Tuple[str, ...]:
    """本机人设档案里**自己表态**（显式 / 标签 / 角色推断）的 kind 集合（P1-2）。

    机器缺省不算——纯陪聊机没建过客服人设，分类表 / 模板一个字不变。任何异常 → 空。
    """
    out = set()
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profiles = getattr(pm, "_profile_personas", {}) or {}
        for p in list(profiles.values()):
            k, src = resolve_kind(p, config)
            if src != "default" and k:
                out.add(k)
    except Exception:
        return ()
    return tuple(sorted(out))


def kind_view(persona: Any, config: Any = None) -> Dict[str, Any]:
    """Studio / 列表用的展示视图：{kind, source, explicit, label_zh, label_en}。"""
    k, src = resolve_kind(persona, config)
    return {
        "kind": k,
        "source": src,
        "explicit": explicit_kind(persona),
        "label_zh": kind_label(k, "zh"),
        "label_en": kind_label(k, "en"),
    }


__all__ = [
    "KINDS", "KIND_COMPANION", "KIND_SUPPORT", "KIND_SALES",
    "normalize_kind", "explicit_kind", "infer_kind", "default_kind",
    "resolve_kind", "persona_kind", "kind_label", "kind_requires_kb", "kind_view",
    "kinds_in_use", "ident_kb_code",
]
