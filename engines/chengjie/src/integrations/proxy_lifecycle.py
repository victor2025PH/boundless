"""托管代理生命周期巡检——自动续期 / 到期回收 / 低库存预警的决策核心。

为什么必须有这一环（P2-1）：P1 只写了 ``period_end``，没有任何到期后的动作。
后果是「代理早就过期了，坐席还在用它登号」——表现为莫名其妙的连接失败，
排查成本极高；而 auto_renew 承诺了却不执行，等于说谎。

## 分工

本模块 = **决策 + 台账/池子变更**（同步、可注入、离线可测）；
发布告警 / 节流 / 定时 = ``HealthWatchdog._check_proxy_managed``（薄接线）。
返回值里的 ``alerts`` 是**已经去重过的**告警 payload（once-per-period 语义由
``expiry_warned_at`` 标记承担），watchdog 只管原样 publish；``low_stock`` 是
持续状态（每轮都会算出来），去重责任在 watchdog（签名变化才重发）。

## 钱的方向（与首购一致）

续期 = **先延期后扣费**：``renew_cas``（乐观锁，每期至多赢一次）→ 扣费 →
``finalize_renewal``。中途崩溃留下 ``renew_state='pending'``，服务照续、钱待
对账——「少收一笔」永远好过「双扣」。余额不够则**不延期**（服务到期自然
停），但在 ``renew_before_days`` 提前量里先轰人，给运营充值窗口。

## 续期宽限（RENEW_GRACE_SEC）

watchdog 若停摆一阵，auto_renew 的订阅可能已越过 period_end——一刀切按过期
回收等于「巡检器宕机连坐用户」。故过期未超 24h 且 auto_renew 的订阅仍走续期
（新到期 = 旧到期 + 周期，服务无缝续上）；超过宽限才真回收。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.integrations.proxy_provider import quote_tokens

logger = logging.getLogger(__name__)

# auto_renew 订阅越过 period_end 后仍可续期的宽限（巡检器自身故障不连坐用户）
RENEW_GRACE_SEC = 86400.0

# 告警 payload 的 kind 值（稳定契约：webhook formatter / 测试按字面量断言）
KIND_EXPIRING = "expiring"          # 快到期且不会自动续（没开 auto_renew）
KIND_RENEW_BLOCKED = "renew_blocked"  # 想续但续不了（余额不足/价格未配）
KIND_RENEW_UNBILLED = "renew_unbilled"  # 已续期但扣费失败（待对账）
KIND_EXPIRED = "expired"            # 已到期回收（代理已解绑归还库存）
KIND_LOW_STOCK = "low_stock"        # 库存水位低于阈值（持续态，watchdog 去重）

BalanceFn = Callable[[str], Optional[int]]   # wallet -> 余额；None = 未启用计费
ChargeFn = Callable[[str, int], int]         # (wallet, tokens) -> 实扣
# (order_ref, period_days, provider) -> (ok, 上游新到期或0)。None = 恒成功
# （stock/mock 语义：到期只在我们的账期时钟上）。http 的真实现由 watchdog 注入。
ProlongFn = Callable[[str, int, str], "tuple[bool, float]"]


def _sample(subs: List[Dict[str, Any]], cap: int = 5) -> List[Dict[str, Any]]:
    """告警里带的样本行（防 payload 无限长；只留人看得懂的字段）。"""
    out = []
    for s in subs[:cap]:
        out.append({
            "sub_id": s.get("sub_id") or "",
            "country": s.get("country") or "",
            "kind": s.get("kind") or "",
            "remaining_days": int(s.get("remaining_days") or 0),
        })
    return out


def run_lifecycle_sweep(
    *,
    store: Any,
    pool: Any,
    mcfg: Dict[str, Any],
    balance_fn: BalanceFn,
    charge_fn: ChargeFn,
    prolong_fn: Optional[ProlongFn] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """跑一轮生命周期巡检。返回计数摘要 + 去重后的告警 payload 列表。

    幂等性：同一时刻重跑一次，续期由 ``renew_cas`` 乐观锁挡住、预警由
    ``expiry_warned_at`` 标记挡住、到期回收本身幂等（mark_expired 只动 active 行）
    ——watchdog 重入 / 进程重启后重跑都不会双扣或双轰。
    """
    ts = time.time() if now is None else float(now)
    life = mcfg.get("lifecycle") or {}
    renew_before_sec = float(life.get("renew_before_days", 3)) * 86400.0
    warn_before_sec = float(mcfg.get("expiry_warn_days", 5)) * 86400.0
    period_sec = float(mcfg.get("period_days") or 30) * 86400.0

    out: Dict[str, Any] = {
        "renewed": 0, "renew_unbilled": 0, "renew_blocked": 0,
        "expired": 0, "warned": 0, "alerts": [], "low_stock": {},
    }

    # 一次取全窗口（预警窗 ≥ 续期窗；expiring() 含已过期行）
    horizon = max(renew_before_sec, warn_before_sec)
    try:
        rows = store.expiring(within_sec=horizon, now=ts)
    except Exception:
        logger.debug("[proxy_lifecycle] 取到期窗口失败（本轮跳过）", exc_info=True)
        return out

    renew_blocked: List[Dict[str, Any]] = []
    blocked_reasons: Dict[str, int] = {}
    warned_subs: List[Dict[str, Any]] = []
    expired_subs: List[Dict[str, Any]] = []
    unbilled_subs: List[Dict[str, Any]] = []

    for sub in rows:
        end = float(sub.get("period_end") or 0)
        auto = bool(sub.get("auto_renew"))
        renewable = auto and end > ts - RENEW_GRACE_SEC

        # ── 到期回收（不可续 / 宽限也用尽）─────────────────────────────────
        if end <= ts and not renewable:
            try:
                store.mark_expired(sub["sub_id"], now=ts)
                pid = str(sub.get("proxy_id") or "")
                if pid:
                    # 解绑归还库存（stock 供给方的货自动回到可售池）。绝不删除
                    # 池条目——那是运营的资产登记，续购/排障还要看它。
                    pool.unassign(pid)
                    # JIT（http）来的 IP 在上游已随订单到期作废——标 fail 防
                    # 它作为「未占用池条目」被手动下拉/自动挑选复卖（死 IP
                    # 卖出去=当场穿帮）。stock 自有货没有上游时钟，照常回池。
                    if str(sub.get("provider") or "") == "http":
                        pool.set_status(pid, "fail")
                expired_subs.append(sub)
                out["expired"] += 1
            except Exception:
                logger.warning("[proxy_lifecycle] 到期回收失败 sub=%s",
                               sub.get("sub_id"), exc_info=True)
            continue

        # ── 自动续期（进入续期窗，含过期宽限）──────────────────────────────
        if renewable and end <= ts + renew_before_sec:
            price = quote_tokens(mcfg, kind=str(sub.get("kind") or ""),
                                 country=str(sub.get("country") or ""))
            if price <= 0:
                # 定价被撤（运营下架了该档）→ 不能按 0 元白续，也不能扣个未知数。
                # 视作被拦续期：本期预警一次，到期走正常回收。
                if not float(sub.get("expiry_warned_at") or 0):
                    renew_blocked.append(sub)
                    blocked_reasons["no_pricing"] = (
                        blocked_reasons.get("no_pricing", 0) + 1)
                    store.mark_expiry_warned(sub["sub_id"], now=ts)
                    out["renew_blocked"] += 1
                continue

            wallet = str(sub.get("wallet") or "default")
            balance = balance_fn(wallet)
            billing_on = balance is not None
            if billing_on and int(balance or 0) < price:
                # 余额不够 → 不延期（到期自然停），提前量内轰一次给充值窗口
                if not float(sub.get("expiry_warned_at") or 0):
                    entry = dict(sub)
                    entry["need_tokens"] = price
                    entry["balance"] = int(balance or 0)
                    renew_blocked.append(entry)
                    blocked_reasons["insufficient"] = (
                        blocked_reasons.get("insufficient", 0) + 1)
                    store.mark_expiry_warned(sub["sub_id"], now=ts)
                    out["renew_blocked"] += 1
                continue

            # 上游先续、我们后延（P4 JIT 正确性）：IP 活在上游时钟上，只延我们的
            # 账期不延上游＝收了续费、IP 期中死掉。上游续期失败 → 不延不扣、
            # 本期预警一次，到期走正常回收（宁可不收这笔钱）。
            # 顺序是刻意的（prolong 在 CAS 之前）：崩溃在两步之间 → 下一轮会再
            # prolong 一次＝**我们**多付供应商一期（有界、可被对账 CLI 的
            # vendor_only diff 看见）；反过来 CAS 在前、prolong 失败 → 用户账期
            # 延了上游没延＝用户买到一段死期。单巡检器 + 秒级窗口，前者概率
            # 近零、代价自担；后者伤用户。方向与「宁少收不双扣」一致。
            vendor_end = 0.0
            if prolong_fn is not None:
                try:
                    p_ok, vendor_end = prolong_fn(
                        str(sub.get("order_ref") or ""),
                        int(mcfg.get("period_days") or 30),
                        str(sub.get("provider") or ""))
                except Exception:
                    logger.warning("[proxy_lifecycle] 上游续期异常 sub=%s",
                                   sub.get("sub_id"), exc_info=True)
                    p_ok = False
                if not p_ok:
                    if not float(sub.get("expiry_warned_at") or 0):
                        renew_blocked.append(sub)
                        blocked_reasons["vendor_prolong"] = (
                            blocked_reasons.get("vendor_prolong", 0) + 1)
                        store.mark_expiry_warned(sub["sub_id"], now=ts)
                        out["renew_blocked"] += 1
                    continue

            # 上游给了权威到期就跟上游（IP 的真实死期在上游），没给按我们的周期推
            new_end = vendor_end if vendor_end > end else end + period_sec
            renewed = store.renew_cas(
                sub["sub_id"], expected_end=end, new_end=new_end, now=ts)
            if renewed is None:
                continue  # 本期已被并发/上一轮续过（乐观锁输家）
            charged = 0
            if billing_on:
                try:
                    charged = int(charge_fn(wallet, price) or 0)
                except Exception:
                    logger.warning("[proxy_lifecycle] 续期扣费异常 sub=%s",
                                   sub.get("sub_id"), exc_info=True)
                    charged = 0
            unbilled = billing_on and charged <= 0
            store.finalize_renewal(sub["sub_id"], tokens=charged,
                                   unbilled=unbilled, now=ts)
            out["renewed"] += 1
            if unbilled:
                out["renew_unbilled"] += 1
                unbilled_subs.append(sub)
            continue

        # ── 到期预警（没开 auto_renew 的：只提醒，绝不代客续费）──────────────
        if (not auto and end > ts
                and not float(sub.get("expiry_warned_at") or 0)):
            warned_subs.append(sub)
            store.mark_expiry_warned(sub["sub_id"], now=ts)
            out["warned"] += 1

    # ── 汇总成告警 payload（once-per-period 已由标记保证）────────────────────
    if warned_subs:
        out["alerts"].append({
            "kind": KIND_EXPIRING, "count": len(warned_subs),
            "subs": _sample(warned_subs),
            "rate_key": "proxy_managed:expiring",
        })
    if renew_blocked:
        out["alerts"].append({
            "kind": KIND_RENEW_BLOCKED, "count": len(renew_blocked),
            "reasons": blocked_reasons,
            "need_tokens": sum(int(s.get("need_tokens") or 0)
                               for s in renew_blocked),
            "subs": _sample(renew_blocked),
            "rate_key": "proxy_managed:renew_blocked",
        })
    if unbilled_subs:
        out["alerts"].append({
            "kind": KIND_RENEW_UNBILLED, "count": len(unbilled_subs),
            "subs": _sample(unbilled_subs),
            "rate_key": "proxy_managed:unbilled",
        })
    if expired_subs:
        countries: Dict[str, int] = {}
        for s in expired_subs:
            c = str(s.get("country") or "?")
            countries[c] = countries.get(c, 0) + 1
        out["alerts"].append({
            "kind": KIND_EXPIRED, "count": len(expired_subs),
            "countries": countries, "subs": _sample(expired_subs),
            "rate_key": "proxy_managed:expired",
        })

    # ── 低库存水位（持续态：每轮都算，去重交给 watchdog 的签名比对）──────────
    out["low_stock"] = collect_low_stock(pool=pool, mcfg=mcfg)
    return out


def collect_low_stock(*, pool: Any, mcfg: Dict[str, Any]) -> Dict[str, int]:
    """各观察地区的低库存水位 ``{country: 当前可售数}``（只含低于阈值的）。

    只对 ``stock`` 供给方有意义（上游 API 现开现给没有本地库存概念）；观察名单
    优先级：``lifecycle.low_stock_countries`` > ``allow_countries`` > 默认地区。
    卖空的地区在一键卡上是「暂无库存」，这里让运营在**之前**就知道要补货。
    """
    from src.integrations.proxy_provider import PROVIDER_STOCK, StockProxyProvider

    if str(mcfg.get("provider") or "") != PROVIDER_STOCK:
        return {}
    life = mcfg.get("lifecycle") or {}
    min_stock = int(life.get("min_stock", 2) or 0)
    if min_stock <= 0:
        return {}
    watch = (life.get("low_stock_countries")
             or mcfg.get("allow_countries")
             or [str(mcfg.get("default_country") or "US")])
    try:
        inv = StockProxyProvider(pool).inventory()
    except Exception:
        logger.debug("[proxy_lifecycle] 库存盘点失败（按空）", exc_info=True)
        return {}
    return {c: int(inv.get(c, 0)) for c in watch if int(inv.get(c, 0)) < min_stock}


__all__ = [
    "KIND_EXPIRED",
    "KIND_EXPIRING",
    "KIND_LOW_STOCK",
    "KIND_RENEW_BLOCKED",
    "KIND_RENEW_UNBILLED",
    "RENEW_GRACE_SEC",
    "collect_low_stock",
    "run_lifecycle_sweep",
]
