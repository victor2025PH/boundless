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

``TOPUP_SKU_CHARS``＝加购型用量包 SKU → 字符量（P4c）：不发 plan license（会覆掉客户
订阅档），改签 **topup 凭证**（``topup_voucher.issue_topup_voucher``，绑定 sub=contact），
回填 order.code 走与授权码同一交付通道；客户在会员中心粘贴兑换。
``MANUAL_SKUS``＝已知但**不可自动履约**的 SKU（当前为空；机制保留——新 SKU 未定映射前
先入此表，转人工经 ``manual_followup_orders`` 在守护日志点名，不静默）。

安全：本模块**只产 payload，绝不签名**。Ed25519 私钥永不入库/上服务器——签名在厂商机
``scripts/fulfill_chatx.py`` 经 ``license_manager.issue_license(payload, private_hex)`` 完成。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# chatx 全渠道（team/flagship「全平台」）
# 新增渠道必须同步进来，否则 `channel_allowed` 对未知渠道是**失败关闭**的：
# 客户买了「全渠道」却用不了新平台，试用更是连评估机会都没有（见 TRIAL_SPEC 注释）。
# 不必担心「白送存量客户」——license 是签名产物，签发时就把 channels 列表**冻进
# payload** 了，改这里只影响此后新签发的授权与试用，存量客户的权益不受影响。
ALL_CHANNELS: List[str] = ["telegram", "line", "whatsapp", "messenger", "web",
                           "discord"]

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
# included_chars_monthly（P6 首单演练补，2026-07-24）：通译是字符计量产品线——
# 订阅授权必须带字符额度，否则激活即「不限量」＝计量/水位提醒失效 + charpack
# 凭证被 unlimited 护栏拒兑（加量包闭环断裂）。口径：
#   - team：官网未承诺具体字符数 → 3_000_000/月（≈2 个 charpack 的量，$99 订阅
#     对 $59×2 单买有折让感；商业数字可调，改这里即全链生效）；
#   - pro：官网明示「不限字符」→ 0（=不限；charpack 对 pro 拒兑是正确语义）。
# 签发时按授权月数放大（annual ×12），见 build_issue_payload。
LINGOX_SKU_SPECS: Dict[str, Dict[str, Any]] = {
    "lingox-team": {
        "plan": "basic", "seats": 5, "channels": list(ALL_CHANNELS),
        "features": {"analytics": True}, "product_id": "tongyi",
        "included_chars_monthly": 3_000_000,
        "note": "多坐席/客户 journey/漏斗看板",
    },
    "lingox-pro": {
        "plan": "basic", "seats": 15, "channels": list(ALL_CHANNELS),
        "features": {"analytics": True}, "product_id": "tongyi",
        "included_chars_monthly": 0,
        "note": "不限字符/多模态翻译/置信度·引擎健康",
    },
}

# 加购型用量包 → 字符量（topup 凭证自动履约；量取自 products/tongyi/product.yaml：
# lingox-charpack $59 / 150 万字符 one-time）。
TOPUP_SKU_CHARS: Dict[str, int] = {"lingox-charpack": 1_500_000}

# 已知但不可自动履约（转人工；理由见模块 docstring）。charpack 已迁 TOPUP（P4c）。
MANUAL_SKUS = frozenset()

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
    # 字符计量产品线（P6）：spec 月度额度 × 授权月数 → payload.included_chars。
    # 月付 32 天=1 个月、年付 366 天=12 个月（用量表按 lic_id 记账，月付续费
    # =新单新 lic_id 额度自然重置；年付一个池）。0/缺省 = 不限量，不写字段。
    monthly = int(spec.get("included_chars_monthly") or 0)
    if monthly > 0:
        months = 12 if d >= 360 else max(1, d // 28)
        payload["included_chars"] = monthly * months
    if d > 0:
        payload["exp"] = now_ts + d * 86400
    if order_id:
        payload["lic_id"] = f"{sku_id}-{order_id}"
    return payload


# ── 注册试用（P2）：官网 claim → 厂商机签发 → 客户端 activate ─────────────────
#
# 试用不是一笔 SKU 销售，但它的 payload 口径同样必须只有一个来源，否则「试用到底给什么」
# 会散落在脚本里各写一遍。规格集中在此：
#
#  · plan=pro —— 试用要展示的是核心价值（翻译套件 + AI 自动发 + 知识库）。给 basic
#    会让评估者看不到 AI 自动发；给 flagship 则把陪伴/RPA 这类溢价能力也送出去，
#    转化时降级反而难受。
#  · channels=全渠道 —— 评估期本来就要挨个平台试，被渠道卡住等于让人无法评估。
#  · grace_days=0 —— **必须显式写 0**。默认宽限 7 天，而 grace 状态同样算 licensed
#    （见 LicenseStatus.licensed），不写就把「7 天试用」变成实际 14 天。
#  · machine=<机器指纹> —— 绑机，防「一份试用发给一群人」。
TRIAL_SPEC: Dict[str, Any] = {
    "plan": "pro",
    "seats": 2,
    "channels": list(ALL_CHANNELS),
    "days": 7,
    "included_chars": 25_000,
    "product_id": "zhiliao",
}

#: 加客服送额度的默认赠量（与官网 TRIAL_GIFT_CHARS 同口径，此处为厂商机兜底默认）
TRIAL_GIFT_CHARS = 100_000


def build_trial_payload(
    *,
    customer: str,
    machine: str,
    claim_id: str = "",
    days: Optional[int] = None,
    chars: Optional[int] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """把一条官网试用领取（claim）映射为可直接签发的 payload。

    ``machine`` 为客户端机器指纹（``XXXX-XXXX-XXXX-XXXX``）：写进 payload 后由
    `license_manager` 在本机校验，绑到别的机器上会判 invalid。空 machine 直接抛——
    不绑机的「试用」等于可无限转发，这条链就白做了。
    """
    m = str(machine or "").strip().upper()
    if not m:
        raise ValueError("试用授权必须绑定机器指纹（machine 不能为空）")
    now_ts = int(now if now is not None else time.time())
    d = int(TRIAL_SPEC["days"] if days is None else days)
    c = int(TRIAL_SPEC["included_chars"] if chars is None else chars)
    # lic_id 取指纹短码：一机一份，额度表按它记账；claim_id 另存于 note 便于对账。
    fp8 = m.replace("-", "")[:8] or "unknown"
    payload: Dict[str, Any] = {
        "sub": str(customer or "")[:120],
        "plan": str(TRIAL_SPEC["plan"]),
        "seats": int(TRIAL_SPEC["seats"]),
        "channels": list(TRIAL_SPEC["channels"]),
        "features": {},
        "product_id": str(TRIAL_SPEC["product_id"]),
        "included_chars": max(0, c),
        "trial": True,
        "machine": m,
        "grace_days": 0,
        "lic_id": f"trial-{fp8}",
    }
    if claim_id:
        payload["claim_id"] = str(claim_id)[:64]
    if d > 0:
        payload["exp"] = now_ts + d * 86400
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


def topup_voucher_args_for_order(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一笔 paid 加购包订单映射为 ``issue_topup_voucher`` 的关键字参数；不可映射 → None。

    绑定用 ``customer=contact``（订单没有 lic_id；授权 payload.sub=contact，同一客户
    名下可兑）。contact 缺失 → None（没有绑定面=谁捡到谁兑，宁转人工不裸发）。
    """
    sku = str((order or {}).get("sku_id") or "").strip()
    chars = TOPUP_SKU_CHARS.get(sku)
    contact = str((order or {}).get("contact") or "").strip()
    oid = str((order or {}).get("id") or "").strip()
    if not chars or not contact or not oid:
        return None
    return {"chars": int(chars), "ref": oid, "customer": contact, "note": sku}


def select_topup_fulfillable(
    orders: List[Dict[str, Any]],
    done_ids: Optional[set] = None,
) -> List[tuple]:
    """从 paid 订单挑出可自动签**加量凭证**的单，返回 [(order, voucher_args), ...]。

    与 ``select_fulfillable`` 同骨架（跳过无 id/已处理/已回填 code），幂等安全。
    """
    done = done_ids or set()
    out: List[tuple] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "")
        if not oid or oid in done or (o or {}).get("code"):
            continue
        args = topup_voucher_args_for_order(o)
        if args is not None:
            out.append((o, args))
    return out


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

    命中：本引擎订单（chatx/lingox）且未处理未回填，但**license 与 topup 凭证两条
    自动通道都映射不出**（老单缺 sku / 未知 sku / charpack 缺 contact 没绑定面）。
    静默跳过会漏单，必须可见。
    """
    done = done_ids or set()
    out: List[Dict[str, Any]] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "")
        if not oid or oid in done or (o or {}).get("code"):
            continue
        if not is_chengjie_order(o):
            continue
        if (fulfillment_payload_for_order(o) is None
                and topup_voucher_args_for_order(o) is None):
            out.append(o)
    return out
