"""订单交付形态判据 — 装机履约与托管履约共用单一事实源。

``delivery == "hosted"`` 或 SKU 含独立 ``hosted`` token → 归 ``tenant_fulfill_watch``；
其余现网 SKU（chatx-entry/team/flagship、lingox-*）→ 归 ``fulfill_chatx_watch`` 签 license。
两条守护必须用本模块同一判据，否则会双发。
"""
from __future__ import annotations

from typing import Any, Dict


def is_hosted_delivery(order: Dict[str, Any]) -> bool:
    """显式托管交付标（与 SKU 命名解耦的首选信号）。"""
    return str((order or {}).get("delivery") or "").strip().lower() == "hosted"


def is_hosted_order(order: Dict[str, Any]) -> bool:
    """该订单是否走托管开通（保守：必须带显式托管信号，绝不匹配现有装机 SKU）。"""
    if is_hosted_delivery(order):
        return True
    sku = str((order or {}).get("sku_id") or "").strip().lower()
    return sku.endswith("-hosted") or "hosted" in sku.split("-")
