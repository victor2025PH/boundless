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
ALL_CHANNELS: List[str] = ["telegram", "line", "whatsapp", "messenger", "web"]

# chatx 档位权威映射（业务可调）。
#
# ── 2026-08-19 Token 定价改版 ────────────────────────────────────────────────
# 在售：personal（39/月）/ team-seat（49/坐席/月，按坐席签发）/ flagship（598/月）。
# 停售但保留可履约：entry/team（历史订单/续费单仍能自动签发，官网已不再挂牌）。
# 新增维度：
#   · included_tokens_monthly —— 订阅每月含 Token（进 payload；运行时由
#     token_ledger.ensure_monthly_tokens 按自然月幂等入账到**客户钱包**）。
#     per_seat SKU 签发时按订单 seats 放大（build_issue_payload）。
#   · per_seat —— 按坐席计价档：payload.seats = 订单 seats（clamp [min,50]），
#     included_tokens_monthly 同步 ×seats。
#   · **新 SKU 刻意不带 included_chars_monthly** —— 字符不限量 = 标准翻译免费
#     （2026-08-19 决议）的引擎侧实现：quota_store 对 included=0 恒放行，零新代码。
#     专业翻译/语音/AI 回复的计量走 Token 钱包（token_ledger），与字符池正交。
CHATX_SKU_SPECS: Dict[str, Dict[str, Any]] = {
    "chatx-personal": {
        "plan": "basic", "seats": 1, "channels": list(ALL_CHANNELS),
        "included_tokens_monthly": 30_000,
        "note": "1 坐席/3 账号/全平台/30,000 Token 每月",
    },
    "chatx-team-seat": {
        "plan": "pro", "seats": 2, "channels": list(ALL_CHANNELS),
        "per_seat": {"min": 2, "max": 50},
        "included_tokens_monthly": 50_000,   # 每坐席；签发按 seats 放大
        "note": "每坐席 5 账号+50,000 Token 入池/权限审计",
    },
    "chatx-entry": {
        "plan": "basic", "seats": 3, "channels": ["telegram"],
        "note": "【停售 2026-08-19】3 账号/AI 翻译/1 平台（历史续费仍可签发）",
    },
    "chatx-team": {
        "plan": "pro", "seats": 10, "channels": list(ALL_CHANNELS),
        "note": "【停售 2026-08-19】10 账号/全平台（历史续费仍可签发）",
    },
    "chatx-flagship": {
        "plan": "flagship", "seats": 50, "channels": list(ALL_CHANNELS),
        "included_tokens_monthly": 0,   # 本地模型不限量；云增值按钱包（不发月含量）
        "note": "50 账号/人工接管/数据看板/本地模型 Token 不限",
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
    # 2026-08-19 在售：翻译工作台（29/坐席/月，纯翻译团队）。不带 included_chars_monthly
    # = 字符不限（标准翻译免费）；专业翻译按 Token 钱包，无订阅月含量。
    "lingox-workbench": {
        "plan": "basic", "seats": 1, "channels": list(ALL_CHANNELS),
        "per_seat": {"min": 1, "max": 50},
        "features": {"analytics": True}, "product_id": "tongyi",
        "included_tokens_monthly": 0,
        "note": "多坐席收件箱/客户 journey/漏斗/术语锁定（标准翻译免费不限量）",
    },
    # ── 停售 2026-08-19（历史续费仍可签发；新客走 workbench + Token）──
    "lingox-team": {
        "plan": "basic", "seats": 5, "channels": list(ALL_CHANNELS),
        "features": {"analytics": True}, "product_id": "tongyi",
        "included_chars_monthly": 3_000_000,
        "note": "【停售】多坐席/客户 journey/漏斗看板",
    },
    "lingox-pro": {
        "plan": "basic", "seats": 15, "channels": list(ALL_CHANNELS),
        "features": {"analytics": True}, "product_id": "tongyi",
        "included_chars_monthly": 0,
        "note": "【停售】不限字符/多模态翻译/置信度·引擎健康",
    },
}

# 加购型用量包 → 字符量（topup 凭证自动履约；lingox-charpack 停售 2026-08-19，
# 保留映射供历史订单兑付——无老客户但防手工补单）。
TOPUP_SKU_CHARS: Dict[str, int] = {"lingox-charpack": 1_500_000}

# Token 包 → Token 量（2026-08-19；与 products/zhiliao/product.yaml + 官网
# chatx-pricing.ts::TOKEN_PACKS 同批改）。履约同走 topup 凭证通道（payload 带
# tokens 而非 chars），兑换入 token_ledger 客户钱包（12 个月有效在账本层实施）。
TOPUP_SKU_TOKENS: Dict[str, int] = {
    "token-pack-s": 10_000,
    "token-pack-m": 60_000,
    "token-pack-l": 300_000,
    "token-pack-xl": 1_000_000,
}

# ── Token 充值档（2026-08-21 充值唯一化；实施50）─────────────────────────────
# 与官网 website/lib/chatx-pricing.ts::RECHARGE_TIERS / NEWBIE_PACK **同批改**
# （门禁 tests/test_recharge_fulfillment.py::test_recharge_specs_match_website_source
#  在两仓同机时交叉钉住；独立 CI 自动跳过）。
#
# 履约形态＝topup 凭证带 **chars**（1 Token = 100 字符，token_ledger.CHARS_PER_TOKEN_LEGACY
# 同口径）：生产扣减当前只强制字符池（Token 钱包默认关、消费点未接线），发 tokens 等于
# 给客户一笔「现在花不动」的余额；发 chars 立刻可用，钱包上线后按官网公示 1:1 并账。
# 刻意**不双发** chars+tokens（并账时会双份到账）。
# 有效期（12/24 个月）当前在字符池层无强制（license_char_topup 无过期列）——
# 结果=客户只多不少，与「退款回收加赠」同属钱包批次上线后的收紧项。
RECHARGE_TOKENS_PER_USD = 1_500
RECHARGE_CHARS_PER_TOKEN = 100  # 并账口径 = token_ledger.CHARS_PER_TOKEN_LEGACY

#: 普通充值档：usd=挂牌价；first_bonus_pct=首笔充值一次性加赠（每人一次，
#: 判定=同 contact_core / 同机器指纹的历史已付普通充值单，见 is_first_recharge）。
RECHARGE_SKU_SPECS: Dict[str, Dict[str, int]] = {
    "recharge-50":    {"usd": 50,    "first_bonus_pct": 0},
    "recharge-100":   {"usd": 100,   "first_bonus_pct": 5},
    "recharge-200":   {"usd": 200,   "first_bonus_pct": 10},
    "recharge-500":   {"usd": 500,   "first_bonus_pct": 20},
    "recharge-1000":  {"usd": 1000,  "first_bonus_pct": 30},
    "recharge-5000":  {"usd": 5000,  "first_bonus_pct": 35},
    "recharge-10000": {"usd": 10000, "first_bonus_pct": 40},
}

#: 新人首充大礼包：6U = 18,000 Token（2 倍率）；注册 72h 内、每账号一次、
#: 刻意不占用首充加赠资格（官网口径）。注册锚=试用台账 claim.created_at。
NEWBIE_SKU = "recharge-newbie-6"
NEWBIE_TOKENS = 18_000
NEWBIE_WINDOW_HOURS = 72

#: VIP 累充等级（实施50 P2）：**复充**按客户累计已付充值（严格早于本单的
#: paid/activated 充值单，含新人包，按 SKU 挂牌价累计）阶梯加赠——
#: 「复充回归基准价」自此升级为「复充按 VIP 等级加赠」。首充加赠（最高 +40%，
#: 一次性）不与 VIP 叠加：首单走首充档，之后每一单走 VIP 档。
#: 比例刻意克制（永久性加赠 ≤ +8% vs 一次性首充 +40%）保毛利；
#: 与官网 chatx-pricing.ts::VIP_REPEAT_BONUS_TIERS 同批改（跨仓门禁钉住）。
VIP_REPEAT_BONUS_TIERS: List[tuple] = [(500, 3), (2000, 5), (10000, 8)]

# 已知但不可自动履约（转人工；理由见模块 docstring）。charpack 已迁 TOPUP（P4c）。
MANUAL_SKUS = frozenset()

# 全部可签发 SKU（chatx + lingox；MANUAL_SKUS 刻意不在内）
_ALL_SKU_SPECS: Dict[str, Dict[str, Any]] = {**CHATX_SKU_SPECS, **LINGOX_SKU_SPECS}

# 月付默认有效期：30 天权益 + 2 天缓冲（宽限另由 grace_days 管，签发时补默认）
DEFAULT_PERIOD_DAYS = 32

# 订阅周期 → 授权天数（对齐 avatarhub/fulfill_orders.py 的 PERIOD_DAYS 口径，留缓冲）
PERIOD_DAYS = {"monthly": 32, "quarterly": 92, "annual": 366}


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
    seats: Optional[int] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """把一笔 chatx 订单映射为 ``issue_license`` 可直接签发的 payload。

    - ``customer``：客户标识（写入 payload.sub）。
    - ``order_id``：订单号 → payload.lic_id=``{sku}-{order}``（便于台账/吊销登记）。
    - ``days``：有效天数（None=DEFAULT_PERIOD_DAYS；<=0=永久，不写 exp）。
    - ``features`` / ``channels``：可覆盖 spec 默认（业务定制；features 与 spec
      默认位合并，同 key 以调用方为准）。
    - ``seats``：**按坐席档**（spec 带 per_seat）的订单席数——clamp 到 [min,max]，
      缺省按下限签发；included_tokens_monthly 同步 ×seats。非按坐席档忽略此参。
    额外写入 ``sku_id`` / ``product_id`` 供台账按产品/SKU 归集（填补 ledger §6 缺口）；
    product_id 随 spec（chatx=zhiliao / lingox=tongyi），供合并实例按产品线出品牌/报表。
    """
    spec = sku_spec(sku_id)
    now_ts = int(now if now is not None else time.time())
    d = DEFAULT_PERIOD_DAYS if days is None else int(days)
    merged_features = dict(spec.get("features") or {})
    merged_features.update(features or {})
    # 按坐席档：席数=订单值 clamp [min,max]；固定档：spec.seats。
    per_seat = spec.get("per_seat") or None
    if per_seat:
        want = int(seats or 0) or int(per_seat["min"])
        eff_seats = max(int(per_seat["min"]), min(int(per_seat["max"]), want))
    else:
        eff_seats = int(spec["seats"])
    payload: Dict[str, Any] = {
        "sub": str(customer or ""),
        "plan": str(spec["plan"]),
        "seats": eff_seats,
        "channels": list(channels if channels is not None else spec["channels"]),
        "features": merged_features,
        "sku_id": str(sku_id),
        "product_id": str(spec.get("product_id") or "zhiliao"),
    }
    # 字符计量产品线（P6，2026-08-19 起仅停售档还带）：spec 月度额度 × 授权月数 →
    # payload.included_chars。月付 32 天=1 个月、年付 366 天=12 个月。
    # 0/缺省 = 不限量，不写字段（新档全部如此 = 标准翻译免费）。
    monthly = int(spec.get("included_chars_monthly") or 0)
    if monthly > 0:
        months = 12 if d >= 360 else max(1, d // 28)
        payload["included_chars"] = monthly * months
    # Token 月含量（2026-08-19）：写**每月值**（不按月数放大——运行时按自然月
    # 幂等入账，当月有效不结转；年付授权自动覆盖 12 次月度入账）。按坐席档 ×seats。
    tokens_monthly = int(spec.get("included_tokens_monthly") or 0)
    if tokens_monthly > 0:
        payload["included_tokens_monthly"] = tokens_monthly * (
            eff_seats if per_seat else 1)
    if d > 0:
        payload["exp"] = now_ts + d * 86400
    if order_id:
        payload["lic_id"] = f"{sku_id}-{order_id}"
    return payload


# ── 注册免费档（P2 → 2026-08-11 升级为「注册领 100 万字符」）─────────────────
#
# 官网 claim → 厂商机签发 → 客户端 activate。免费档不是一笔 SKU 销售，但它的 payload
# 口径同样必须只有一个来源，否则「免费档到底给什么」会散落在脚本里各写一遍。规格集中在此：
#
#  · plan=pro —— 免费档要展示的是核心价值（翻译套件 + AI 自动发 + 知识库）。给 basic
#    会让评估者看不到 AI 自动发；给 flagship 则把陪伴/RPA 这类溢价能力也送出去，
#    转化时降级反而难受。转化杠杆是**字符量**（用完即止），不锁功能。
#  · channels=全渠道 —— 评估期本来就要挨个平台试，被渠道卡住等于让人无法评估。
#  · days=0 —— **无期限**（不写 exp）。2026-08-11 运营拍板：免费档从「7 天 · 2.5 万」
#    升级为「100 万字符 · 用完即止」——100 万本身够用很久，时间窗反而稀释价值感；
#    用尽后由额度闸门硬拦（quota_store，签名授权 included>0 天然强制），
#    出路＝邀请好友 / 联系客服申请 / 购买。
#  · grace_days=0 —— **必须显式写 0**。默认宽限 7 天，而 grace 状态同样算 licensed
#    （见 LicenseStatus.licensed）；无期限档虽然用不到 exp，保留 0 防止将来有人
#    重新设 days 时把「N 天」悄悄变成「N+7 天」。
#  · machine=<机器指纹> —— 绑机，防「一份免费档发给一群人」。
#
# 存量升级：已按旧规格（25_000 / 7 天）签出的试用授权 payload 已固化，**不会自动变大**
# ——由 fulfill_trial.py 的 upgrade pass 对 issued 台账重签（同 lic_id，用量累计不清零），
# 客户端经会员页/轮询按「license 指纹变化」重新落盘激活。
TRIAL_SPEC: Dict[str, Any] = {
    "plan": "pro",
    "seats": 2,
    "channels": list(ALL_CHANNELS),
    "days": 0,
    "included_chars": 1_000_000,
    "product_id": "zhiliao",
}

#: 加客服送额度的默认赠量（与官网 TRIAL_GIFT_CHARS 同口径，此处为厂商机兜底默认）。
#: 客服核销时可按用户情况在控制台改量（bind_chars 优先，见 gift_chars_for）——
#: 「联系客服申请更多字符」的弹性给量走的就是这条链。
TRIAL_GIFT_CHARS = 100_000

#: 邀请裂变（P2 referral）：被邀请人注册成功的见面礼 / 邀请人在被邀请人真实消耗
#: 达标后的奖励。厂商机兜底默认——官网侧同名 env 可覆盖（两边对齐）。
REFERRAL_INVITEE_CHARS = 100_000
REFERRAL_INVITER_CHARS = 100_000
#: 邀请人奖励的达标门：被邀请人真实消耗 ≥ 此数才给邀请人发奖——挡「批量注册
#: 即弃的女巫账号」，同时顺手激励邀请人帮好友把产品用起来。
REFERRAL_QUALIFY_CHARS = 10_000

#: 加码发奖（里程碑/首充返利，实施50 P1）单笔上限——**厂商机侧的私钥护栏**：
#: 金额由官网 bonus_due 计算，但签名端绝不能是官网说多少签多少的橡皮章。
#: 合法上限=返利封顶 100U 等值（100×1,500 Token×100 字符/Token=15,000,000 字符）；
#: 里程碑最大档 1,000,000 字符远在其内。超限条目跳过并大声点名（宁可漏发不错发）。
REFERRAL_BONUS_MAX_CHARS = 15_000_000


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
    """该 website 订单是否属于 chatx（zhiliao）。sku_id 前缀优先，product_id 兜底。

    Token 包（token-pack-*）与充值档（recharge-*）挂 zhiliao 名下
    （跨 ChatX/LingoX 通用钱包，registry 归属）。
    """
    sku = str((order or {}).get("sku_id") or "")
    if sku.startswith("chatx") or sku.startswith("token-pack") or sku.startswith("recharge"):
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
    仅人工 SKU（MANUAL_SKUS，如 lingox-charpack 字符加购包）、
    **托管交付单**（``delivery==hosted`` / hosted SKU → 归 tenant_fulfill_watch，防双发）。
    """
    from src.licensing.order_delivery import is_hosted_order

    if is_hosted_order(order):
        return None
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
        # 按坐席档（team-seat/workbench）：官网订单 2026-08-19 起带 seats 字段；
        # 缺失/历史单按档位下限签发（build_issue_payload 内 clamp）。
        seats=int((order or {}).get("seats") or 0),
    )


def topup_voucher_args_for_order(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一笔 paid 加购包订单映射为 ``issue_topup_voucher`` 的关键字参数；不可映射 → None。

    两类加购走同一凭证轨道（typ=topup，客户在会员中心同一个兑换框粘贴）：
      · 字符包（TOPUP_SKU_CHARS，停售存量）→ args 带 ``chars``；
      · Token 包（TOPUP_SKU_TOKENS，2026-08-19 在售）→ args 带 ``tokens``。
    绑定用 ``customer=contact``（订单没有 lic_id；授权 payload.sub=contact，同一客户
    名下可兑）。contact 缺失 → None（没有绑定面=谁捡到谁兑，宁转人工不裸发）。
    托管交付单不签装机加购包（归租户实例内计量）。
    """
    from src.licensing.order_delivery import is_hosted_order

    if is_hosted_order(order):
        return None
    sku = str((order or {}).get("sku_id") or "").strip()
    contact = str((order or {}).get("contact") or "").strip()
    oid = str((order or {}).get("id") or "").strip()
    if not contact or not oid:
        return None
    chars = TOPUP_SKU_CHARS.get(sku)
    if chars:
        return {"chars": int(chars), "ref": oid, "customer": contact, "note": sku}
    tokens = TOPUP_SKU_TOKENS.get(sku)
    if tokens:
        return {"tokens": int(tokens), "ref": oid, "customer": contact, "note": sku}
    return None


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
    充值档（recharge-*）刻意不在本函数点名——它们由 ``plan_recharge_fulfillment``
    专责（自带 manual 队列与**具体原因**，比这里的笼统点名信息量大）。
    """
    done = done_ids or set()
    out: List[Dict[str, Any]] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "")
        if not oid or oid in done or (o or {}).get("code"):
            continue
        if not is_chengjie_order(o):
            continue
        if is_recharge_sku(str((o or {}).get("sku_id") or "")):
            continue
        if (fulfillment_payload_for_order(o) is None
                and topup_voucher_args_for_order(o) is None):
            out.append(o)
    return out


# ── 充值档履约（2026-08-21 充值唯一化；实施50）───────────────────────────────
# 纯函数：首充判定 / 新人包资格 / 凭证参数 / 履约规划。HTTP、签名、state 仍由
# fulfill_chatx_watch.py 薄壳注入。所有判定都从**官网台账真相**（订单历史 + 试用
# claim）现算，刻意不建本地「首充台账」——无状态推导自愈且可审计，厂商机 state
# 文件丢了也不会重复发加赠（历史订单还在）。

def is_recharge_sku(sku: str) -> bool:
    """是否充值档 SKU（含新人包）。"""
    s = str(sku or "").strip()
    return s in RECHARGE_SKU_SPECS or s == NEWBIE_SKU


def _parse_iso_epoch(value: Any) -> float:
    """ISO 8601（官网订单 t / 试用 created_at，含尾缀 Z）→ epoch 秒；解析失败 → 0。"""
    s = str(value or "").strip()
    if not s:
        return 0.0
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _norm_fp(value: Any) -> str:
    """机器指纹归一（比对用）：去分隔、大写。空/无内容 → 空串。"""
    s = str(value or "").strip().upper().replace("-", "").replace(" ", "")
    return s


def _order_epoch(order: Dict[str, Any]) -> float:
    """订单时间锚：优先创建时刻 t（新人 72h 窗按下单时刻算，付款确认可能晚几分钟——
    按 t 判对客户更有利且不受对账延迟影响）；缺 t 回退 paid_at。"""
    return _parse_iso_epoch((order or {}).get("t")) or _parse_iso_epoch(
        (order or {}).get("paid_at"))


def _same_person(order_a: Dict[str, Any], order_b: Dict[str, Any]) -> bool:
    """两笔订单是否同一客户：contact_core 相等（非空）或机器指纹相等（非空）。"""
    from src.licensing.topup_voucher import contact_core

    ca = contact_core(str((order_a or {}).get("contact") or ""))
    cb = contact_core(str((order_b or {}).get("contact") or ""))
    if ca and cb and ca == cb:
        return True
    fa = _norm_fp((order_a or {}).get("fingerprint"))
    fb = _norm_fp((order_b or {}).get("fingerprint"))
    return bool(fa and fb and fa == fb)


def _person_recharge_orders(
    order: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]],
    *,
    newbie: bool,
    include_all: bool = False,
) -> List[Dict[str, Any]]:
    """同一客户名下的已付充值单（paid/activated；newbie 开关选普通档或新人包，
    include_all=True 时两类都要——VIP 累充口径）。

    含 order 本身（若不在 history 里则补入）——排序定谁是「第一笔」要用。
    退款单天然不入池：watcher 只拉 paid/activated，且本函数按 status 过滤——
    官网把单改成 refunded 后，首充/新人/VIP 判定自动把它当不存在。
    """
    if include_all:
        sku_ok = is_recharge_sku
    else:
        sku_ok = ((lambda s: s == NEWBIE_SKU) if newbie
                  else (lambda s: s in RECHARGE_SKU_SPECS))
    oid = str((order or {}).get("id") or "")
    pool: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for o in list(history or []) + [order]:
        i = str((o or {}).get("id") or "")
        if not i or i in seen_ids:
            continue
        if str((o or {}).get("status") or "") not in ("paid", "activated"):
            continue
        if not sku_ok(str((o or {}).get("sku_id") or "")):
            continue
        if i != oid and not _same_person(order, o):
            continue
        seen_ids.add(i)
        pool.append(o)
    return pool


def is_first_recharge(
    order: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]],
) -> bool:
    """本单是否该客户的**首笔**普通充值（享一次性加赠）。

    判定＝同客户全部已付普通充值单按 (时间, 订单号) 排序后的第一笔是不是本单——
    确定性并列裁决：两笔同时到账也只有一笔拿加赠（无状态、可重放，不依赖处理顺序）。
    新人包（NEWBIE_SKU）刻意不入池：官网口径「新人包不占用首充资格」。
    """
    pool = _person_recharge_orders(order, history, newbie=False)
    if not pool:
        return True
    pool.sort(key=lambda o: (_order_epoch(o), str(o.get("id") or "")))
    return str(pool[0].get("id") or "") == str((order or {}).get("id") or "")


def repeat_bonus_pct(cumulative_usd: float) -> int:
    """VIP 累充等级 → 复充加赠百分比（未达最低门槛 = 0）。"""
    pct = 0
    for threshold, tier_pct in VIP_REPEAT_BONUS_TIERS:
        if cumulative_usd >= threshold:
            pct = int(tier_pct)
    return pct


def cumulative_recharge_usd_before(
    order: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]],
) -> int:
    """本单之前（严格按 (时间, 订单号) 早于本单）该客户累计已付充值（USD）。

    金额取 **SKU 挂牌价**而非订单 amount——amount 是客户端提交值，挂牌价才是
    不可伪造的口径（付款对账已保证钱真到了，但 VIP 档位按台账价累计更保守）。
    含新人包（6U 也是真金白银）。同时刻并付的单互不计入（strictly-before 语义
    与 is_first_recharge 的并列裁决一致：谁都不能靠「同秒的另一单」升档）。
    """
    self_key = (_order_epoch(order), str((order or {}).get("id") or ""))
    total = 0
    for o in _person_recharge_orders(order, history, newbie=False, include_all=True):
        if str(o.get("id") or "") == str((order or {}).get("id") or ""):
            continue
        if (_order_epoch(o), str(o.get("id") or "")) >= self_key:
            continue
        sku = str(o.get("sku_id") or "")
        if sku == NEWBIE_SKU:
            total += 6
        elif sku in RECHARGE_SKU_SPECS:
            total += int(RECHARGE_SKU_SPECS[sku]["usd"])
    return total


def _claims_for_person(
    order: Dict[str, Any],
    claims: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """试用台账里属于本单客户的 claim（指纹或 contact_core 匹配）。"""
    from src.licensing.topup_voucher import contact_core

    fp = _norm_fp((order or {}).get("fingerprint"))
    cc = contact_core(str((order or {}).get("contact") or ""))
    out: List[Dict[str, Any]] = []
    for c in claims or []:
        cfp = _norm_fp((c or {}).get("fingerprint"))
        ccc = contact_core(str((c or {}).get("contact") or ""))
        if (fp and cfp and cfp == fp) or (cc and ccc and ccc == cc):
            out.append(c)
    return out


def newbie_eligibility(
    order: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]],
    claims: Optional[List[Dict[str, Any]]],
) -> tuple:
    """新人包资格：(ok, reason)。

    - 每账号一次：同客户已付新人包单里按 (时间, 订单号) 排序的第一笔才放行
      （与 is_first_recharge 同款确定性裁决）→ 其余 ``newbie_already_claimed``；
    - 72h 窗：注册锚=该客户**最早**的试用 claim.created_at；下单时刻超窗 →
      ``newbie_window_passed``。台账里找不到 claim（先付费后装机的新客）→ 放行
      （首触即注册语义；官网下单闸另有同口径预检，见 website/lib/newbie-gate.ts）。
    - claims=None（台账拉取失败）→ 按「无 claim」放行：新人包客单价 6U，
      挡住全部新客履约比多发几单的代价大得多；每账号一次仍由订单历史强制。
    """
    pool = _person_recharge_orders(order, history, newbie=True)
    if pool:
        pool.sort(key=lambda o: (_order_epoch(o), str(o.get("id") or "")))
        if str(pool[0].get("id") or "") != str((order or {}).get("id") or ""):
            return False, "newbie_already_claimed"
    mine = _claims_for_person(order, claims)
    if mine:
        reg = min(_parse_iso_epoch(c.get("created_at")) or float("inf") for c in mine)
        ordered_at = _order_epoch(order)
        if reg != float("inf") and ordered_at > reg + NEWBIE_WINDOW_HOURS * 3600:
            return False, "newbie_window_passed"
    return True, ""


def recharge_voucher_decision(
    order: Dict[str, Any],
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """一笔 paid 充值单 → 履约决定。

    返回三态：
      {"action": "issue", "args": {chars, ref, customer, note}, "meta": {...}}
      {"action": "manual", "reason": "..."}   —— 需人工（缺绑定面/资格不符/托管单）
      {"action": "skip"}                       —— 非充值单（调用方走其它通道）
    凭证载荷=chars（tokens×100，理由见 RECHARGE_SKU_SPECS 段注释）；meta 带 Token
    口径明细供日志/审计。
    """
    from src.licensing.order_delivery import is_hosted_order

    sku = str((order or {}).get("sku_id") or "").strip()
    if not is_recharge_sku(sku):
        return {"action": "skip"}
    if is_hosted_order(order):
        return {"action": "manual", "reason": "hosted_order"}
    contact = str((order or {}).get("contact") or "").strip()
    oid = str((order or {}).get("id") or "").strip()
    if not contact or not oid:
        return {"action": "manual", "reason": "missing_contact"}

    if sku == NEWBIE_SKU:
        ok, reason = newbie_eligibility(order, history, claims)
        if not ok:
            return {"action": "manual", "reason": reason}
        tokens = NEWBIE_TOKENS
        note = f"{sku} newbie x2 tokens={tokens} (1tk={RECHARGE_CHARS_PER_TOKEN}chars)"
        meta = {"tokens": tokens, "base_tokens": tokens, "bonus_tokens": 0,
                "first_charge": False, "bonus_pct": 0}
    else:
        spec = RECHARGE_SKU_SPECS[sku]
        base = int(spec["usd"]) * RECHARGE_TOKENS_PER_USD
        first = is_first_recharge(order, history)
        vip_pct = 0
        cum_usd = 0
        if first:
            pct = int(spec["first_bonus_pct"])
            tag = f"first_charge+{pct}%" if pct else "first_charge"
        else:
            # 复充走 VIP 累充等级（实施50 P2）：按本单之前的累计已付充值定档。
            cum_usd = cumulative_recharge_usd_before(order, history)
            vip_pct = repeat_bonus_pct(cum_usd)
            pct = vip_pct
            tag = f"repeat_vip+{pct}%(cum={cum_usd}U)" if pct else "repeat"
        bonus = base * pct // 100
        tokens = base + bonus
        note = (f"{sku} {tag} tokens={tokens} (base {base} + bonus {bonus}; "
                f"1tk={RECHARGE_CHARS_PER_TOKEN}chars)")
        meta = {"tokens": tokens, "base_tokens": base, "bonus_tokens": bonus,
                "first_charge": first, "bonus_pct": pct,
                "vip_pct": vip_pct, "cum_usd": cum_usd}
    return {
        "action": "issue",
        "args": {"chars": tokens * RECHARGE_CHARS_PER_TOKEN, "ref": oid,
                 "customer": contact, "note": note},
        "meta": meta,
    }


def plan_recharge_fulfillment(
    orders: List[Dict[str, Any]],
    done_ids: Optional[set] = None,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, List[tuple]]:
    """从 paid 订单挑出充值单并给出履约计划：{"issue": [(order, args, meta)], "manual": [(order, reason)]}。

    与 select_topup_fulfillable 同款幂等跳过（无 id/已处理/已回填 code）。
    ``history`` 建议传 paid+activated 全量（首充/每账号一次判定的数据面），
    ``claims`` 传试用台账列表（新人 72h 窗）——两者缺省时判定按保守放行语义
    退化（见 newbie_eligibility / is_first_recharge docstring）。
    """
    done = done_ids or set()
    issue: List[tuple] = []
    manual: List[tuple] = []
    for o in orders or []:
        oid = str((o or {}).get("id") or "")
        if not oid or oid in done or (o or {}).get("code"):
            continue
        if not is_recharge_sku(str((o or {}).get("sku_id") or "")):
            continue
        d = recharge_voucher_decision(o, history=history, claims=claims)
        if d["action"] == "issue":
            issue.append((o, d["args"], d["meta"]))
        elif d["action"] == "manual":
            manual.append((o, d["reason"]))
    return {"issue": issue, "manual": manual}
