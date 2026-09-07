"""业务域单一真值（N-3 #241，老板决策 D-N1，2026-09-08）。

背景：skuio 机 13:14 启动日志同一秒里 ``Domain hook registered: ConversionDomainHook``
与 ``Domain persona set: 线上陪伴``——人设是陪伴，域包叫 conversion。根因不是「装错了包」：
``conversion`` 域包（manifest 自述「多语言情感陪伴与日常闲聊」，persona 就是「线上陪伴」）
在代码里**就是**陪伴包（skill_manager / ai_client / sender 十几处以
``effective_domain_name(cfg) == "conversion"`` 当「陪伴部署」判定）。真正错配的是挂在这个
包上的**销售物料**：KB 分类（产品价值 / 转化话术 / 异议处理 / 命理）、BANT 摸底槽位、
「转化成交」目标模板、「标成交」产品 / 金额、「营销目标」字样、GXP 支付话术种子——它们
在 zhiliao 生产（厂商自家售卖机器人）是对的，在陪伴运营的客户机上全是噪音。

所以本模块不换域包，而是给出**业务域**这一个真值，让每个物料消费方读它：

- 值域：``companion``（陪伴运营）/ ``sales``（业务 / 销售）。
- 来源序：显式配置 ``business_domain``（顶层键；可在后台改）→ 推导（见
  :func:`infer_business_domain`）。推导结果由 :func:`ensure_business_domain` 在启动期
  **写进 overlay 一次**，之后就是显式值（「已装机器按当前形态推导一次并写入」）。
- 推导口径：显式 ``domain`` 指向 conversion 以外的包（payment / ecommerce / …）→ sales；
  桌面客户机形态（``desktop_mode.is_desktop_client``，与 L-4 ``ui_flavor=client`` 同信号）
  → companion；服务器 / 内部部署 → sales（zhiliao 生产零变化）。
- 消费方：``domain_loader``（按业务域选 hook 类与 KB 分类变体）、``goals.templates``
  （陪伴域不下发「转化成交」类目）、``goals.profile_slots``（陪伴 / 销售各自的摸底槽位表）、
  ``goal_routes``（画像 schema / 「标成交」字段）、``kb_store``（系统话术种子按域播）。
- 进程级 ``active``：装配层解析一次后 :func:`set_active_business_domain`；拿不到配置的
  纯函数消费方（profile_slots）读 :func:`active_business_domain`。

不动 ``effective_domain_name`` 的返回值：那是「装的是哪个域包」，本模块是「这台机器做什么
生意」，两者正交——陪伴域仍装 conversion 包（陪伴 system prompt / 人设都在那里）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.business_domain")

BUSINESS_DOMAIN_KEY = "business_domain"
COMPANION = "companion"
SALES = "sales"
BUSINESS_DOMAINS: Tuple[str, ...] = (COMPANION, SALES)

# 别名（配置里写中文 / 旧词也认）
_ALIASES = {
    "companion": COMPANION, "companionship": COMPANION, "陪伴": COMPANION,
    "陪聊": COMPANION, "情感陪伴": COMPANION, "relationship": COMPANION,
    "sales": SALES, "sale": SALES, "business": SALES, "conversion": SALES,
    "marketing": SALES, "销售": SALES, "业务": SALES, "转化": SALES, "获客": SALES,
}

# 域包名 → 该包本身就是「销售」生意（装了它就不必再问业务域）
_SALES_PACKS = frozenset({"payment", "ecommerce", "crypto", "it_helpdesk",
                          "legal", "education", "community", "general"})

_LABELS = {
    COMPANION: {"zh": "陪伴", "en": "Companion"},
    SALES: {"zh": "销售", "en": "Sales"},
}

_ACTIVE: Optional[str] = None


def _cfg_dict(config: Any) -> Dict[str, Any]:
    """dict / ConfigManager（带 .config）/ None → dict。"""
    if isinstance(config, dict):
        return config
    root = getattr(config, "config", None)
    return root if isinstance(root, dict) else {}


def normalize_business_domain(raw: Any) -> str:
    """任意写法 → ``companion`` / ``sales``；认不出 → ""。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    return _ALIASES.get(s, s if s in BUSINESS_DOMAINS else "")


def explicit_business_domain(config: Any) -> str:
    """只看显式配置 ``business_domain``；缺省 / 非法 → ""。"""
    cfg = _cfg_dict(config)
    return normalize_business_domain(cfg.get(BUSINESS_DOMAIN_KEY))


def infer_business_domain(config: Any) -> str:
    """缺显式值时的推导（纯判定，不落盘）。

    · ``domain`` 显式指向 conversion 以外的域包 → sales（payment 关插件时回落 conversion
      的情况按 conversion 处理，与 ``effective_domain_name`` 同口径）；
    · 桌面客户机（env ``AITR_DESKTOP_MODE`` / ``app.desktop_mode``）→ companion——用户版
      就是陪伴运营（M-5 A #217 已按 client 形态藏「转化成交」，这里把同一判断升成域）；
    · 其余（服务器 / 内部部署）→ sales。
    """
    cfg = _cfg_dict(config)
    try:
        from src.utils.domain_policy import effective_domain_name
        pack = effective_domain_name(cfg) if cfg else ""
    except Exception:
        pack = str(cfg.get("domain") or "").strip()
    if pack and pack != "conversion" and pack in _SALES_PACKS:
        return SALES
    try:
        from src.utils.desktop_mode import is_desktop_client
        if is_desktop_client(cfg if cfg else None):
            return COMPANION
    except Exception:
        pass
    return SALES


def resolve_business_domain(config: Any = None) -> str:
    """显式值优先，否则推导。永远返回 ``companion`` / ``sales`` 之一。"""
    return explicit_business_domain(config) or infer_business_domain(config)


def is_companion(config: Any = None) -> bool:
    return resolve_business_domain(config) == COMPANION


def business_domain_label(bd: str, lang: str = "zh") -> str:
    d = _LABELS.get(normalize_business_domain(bd) or "", {})
    return d.get("en" if str(lang).lower().startswith("en") else "zh", str(bd or ""))


# ── 进程级 active（装配层解析一次；无配置句柄的纯函数消费方读它）──────────────

def set_active_business_domain(bd: Any) -> str:
    """装配层（DomainLoader.load）解析后登记；返回规范化值。空 / 非法 → 不改。"""
    global _ACTIVE
    v = normalize_business_domain(bd)
    if v:
        _ACTIVE = v
    return _ACTIVE or ""


def active_business_domain(config: Any = None) -> str:
    """进程级 active；未登记时回落 :func:`resolve_business_domain`（给了配置按配置，
    否则只看 env——桌面壳设了 AITR_DESKTOP_MODE 即 companion）。"""
    if _ACTIVE:
        return _ACTIVE
    return resolve_business_domain(config)


def reset_active_business_domain() -> None:
    """测试用。"""
    global _ACTIVE
    _ACTIVE = None


# ── 启动期：推导一次并写入 overlay ────────────────────────────────────────────

def ensure_business_domain(config_manager: Any) -> Tuple[str, bool]:
    """返回 ``(business_domain, persisted_now)``。

    显式值 → 原样返回（不写）。缺省 → 推导，并经 ``config_manager.set_overlay_flag``
    写入 ``config.local.yaml``（保注释路径），从此这台机器的业务域是**显式**的、可在后台改。
    没有 set_overlay_flag（测试桩 / 纯 dict）→ 只返回推导值不落盘。绝不抛。
    """
    explicit = explicit_business_domain(config_manager)
    if explicit:
        set_active_business_domain(explicit)
        return explicit, False
    inferred = infer_business_domain(config_manager)
    set_active_business_domain(inferred)
    setter = getattr(config_manager, "set_overlay_flag", None)
    if not callable(setter):
        return inferred, False
    try:
        ok, msg = setter(BUSINESS_DOMAIN_KEY, inferred)
        if ok:
            logger.info("business_domain 缺省 → 按部署形态推导为 %s 并写入 overlay（可在后台改）",
                        inferred)
            return inferred, True
        logger.warning("business_domain 推导为 %s 但写入 overlay 失败：%s", inferred, msg)
    except Exception:
        logger.debug("business_domain 落盘异常（下次启动再试）", exc_info=True)
    return inferred, False


def describe(config: Any = None, *, hook_name: str = "", pack: str = "") -> str:
    """启动日志一行：``business_domain=companion（hook=CompanionDomainHook，pack=conversion）``。"""
    bd = active_business_domain(config)
    parts = []
    if hook_name:
        parts.append(f"hook={hook_name}")
    if pack:
        parts.append(f"pack={pack}")
    tail = f"（{'，'.join(parts)}）" if parts else ""
    return f"business_domain={bd}{tail}"


__all__ = [
    "BUSINESS_DOMAINS",
    "BUSINESS_DOMAIN_KEY",
    "COMPANION",
    "SALES",
    "active_business_domain",
    "business_domain_label",
    "describe",
    "ensure_business_domain",
    "explicit_business_domain",
    "infer_business_domain",
    "is_companion",
    "normalize_business_domain",
    "reset_active_business_domain",
    "resolve_business_domain",
    "set_active_business_domain",
]
