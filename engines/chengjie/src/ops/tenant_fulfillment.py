"""托管 SaaS 订单开通守护的**纯决策逻辑**（HTTP/编排由 scripts/tenant_fulfill_watch.py 注入）。

与 `chatx_fulfillment.py`（装机版 license 签发）**并行的另一条履约路径**：
- 装机版订单 → 签发 license token 回填 → 客户粘贴激活（现有 fulfill_chatx_watch）；
- **托管订单 → provision 实例 + expose 公网 → 回填「公网地址 + 登录令牌」**（本模块 + tenant_ops）。

═══ 安全不变量（本模块存在的第一理由）═══
`is_hosted_order` 必须**保守**：只认**显式托管信号**，绝不能误匹配现有装机 SKU
（chatx-entry/team/flagship / lingox-*）——否则会给买装机版的客户误开托管实例。
显式信号（满足其一）：
  1. `order.delivery == "hosted"`（website 未来给托管单打的交付标；首选，与 SKU 命名解耦）；
  2. `sku_id` 以 `-hosted` 结尾或含 `hosted`（如 `chatx-hosted-team`）。
今日默认下单 `delivery=installed`（或字段缺失视同装机）→ `select_hostable` 对现网零副作用。
官网已支持下单传 `delivery=hosted`；装机侧 `fulfillment_payload_for_order` 已排除
``is_hosted_order``（单一事实源 ``src.licensing.order_delivery``），防双发。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.licensing.order_delivery import is_hosted_delivery, is_hosted_order

# product_id → tenant_ops provision 的 --product（今日两条产品线）
_PRODUCT_MAP = {"zhiliao": "zhiliao", "tongyi": "tongyi", "chatx": "zhiliao", "lingox": "tongyi"}

_SLUG_SAFE = re.compile(r"[^a-z0-9]+")

# ── 托管 AI 网关日额度（字符）──
# 计量主体 = IID:<instance_id>；默认环境 50k/日太小，开通/续费时按套餐覆写。
# 公式：席位 × 每席日额度，再按档位夹 floor/cap——改席位表即联动，不必另维护平行价目。
_CHARS_PER_SEAT_DAY = 25_000
_TIER_FLOOR = {"basic": 100_000, "pro": 250_000, "flagship": 800_000}
_TIER_CAP = {"basic": 200_000, "pro": 500_000, "flagship": 1_500_000}
# 付费托管但 SKU 未入权威表（未来新档）→ 高于试用默认，避免「开了却几乎不能聊」
_DEFAULT_HOSTED_DAILY_CHARS = 200_000

# 再导出：历史测试/文档 `from src.ops.tenant_fulfillment import is_hosted_*` 继续可用
__all__ = [
    "is_hosted_delivery",
    "is_hosted_order",
    "order_product",
    "order_slug",
    "hosted_plan_args_for_order",
    "select_hostable",
    "manual_hosted_followup",
    "period_days",
    "build_delivery_code",
    "build_renewal_code",
    "resolve_catalog_sku",
    "gw_daily_chars_for_order",
    "gw_budget_for_order",
    "is_existing_hosted_tenant",
    "stack_expires_at",
    "renew_order_url",
]


def order_product(order: Dict[str, Any]) -> str:
    """订单 → tenant_ops --product。sku_id 前缀优先，product_id 兜底，默认 zhiliao。"""
    sku = str((order or {}).get("sku_id") or "").strip().lower()
    for pref, prod in (("chatx", "zhiliao"), ("lingox", "tongyi")):
        if sku.startswith(pref):
            return prod
    pid = str((order or {}).get("product_id") or "").strip().lower()
    return _PRODUCT_MAP.get(pid, "zhiliao")


def order_slug(order: Dict[str, Any]) -> str:
    """订单 → 子域 slug 候选（显式 order.slug 优先，否则 contact/id 派生）。

    仅产出候选；DNS 合法性/保留字/唯一性由 tenant_lifecycle.validate_slug 与开通期把关。
    """
    explicit = str((order or {}).get("slug") or "").strip().lower()
    if explicit:
        return _SLUG_SAFE.sub("-", explicit).strip("-")
    base = str((order or {}).get("contact") or order.get("id") or "").strip().lower()
    slug = _SLUG_SAFE.sub("-", base).strip("-")
    return slug[:40] or "tenant"


def hosted_plan_args_for_order(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一笔 paid 托管订单映射为 tenant_ops provision 的关键字参数；不可映射 → None。

    契约最小面（与 provision_instance 对齐）：product + customer(=contact) + instance_id 派生。
    contact 缺失 → None（没有客户标识不开通，转人工，绝不裸开）。
    """
    if not is_hosted_order(order):
        return None
    contact = str((order or {}).get("contact") or "").strip()
    oid = str((order or {}).get("id") or "").strip()
    if not contact or not oid:
        return None
    product = order_product(order)
    slug = order_slug(order)
    return {
        "order_id": oid,
        "product": product,
        "customer": contact,
        "slug": slug,
        # instance_id：product + slug（下划线，tenant_lifecycle 派生子域时再转连字符）
        "instance_id": f"{product}_{slug.replace('-', '_')}"[:48],
    }


def select_hostable(
    orders: List[Dict[str, Any]],
    done_ids: Optional[set] = None,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """从 paid 订单挑出可自动托管开通的单，返回 [(order, plan_args), ...]。

    与 select_fulfillable 同骨架：跳过无 id / 已处理(done) / 已回填 code / 不可映射。
    幂等安全——done_ids 由守护 state 提供，重复跑不会重复开通。
    """
    done = set(done_ids or ())
    out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "").strip()
        if not oid or oid in done:
            continue
        if str((o or {}).get("code") or "").strip():  # 已回填过交付信息
            continue
        args = hosted_plan_args_for_order(o)
        if args is not None:
            out.append((o, args))
    return out


def manual_hosted_followup(
    orders: List[Dict[str, Any]],
    done_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """托管订单但映射不出 plan（缺 contact 等）→ 点名转人工，绝不静默漏单。"""
    done = set(done_ids or ())
    out: List[Dict[str, Any]] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "").strip()
        if not oid or oid in done:
            continue
        if is_hosted_order(o) and hosted_plan_args_for_order(o) is None:
            out.append(o)
    return out


def period_days(order: Dict[str, Any]) -> int:
    """订单周期 → 托管有效天数（与装机 license 同一张表，单一事实源）。

    monthly=32 / quarterly=92 / annual=366（含缓冲）；未知/缺失按 monthly 口径。
    """
    from src.licensing.chatx_fulfillment import DEFAULT_PERIOD_DAYS, PERIOD_DAYS

    return PERIOD_DAYS.get(str((order or {}).get("period") or "").lower(),
                           DEFAULT_PERIOD_DAYS)


def resolve_catalog_sku(sku_id: str) -> str:
    """托管 SKU → 装机权威表可查的目录 SKU（``chatx-hosted-team`` → ``chatx-team``）。

    剥 ``-hosted`` / ``hosted-`` 标记；装机单原样返回。查不到权威表时调用方自行回落。
    """
    s = str(sku_id or "").strip().lower()
    if not s:
        return ""
    return s.replace("-hosted", "").replace("hosted-", "") or s


def gw_daily_chars_for_order(order: Dict[str, Any]) -> int:
    """托管订单 → AI 网关**日字符**额度（按套餐席位/月包推算，纯函数）。

    - chatx：席位 × 25k/日，再按 plan 档夹 floor/cap；
    - lingox：有 ``included_chars_monthly`` 时用月包÷30（不限=旗舰 cap）；
    - 未知 SKU：``_DEFAULT_HOSTED_DAILY_CHARS``（仍高于试用 50k 默认）。
    """
    sku = resolve_catalog_sku(str((order or {}).get("sku_id") or ""))
    if not sku:
        return _DEFAULT_HOSTED_DAILY_CHARS
    try:
        from src.licensing.chatx_fulfillment import sku_spec

        spec = sku_spec(sku)
    except Exception:  # noqa: BLE001 - 未知/仅人工 SKU → 默认档
        return _DEFAULT_HOSTED_DAILY_CHARS

    plan = str(spec.get("plan") or "basic").lower()
    floor = _TIER_FLOOR.get(plan, _TIER_FLOOR["basic"])
    cap = _TIER_CAP.get(plan, _TIER_CAP["pro"])

    monthly = spec.get("included_chars_monthly")
    if monthly is not None:
        try:
            m = int(monthly)
        except (TypeError, ValueError):
            m = -1
        if m <= 0:  # 不限字符
            return _TIER_CAP["flagship"]
        return max(floor, m // 30)

    try:
        seats = max(1, int(spec.get("seats") or 1))
    except (TypeError, ValueError):
        seats = 1
    return min(cap, max(floor, seats * _CHARS_PER_SEAT_DAY))


def gw_budget_for_order(order: Dict[str, Any], instance_id: str) -> Optional[Dict[str, Any]]:
    """组装 ``POST /api/admin/gw-budget`` 载荷；非托管/无实例 → None。"""
    iid = str(instance_id or "").strip()
    if not iid or not is_hosted_order(order or {}):
        return None
    budget = gw_daily_chars_for_order(order)
    oid = str((order or {}).get("id") or "").strip()
    sku = str((order or {}).get("sku_id") or "").strip()
    period = str((order or {}).get("period") or "monthly").strip() or "monthly"
    return {
        "subject": f"IID:{iid}",
        "budget": int(budget),
        "note": f"order={oid} sku={sku} period={period}",
    }


def is_existing_hosted_tenant(card: Dict[str, Any]) -> bool:
    """交付卡是否已是「曾经送达过」的存量租户（续费路径判据）。

    要同时有公网地址 + 到期账本（``expires_at`` 或 ``expiry_order``）——仅 expose
    持单中的卡只有 URL、无账本，仍走首开交付（含初始密码）。
    """
    if not str((card or {}).get("public_url") or "").strip():
        return False
    return bool(
        str((card or {}).get("expires_at") or "").strip()
        or str((card or {}).get("expiry_order") or "").strip()
    )


def stack_expires_at(
    old_expires_at: Optional[str],
    days: int,
    now: Optional[float] = None,
) -> str:
    """续费/首开共用的到期叠期：从 ``max(now, 旧到期)`` 起加 ``days`` 天。

    未过期续费＝剩余时长不浪费；已过期续费＝从今天起算（不给空窗白送）。
    旧串缺/坏格式 → 视同无账本，从 now 起算（=首开语义）。
    """
    try:
        d = max(1, int(days))
    except (TypeError, ValueError):
        d = 32
    base = float(time.time() if now is None else now)
    raw = str(old_expires_at or "").strip()
    if raw:
        try:
            old = time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
            if old > base:
                base = old
        except Exception:  # noqa: BLE001
            pass
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + d * 86400))


def renew_order_url(sku_id: str, site: str = "https://bd2026.cc") -> str:
    """续费深链：SKU → 官网下单页（同档位 + hosted 预选）。

    sku → offer 前缀映射与 website `lib/offer-map.ts` 的 ORDER_SKU_MAP 反向对应
    （chatx-*→autochat-* / lingox-*→translate-*）；识别不了的 SKU 回落裸下单页
    （客户自选档位），绝不拼出不存在的 plan 参数。
    """
    base = (site or "https://bd2026.cc").rstrip("/")
    sku = resolve_catalog_sku(sku_id)
    offer = ""
    if sku.startswith("chatx-"):
        offer = "autochat-" + sku[len("chatx-"):]
    elif sku.startswith("lingox-"):
        offer = "translate-" + sku[len("lingox-"):]
    if offer:
        return f"{base}/order?plan={offer}&delivery=hosted"
    return f"{base}/order?delivery=hosted"


def build_delivery_code(public_url: str, login_token: str, instance_id: str,
                        username: str = "owner") -> str:
    """回填给订单的 code：客户拿到的「网址 + 账号 + 初始密码 + 第一步做什么」交付串。

    用简短可读文本而非 JSON——它会被当作激活码直接展示给客户。三点刻意如此：
    ① 交付的是**客户自己的账号**（`owner`，凭据职责分离；我方 auth_token 绝不进此串
       ——实测客户改密**不会**使 token 直登失效，混发等于泄露即无法止血）；
    ② 提示自助改密（`/api/change-password` 对该账号可用，已实测）；
    ③ 网址用 ``/login?next=/workspace/dash`` 深链——登录后直达上线自检看板
       （三步开通：配 AI → 接渠道 → 开自动回复），不再绕 `/cases`。
    """
    from src.ops.tenant_lifecycle import apply_public_base

    base = (public_url or "").rstrip("/")
    login = apply_public_base(base)["login_url"] if base else "/login?next=/workspace/dash"
    return (f"网址: {login}  |  账号: {username}  |  初始密码: {login_token}  "
            f"（打开网址登录后直达「上线自检」看板，按三步开通：配 AI → 接渠道 → 开自动回复；"
            f"建议在 设置→修改密码 自行改密。实例: {instance_id}）")


def build_renewal_code(
    public_url: str,
    instance_id: str,
    expires_at: str,
    username: str = "owner",
) -> str:
    """续费回填串：告之延期到何时 + 原站登录；**绝不重发初始密码**（多半已改）。"""
    from src.ops.tenant_lifecycle import apply_public_base

    base = (public_url or "").rstrip("/")
    login = apply_public_base(base)["login_url"] if base else "/login?next=/workspace/dash"
    exp = str(expires_at or "").strip() or "(见工作台)"
    return (f"续费已到账 · 有效期延至 {exp}  |  网址: {login}  |  账号: {username}  "
            f"（请用原密码登录；忘记密码由客服重置。实例: {instance_id}）")
