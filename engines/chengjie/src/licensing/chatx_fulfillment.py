"""chatx SKU → chengjie license 签发 payload 权威映射（履约用）。

背景（Sprint3）：`platform/licensing/sku_registry.json` 与 `products/zhiliao/product.yaml`
只有价格 + note 文案，**没有** license payload 字段（见 platform/licensing/ledger/README §6：
chengjie payload 里 product_id/sku_id 一律 null）。自动履约需要一份把「三档 note」固化为
可签发 payload 的权威表——本模块即此表。

只映射【当前 license 真正强制的维度】：
  - ``plan``     授权档（community/basic/pro/flagship）
  - ``seats``    最大坐席席位（seat_exceeded gate 强制；3/10/50 来自 note，无歧义）
  - ``channels`` 允许渠道（channel_allowed gate 强制）
  - 有效期天数 → ``exp``

「人工接管 / 数据看板 / AI 自动成交」等 note 卖点当前**未**在 ``gate.feature_allowed`` 接线，
故不臆造 features（留 override 口子，待其 gating 落地后再填），避免签发出不被强制的空 features。

安全：本模块**只产 payload，绝不签名**。Ed25519 私钥永不入库/上服务器——签名在厂商机
``scripts/fulfill_chatx.py`` 经 ``license_manager.issue_license(payload, private_hex)`` 完成。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# chatx 全渠道（team/flagship「全平台」）
ALL_CHANNELS: List[str] = ["telegram", "line", "whatsapp", "messenger", "web"]

# chatx 三档权威映射（业务可调）。entry「1 平台」默认主渠道 telegram（可经 channels 覆盖）。
CHATX_SKU_SPECS: Dict[str, Dict[str, Any]] = {
    "chatx-entry": {
        "plan": "basic", "seats": 3, "channels": ["telegram"],
        "note": "3 账号/AI 翻译/1 平台",
    },
    "chatx-team": {
        "plan": "pro", "seats": 10, "channels": list(ALL_CHANNELS),
        "note": "10 账号/全平台/AI 自动成交",
    },
    "chatx-flagship": {
        "plan": "flagship", "seats": 50, "channels": list(ALL_CHANNELS),
        "note": "50 账号/人工接管/数据看板",
    },
}

# 月付默认有效期：30 天权益 + 2 天缓冲（宽限另由 grace_days 管，签发时补默认）
DEFAULT_PERIOD_DAYS = 32


def sku_spec(sku_id: str) -> Dict[str, Any]:
    """返回某 chatx SKU 的权威 spec；非 chatx / 未知 → ValueError。"""
    spec = CHATX_SKU_SPECS.get(str(sku_id or "").strip())
    if spec is None:
        raise ValueError(
            f"未知或非 chatx SKU: {sku_id!r}（支持: {sorted(CHATX_SKU_SPECS)}）")
    return spec


def build_issue_payload(
    sku_id: str,
    *,
    customer: str,
    order_id: str = "",
    days: Optional[int] = None,
    features: Optional[Dict[str, Any]] = None,
    channels: Optional[List[str]] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """把一笔 chatx 订单映射为 ``issue_license`` 可直接签发的 payload。

    - ``customer``：客户标识（写入 payload.sub）。
    - ``order_id``：订单号 → payload.lic_id=``{sku}-{order}``（便于台账/吊销登记）。
    - ``days``：有效天数（None=DEFAULT_PERIOD_DAYS；<=0=永久，不写 exp）。
    - ``features`` / ``channels``：可覆盖 spec 默认（业务定制）。
    额外写入 ``sku_id`` / ``product_id`` 供台账按产品/SKU 归集（填补 ledger §6 缺口）。
    """
    spec = sku_spec(sku_id)
    now_ts = int(now if now is not None else time.time())
    d = DEFAULT_PERIOD_DAYS if days is None else int(days)
    payload: Dict[str, Any] = {
        "sub": str(customer or ""),
        "plan": str(spec["plan"]),
        "seats": int(spec["seats"]),
        "channels": list(channels if channels is not None else spec["channels"]),
        "features": dict(features or {}),
        "sku_id": str(sku_id),
        "product_id": "zhiliao",
    }
    if d > 0:
        payload["exp"] = now_ts + d * 86400
    if order_id:
        payload["lic_id"] = f"{sku_id}-{order_id}"
    return payload
