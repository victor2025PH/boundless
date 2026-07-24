"""会员中心「购买 / 续费」CTA → 官网 /order 深链（融合实例 P7）。

背景：P6 演练证明履约链路已通，但会员页 CTA 原先只透传 ``licensing.shop_url``
裸链——客户点进去落在 AvatarHub 默认档，要再点两次 Tab 才到自己的产品线。
本模块按当前授权（sku_id / product_id / 额度是否耗尽）选一个 offer id，
拼到 shop_url 上（``?plan=<offer>&period=monthly``），与
``website/lib/order-lines.ts::familyOfPlan`` 契约对齐。

纯函数、零 I/O；未知授权 / 非 /order 基址 → 原样回传（绝不破坏自营/TG 客服链）。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# sku_id 前缀 → 官网 offer id（长前缀优先，防 chatx-entry 被 chatx 误匹配）。
# lic_id 形如 ``chatx-entry-AH-…``，startswith 即可。
_SKU_OFFER_PREFIXES = (
    ("chatx-flagship", "autochat-flagship"),
    ("chatx-team", "autochat-team"),
    ("chatx-entry", "autochat-entry"),
    ("lingox-pro", "translate-pro"),
    ("lingox-team", "translate-team"),
)

# plan 档位回退（老授权无 sku_id 时）：只给「续费同档」的保守映射，
# community 不猜——裸链让客户自己选产品线。
_PLAN_OFFER = {
    "basic": "autochat-entry",
    "pro": "autochat-team",
    "flagship": "autochat-flagship",
}


def resolve_shop_offer(
    *,
    sku_id: str = "",
    product_id: str = "",
    plan: str = "",
    quota_exceeded: bool = False,
) -> str:
    """根据当前授权挑一个 /order?plan= offer id；挑不出 → 空串（用裸 shop_url）。"""
    sku = str(sku_id or "").strip().lower()
    pid = str(product_id or "").strip().lower()
    # 额度耗尽优先加量包（通译计量线）；chatx 一般不限字符，此分支自然不触发。
    if quota_exceeded and (sku.startswith("lingox") or pid == "tongyi"):
        return "translate-charpack"
    for prefix, offer in _SKU_OFFER_PREFIXES:
        if sku.startswith(prefix):
            return offer
    if pid == "tongyi":
        return "translate-team"
    if pid == "zhiliao":
        return "autochat-entry"
    return _PLAN_OFFER.get(str(plan or "").strip().lower(), "")


def build_shop_url(
    base: str,
    offer: str = "",
    *,
    period: str = "monthly",
) -> str:
    """把 offer 深链参数并进 shop_url。

    - 基址空 / offer 空 → 原样；
    - 基址路径不含 ``order``（TG 客服、外链商城）→ 原样，绝不污染；
    - 已有 plan 参数 → 不覆盖（运营手写深链优先）。
    """
    base = str(base or "").strip()
    offer = str(offer or "").strip().lower()
    if not base or not offer:
        return base
    try:
        parts = urlsplit(base)
    except Exception:
        return base
    # 相对 "/order" 或绝对 "https://x/order[/]"：路径任一节为 order 才深链；
    # TG 客服 / 外链商城等非下单页原样回传，绝不污染。
    segs = [p for p in (parts.path or "").lower().split("/") if p]
    if "order" not in segs:
        return base
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if q.get("plan"):
        return base  # 运营手写深链优先，不覆盖
    q["plan"] = offer
    per = str(period or "monthly").strip().lower()
    if per == "annual":
        q["period"] = "annual"
    else:
        q.pop("period", None)  # 月付不写 period——与官网默认一致，URL 更干净
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(q), parts.fragment))


def shop_cta_from_license(
    base: str,
    lic: Optional[dict] = None,
    *,
    quota_exceeded: bool = False,
) -> dict:
    """装配会员页 shop 块：{url, offer}。``url`` 已含深链（可深则深）。"""
    lic = lic or {}
    offer = resolve_shop_offer(
        sku_id=str(lic.get("sku_id") or lic.get("lic_id") or ""),
        product_id=str(lic.get("product_id") or ""),
        plan=str(lic.get("plan") or ""),
        quota_exceeded=bool(quota_exceeded),
    )
    return {"url": build_shop_url(base, offer), "offer": offer}


__all__ = [
    "build_shop_url",
    "resolve_shop_offer",
    "shop_cta_from_license",
]
