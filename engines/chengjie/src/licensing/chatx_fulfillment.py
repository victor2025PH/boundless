"""chengjie 系产品（chatx/lingox）SKU → license 签发 payload 权威映射（履约用）。

背景（Sprint3）：`platform/licensing/sku_registry.json` 与 `products/zhiliao/product.yaml`
只有价格 + note 文案，**没有** license payload 字段（见 platform/licensing/ledger/README §6：
chengjie payload 里 product_id/sku_id 一律 null）。自动履约需要一份把「三档 note」固化为
可签发 payload 的权威表——本模块即此表。

融合实例 P4（2026-07）：智聊/通译合并为单实例后，lingox（通译 LingoX）订单也在
本引擎履约——``LINGOX_SKU_SPECS`` 入表，同一守护（fulfill_chatx_watch.py）双产品线通吃。
lingox 档位语义＝**翻译线**：plan 一律 basic（translation_suite 档），产品差异走
seats/channels/显式 features（feature_gate 规则 3：license.features 可越档单点授予）——
**绝不**发 pro（否则与同价 chatx-team 无差异，且白送 lingox 从未卖过的 ai_autosend）。

只映射【当前 license 真正强制的维度】：
  - ``plan``     授权档（community/basic/pro/flagship）
  - ``seats``    最大坐席席位（seat_exceeded gate 强制；3/10/50 来自 note，无歧义）
  - ``channels`` 允许渠道（channel_allowed gate 强制）
  - ``features`` 显式功能位（feature_gate 强制；lingox 用它拿 analytics 漏斗看板）
  - 有效期天数 → ``exp``

「人工接管 / 数据看板 / AI 自动成交」等 note 卖点当前**未**在 ``gate.feature_allowed`` 接线，
故不臆造 features（留 override 口子，待其 gating 落地后再填），避免签发出不被强制的空 features。

``MANUAL_SKUS``＝已知但**不可自动签发**的 SKU（如 lingox-charpack 字符包：加购型用量包，
字符计量尚未在 license 强制——自动发 plan license 要么变相永久 basic、要么覆掉客户已有
订阅档，都错；转人工经 ``manual_followup_orders`` 在守护日志点名，不静默）。

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

# lingox（通译 LingoX，product_id=tongyi）三 SKU 权威映射（业务可调）。
# 定价锚（products/tongyi/product.yaml，2026-07-18 决议）：team $99/mo、pro $198/mo、
# charpack $59 一次性。座席取位：team=5（chatx-entry 3 与 chatx-team 10 之间，半价对半档）、
# pro=15（翻译专精线以「更多坐席」而非「更多功能」区分于同价 chatx-team）。
# 渠道：产品定位「多平台文字+语音双向」→ 全渠道。
# features：includes 里「漏斗看板/客户 journey/置信度看板」落在 analytics 功能位
# （basic 档默认无 → 显式授予，feature_gate 规则 3）。
LINGOX_SKU_SPECS: Dict[str, Dict[str, Any]] = {
    "lingox-team": {
        "plan": "basic", "seats": 5, "channels": list(ALL_CHANNELS),
        "features": {"analytics": True}, "product_id": "tongyi",
        "note": "多坐席/客户 journey/漏斗看板",
    },
    "lingox-pro": {
        "plan": "basic", "seats": 15, "channels": list(ALL_CHANNELS),
        "features": {"analytics": True}, "product_id": "tongyi",
        "note": "不限字符/多模态翻译/置信度·引擎健康",
    },
}

# 已知但不可自动签发（转人工；理由见模块 docstring）。
MANUAL_SKUS = frozenset({"lingox-charpack"})

# 全部可签发 SKU（chatx + lingox；MANUAL_SKUS 刻意不在内）
_ALL_SKU_SPECS: Dict[str, Dict[str, Any]] = {**CHATX_SKU_SPECS, **LINGOX_SKU_SPECS}

# 月付默认有效期：30 天权益 + 2 天缓冲（宽限另由 grace_days 管，签发时补默认）
DEFAULT_PERIOD_DAYS = 32

# 订阅周期 → 授权天数（对齐 avatarhub/fulfill_orders.py 的 PERIOD_DAYS 口径，留缓冲）
PERIOD_DAYS = {"monthly": 32, "annual": 366}


def sku_spec(sku_id: str) -> Dict[str, Any]:
    """返回某 chatx/lingox SKU 的权威 spec；未知/仅人工 SKU → ValueError。"""
    spec = _ALL_SKU_SPECS.get(str(sku_id or "").strip())
    if spec is None:
        raise ValueError(
            f"未知或不可自动签发的 SKU: {sku_id!r}"
            f"（支持: {sorted(_ALL_SKU_SPECS)}；仅人工: {sorted(MANUAL_SKUS)}）")
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
    - ``features`` / ``channels``：可覆盖 spec 默认（业务定制；features 与 spec
      默认位合并，同 key 以调用方为准）。
    额外写入 ``sku_id`` / ``product_id`` 供台账按产品/SKU 归集（填补 ledger §6 缺口）；
    product_id 随 spec（chatx=zhiliao / lingox=tongyi），供合并实例按产品线出品牌/报表。
    """
    spec = sku_spec(sku_id)
    now_ts = int(now if now is not None else time.time())
    d = DEFAULT_PERIOD_DAYS if days is None else int(days)
    merged_features = dict(spec.get("features") or {})
    merged_features.update(features or {})
    payload: Dict[str, Any] = {
        "sub": str(customer or ""),
        "plan": str(spec["plan"]),
        "seats": int(spec["seats"]),
        "channels": list(channels if channels is not None else spec["channels"]),
        "features": merged_features,
        "sku_id": str(sku_id),
        "product_id": str(spec.get("product_id") or "zhiliao"),
    }
    if d > 0:
        payload["exp"] = now_ts + d * 86400
    if order_id:
        payload["lic_id"] = f"{sku_id}-{order_id}"
    return payload


# ── 履约守护纯逻辑（Sprint4；HTTP/签名/state 由 scripts/fulfill_chatx_watch.py 薄壳注入）──
# 与 avatarhub/fulfill_orders.py 同构，但把「订单→是否可履约→签发 payload」抽成纯函数以便单测。

def is_chatx_order(order: Dict[str, Any]) -> bool:
    """该 website 订单是否属于 chatx（zhiliao）。sku_id 前缀优先，product_id 兜底。"""
    sku = str((order or {}).get("sku_id") or "")
    if sku.startswith("chatx"):
        return True
    return str((order or {}).get("product_id") or "") == "zhiliao"


def is_lingox_order(order: Dict[str, Any]) -> bool:
    """该 website 订单是否属于 lingox（tongyi）。sku_id 前缀优先，product_id 兜底。"""
    sku = str((order or {}).get("sku_id") or "")
    if sku.startswith("lingox"):
        return True
    return str((order or {}).get("product_id") or "") == "tongyi"


def is_chengjie_order(order: Dict[str, Any]) -> bool:
    """本引擎（合并实例）承接的订单：chatx 或 lingox。"""
    return is_chatx_order(order) or is_lingox_order(order)


def fulfillment_payload_for_order(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一笔 paid chatx/lingox 订单映射为可签发 payload；无法自动映射 → None（转人工跟进）。

    无法映射的情形：非本引擎订单、老单 sku_id 缺失/未知（Sprint3 前的历史单）、
    仅人工 SKU（MANUAL_SKUS，如 lingox-charpack 字符加购包）。
    """
    if not is_chengjie_order(order):
        return None
    sku = str((order or {}).get("sku_id") or "").strip()
    if sku not in _ALL_SKU_SPECS:
        return None
    days = PERIOD_DAYS.get(str((order or {}).get("period") or "").lower(), DEFAULT_PERIOD_DAYS)
    return build_issue_payload(
        sku,
        customer=str((order or {}).get("contact") or ""),
        order_id=str((order or {}).get("id") or ""),
        days=days,
    )


def select_fulfillable(
    orders: List[Dict[str, Any]],
    done_ids: Optional[set] = None,
) -> List[tuple]:
    """从 paid 订单列表挑出可自动履约的 chatx 单，返回 [(order, issue_payload), ...]。

    跳过：无 id / 已处理(done) / 已回填 code / 非 chatx / 无法映射（转人工）。幂等安全。
    """
    done = done_ids or set()
    out: List[tuple] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "")
        if not oid or oid in done or (o or {}).get("code"):
            continue
        payload = fulfillment_payload_for_order(o)
        if payload is not None:
            out.append((o, payload))
    return out


def manual_followup_orders(
    orders: List[Dict[str, Any]],
    done_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """本引擎的 paid 订单里**需要人工跟进**的（不可自动签发）——守护日志点名用。

    命中：本引擎订单（chatx/lingox）且未处理未回填，但 fulfillment_payload_for_order
    映射不出 payload（仅人工 SKU / 老单缺 sku / 未知 sku）。lingox-charpack 入表后属
    「已知必人工」，静默跳过会漏单，必须可见。
    """
    done = done_ids or set()
    out: List[Dict[str, Any]] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "")
        if not oid or oid in done or (o or {}).get("code"):
            continue
        if not is_chengjie_order(o):
            continue
        if fulfillment_payload_for_order(o) is None:
            out.append(o)
    return out
