"""统一收件箱——代理池 / 指纹路由域（巨石拆分 slice 8：路由域试切第一刀）。

把自包含、仅依赖 ``api_auth`` 的代理池（``/api/proxies*``）与指纹
（``/api/fingerprints*``）端点，从 ``register_unified_inbox_routes`` 的巨型闭包中
抽出，封装为 ``register_proxy_fingerprint_routes(app, *, api_auth)``，由主 register
顺序调用。端点路径/方法/响应零变化（由 admin_route_inventory URL 契约守卫保证）。

这是路由域拆分的**模式验证刀**：子注册函数只接收自身真正需要的依赖（此处仅
``api_auth``），其余服务（proxy_pool / fingerprint_store）由本模块自行 import，
不再依赖 routes 的模块级符号，故无循环 import。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import Request

from src.integrations.fingerprint import get_fingerprint_store, summarize as fp_summarize
from src.integrations.proxy_pool import PROXY_KINDS, get_proxy_pool

logger = logging.getLogger(__name__)

# 托管代理扣费动作名（进 token_spend.action，会员页/对账按此聚合）
MANAGED_SPEND_ACTION = "proxy_managed"


def _managed_cfg(request: Request) -> Dict[str, Any]:
    """读 ``proxies.managed`` 归一配置。

    刻意走 ``app.state.config_manager`` 而非 bootstrap 注入的模块级开关：
    ``config.local.yaml`` 是**热重载**的，运营调价、开关地区、临时停售都免重启生效
    ——一键代理的参数属于运营日常动作，不该每次都吃一个 15-30s 的坐席断窗。
    """
    from src.integrations.proxy_provider import parse_managed_cfg

    try:
        cm = getattr(request.app.state, "config_manager", None)
        cfg = (getattr(cm, "config", None) if cm is not None else None) or {}
    except Exception:
        cfg = {}
    return parse_managed_cfg(cfg)


def _wallet_view(now: float = 0.0) -> Dict[str, Any]:
    """Token 钱包视图：{billing, wallet, balance}。绝不抛。

    ``billing='off'`` = 本部署未启用 Token 账本（自建/内部部署的常态）——此时一键
    代理**不扣费**照常开通，用的是运营自己囤的货。这是刻意的：把「计费没开」
    变成「功能不可用」，会让自建用户平白失去一个已经能跑的能力。
    """
    out = {"billing": "off", "wallet": "", "balance": 0}
    try:
        from src.licensing.token_ledger import (
            check_token_balance, ensure_monthly_tokens, token_ledger_enabled,
            wallet_id_for_status,
        )

        if not token_ledger_enabled():
            return out
        out["billing"] = "on"
        ensure_monthly_tokens(now=now or None)
        wallet = wallet_id_for_status()
        out["wallet"] = wallet
        out["balance"] = int(check_token_balance(wallet).get("balance") or 0)
    except Exception:
        logger.debug("[proxy_managed] 钱包读取失败（按未启用计费）", exc_info=True)
    return out


def register_proxy_fingerprint_routes(app, *, api_auth) -> None:
    """挂载代理池 + 指纹相关端点（M4：用户自填，一号一代理 / 一号一指纹）。"""

    # ── 一键代理（托管代理：自动选区 + Token 扣费 + 入池绑定）────────────────
    # 先于 /api/proxies/{proxy_id} 声明：路径形状虽不冲突（末段字面量不同），
    # 但显式排在前面可免去将来加端点时的匹配顺序心智负担。

    @app.get("/api/proxies/managed/status")
    async def api_proxies_managed_status(request: Request):
        """一键代理能力/报价探测——**下单前**把价格、地区、存量、余额一次说清。

        UI 契约：``available=false`` 时整块隐藏一键卡（fail-hidden），只留手动录入。
        报价与真实扣费共用 ``quote_tokens``，结构上不可能出现「页面 300 实扣 500」。
        """
        api_auth(request)
        from src.integrations.proxy_provider import (
            build_provider, capability, quote_tokens, resolve_country,
        )

        mcfg = _managed_cfg(request)
        cap = capability(mcfg)
        q = request.query_params
        phone = str(q.get("phone") or "")
        kind = str(q.get("kind") or mcfg.get("default_kind") or "isp").lower()
        country, source = resolve_country(
            mcfg, requested=str(q.get("country") or ""), phone=phone)

        out: Dict[str, Any] = dict(cap)
        out["ok"] = True
        out["kind"] = kind
        out["country"] = country
        out["country_source"] = source
        out["price_tokens"] = quote_tokens(mcfg, kind=kind, country=country)

        wallet = _wallet_view()
        out["billing"] = wallet["billing"]
        out["balance"] = wallet["balance"]
        # 未启用计费 = 不扣费 → 恒可负担（别让「余额 0」挡住自建部署）
        out["affordable"] = (wallet["billing"] == "off"
                             or wallet["balance"] >= out["price_tokens"])

        provider = build_provider(mcfg, pool=get_proxy_pool())
        inv = (await provider.inventory_async(kind=kind)
               if provider is not None else {})
        out["inventory"] = inv
        # 存量语义（P2/P4 两次修正沉淀）：
        # - stock：能盘点，空 dict＝真零库存 → 禁售（旧「空=不支持盘点=放行」把
        #   空池部署变成「按钮可点、点了必报 out_of_stock」的死路按钮）；
        # - http 配了 inventory：目录级余量，n>0 或 -1（有货不给数）＝可买，
        #   不含该国/n==0＝该地区上游无货 → 禁售；
        # - http 未配 inventory / mock：无从盘点，fail-open 放行（下单环节兜底）。
        from src.integrations.proxy_provider import PROVIDER_STOCK

        n = int(inv.get(country, 0))
        if provider is not None and provider.name == PROVIDER_STOCK:
            out["in_stock"] = n > 0
        else:
            out["in_stock"] = (not inv) or n > 0 or n == -1
        return out

    @app.post("/api/proxies/managed/provision")
    async def api_proxies_managed_provision(request: Request):
        """一键开通：占坑（幂等）→ 取货 → 入池绑定 → 记账。

        顺序是刻意的——**先交付后扣费**：中途崩溃时「我们少收一笔」远好过「用户
        扣了钱没拿到代理」。滥用敞口由下单前的余额预检封住（余额不够根本不向上游
        要货），且每一步失败都有对应回滚（取货失败删占坑，记账失败留 charged=0
        待对账）。
        """
        api_auth(request)
        from src.integrations.proxy_provider import (
            build_provider, capability, period_end, quote_tokens, resolve_country,
        )
        from src.integrations.proxy_subscription import (
            build_charge_ref, get_proxy_subscriptions,
        )

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}

        mcfg = _managed_cfg(request)
        cap = capability(mcfg)
        if not cap.get("available"):
            return {"ok": False, "error": "unavailable", "reason": cap.get("reason") or ""}

        now = time.time()
        phone = str(body.get("phone") or "")
        account_key = str(body.get("account_key") or "").strip()
        kind = str(body.get("kind") or mcfg.get("default_kind") or "isp").lower()
        if kind not in PROXY_KINDS:
            kind = "isp"
        country, country_source = resolve_country(
            mcfg, requested=str(body.get("country") or ""), phone=phone)
        price = quote_tokens(mcfg, kind=kind, country=country)
        if price <= 0:
            # capability 已探过默认档，但「所选地区/类型」仍可能配漏 → 不按 0 元放行
            return {"ok": False, "error": "unavailable", "reason": "no_pricing"}

        wallet = _wallet_view(now)
        if wallet["billing"] == "on" and wallet["balance"] < price:
            return {"ok": False, "error": "insufficient_tokens",
                    "need": price, "balance": wallet["balance"]}

        store = get_proxy_subscriptions()
        charge_ref = build_charge_ref(
            wallet=wallet["wallet"], account_key=account_key,
            request_id=str(body.get("request_id") or ""), now=now)
        sub = store.reserve(
            charge_ref=charge_ref, wallet=wallet["wallet"], account_key=account_key,
            provider=str(mcfg.get("provider") or ""), country=country, kind=kind,
            tokens=price, auto_renew=bool(mcfg.get("auto_renew")), now=now,
        )
        if sub is None:
            # 幂等重放：连点第二下 / 网络重发 → 原样返回首次结果，绝不再买一条
            prior = store.by_charge_ref(charge_ref, now=now) or {}
            pool_entry = get_proxy_pool().get(str(prior.get("proxy_id") or "")) or {}
            return {"ok": bool(prior.get("proxy_id")), "replay": True,
                    "subscription": prior, "proxy": pool_entry,
                    "tokens": int(prior.get("tokens") or 0),
                    "balance": wallet["balance"],
                    "error": "" if prior.get("proxy_id") else "in_progress"}

        pool = get_proxy_pool()
        provider = build_provider(mcfg, pool=pool)
        if provider is None:
            store.release(sub["sub_id"])
            return {"ok": False, "error": "unavailable", "reason": "no_provider"}

        # ── 取货 + 开通即体检（P2-3）───────────────────────────────────────
        # stock：验活失败换下一条重挑（自有库存零边际成本；pool.test 失败会经
        #   record_probe 标 fail/冷却，pick_available 天然跳过，不会原地打转）。
        # http：只验活一次、结果**不拦交付**（上游已对这单计费，丢弃=我们白付；
        #   且陌生厂商出口对探测器可能假阴），探测结果照记状态机供事后换货。
        # mock：跳过验活（.invalid 假出口专供测试，验必挂）。
        verify = bool(mcfg.get("verify_on_provision", True)) and provider.name != "mock"
        max_attempts = (int(mcfg.get("verify_max_attempts") or 3)
                        if (verify and provider.name == "stock") else 1)
        proxy_id, res, verified = "", None, None
        saw_unhealthy = False
        for _attempt in range(max_attempts):
            try:
                res = await provider.provision(
                    country=country, kind=kind,
                    period_days=int(mcfg.get("period_days") or 30),
                    label=f"一键代理 {country}",
                    idempotency_key=charge_ref,  # 上游支持幂等键时双侧防重复下单
                )
            except Exception:  # noqa: BLE001 — 供给方异常绝不炸路由，且必须释放占坑
                logger.warning("[proxy_managed] 供给方异常", exc_info=True)
                store.release(sub["sub_id"])
                return {"ok": False, "error": "upstream_error", "country": country}
            if not res.ok:
                break  # 缺货/上游拒绝：没有下一条可试
            try:
                if res.existing_proxy_id:
                    candidate = res.existing_proxy_id  # 库存复用：绝不重复 add
                else:
                    entry = pool.add(
                        scheme=res.scheme, host=res.host, port=res.port,
                        username=res.username, password=res.password,
                        label=f"一键代理 {res.country or country}",
                        kind=res.kind if res.kind in PROXY_KINDS else kind,
                        country=res.country or country,
                    )
                    candidate = str(entry.get("proxy_id") or "")
                if not candidate:
                    raise ValueError("proxy_id 为空")
            except Exception as ex:  # noqa: BLE001
                logger.warning("[proxy_managed] 入池失败: %s", ex, exc_info=True)
                store.release(sub["sub_id"])
                return {"ok": False, "error": "bind_failed", "country": country}
            if verify:
                healthy = await pool.test(candidate, timeout=6.0)
                verified = bool(healthy)
                if not healthy and provider.name == "stock":
                    saw_unhealthy = True
                    continue  # 已标 fail/冷却，换下一条
            proxy_id = candidate
            break

        if not proxy_id:
            # 与「本来就没货」分开报：验活淘汰过候选＝有货但全是坏的（运营该查
            # 库存质量），单纯 out_of_stock＝该补货。两种都诚实失败、不扣费。
            store.release(sub["sub_id"])
            if saw_unhealthy:
                return {"ok": False, "error": "no_healthy_stock", "country": country}
            return {"ok": False,
                    "error": (res.error if res else "") or "upstream_error",
                    "country": country}

        try:
            # 无 account_key（连号弹层里账号还没建）→ 用订阅号占位持有，防并发把同
            # 一条库存卖两次；账号建好走常规绑定流程时会被真实 account_key 覆盖。
            pool.assign(proxy_id, account_key or f"pending:{sub['sub_id']}",
                        exclusive=bool(account_key))
        except Exception as ex:  # noqa: BLE001
            logger.warning("[proxy_managed] 绑定失败: %s", ex, exc_info=True)
            store.release(sub["sub_id"])
            return {"ok": False, "error": "bind_failed", "country": country}

        charged = 0
        if wallet["billing"] == "on":
            from src.licensing.token_ledger import record_fixed_spend

            charged = record_fixed_spend(
                wallet["wallet"], MANAGED_SPEND_ACTION, price, now=now)

        sub = store.activate(
            sub["sub_id"], proxy_id=proxy_id, order_ref=res.order_ref,
            period_end=res.expires_at or period_end(mcfg, now=now),
            charged=(wallet["billing"] == "off" or charged > 0),
            country=res.country or country, kind=res.kind or kind,
            note="" if wallet["billing"] == "on" else "billing_off", now=now,
        ) or {}

        return {
            "ok": True, "replay": False,
            "proxy": pool.get(proxy_id) or {},
            "subscription": sub,
            "tokens": charged,
            "price_tokens": price,
            "country": res.country or country,
            "country_source": country_source,
            "verified": verified,  # None=未验（关了/mock），true/false=体检结果
            "balance": max(0, wallet["balance"] - charged),
        }

    @app.post("/api/proxies/managed/swap")
    async def api_proxies_managed_swap(request: Request):
        """免费换货：窗口期内（默认 3 天）代理不好用 → 同地区同类型换一条，零扣费。

        为什么要有它：开通即体检只能拦「当场就连不上」的；「用两天变慢/被封」
        只有用户知道。窗口 + 次数双限（``swap_window_days`` / ``max_swaps``）封住
        「无限换货当 IP 轮换器薅」的口子。旧代理标 fail 进健康状态机（不删除——
        资产登记要留着），新代理走与首购同一套验活。
        """
        api_auth(request)
        from src.integrations.proxy_provider import build_provider
        from src.integrations.proxy_subscription import get_proxy_subscriptions

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}

        mcfg = _managed_cfg(request)
        if not mcfg.get("enabled"):
            return {"ok": False, "error": "unavailable"}
        store = get_proxy_subscriptions()
        sub = store.get(str(body.get("sub_id") or ""))
        if not sub or sub.get("status") != "active":
            return {"ok": False, "error": "sub_not_found"}

        now = time.time()
        window_sec = float(mcfg.get("swap_window_days") or 0) * 86400.0
        started = float(sub.get("period_start") or 0)
        if window_sec > 0 and started and now - started > window_sec:
            return {"ok": False, "error": "swap_window_closed",
                    "window_days": mcfg.get("swap_window_days")}
        if int(sub.get("swap_count") or 0) >= int(mcfg.get("max_swaps") or 0):
            return {"ok": False, "error": "swap_limit_reached",
                    "max_swaps": mcfg.get("max_swaps")}

        pool = get_proxy_pool()
        provider = build_provider(mcfg, pool=pool)
        if provider is None:
            return {"ok": False, "error": "unavailable"}

        old_id = str(sub.get("proxy_id") or "")
        if old_id:
            # 先解绑再标 fail：pick_available 会跳过 fail，同一条不会被自己换回来
            try:
                pool.unassign(old_id)
                pool.set_status(old_id, "fail")
            except Exception:
                logger.debug("[proxy_managed] 旧代理下线失败（继续换货）",
                             exc_info=True)

        verify = bool(mcfg.get("verify_on_provision", True)) and provider.name != "mock"
        attempts = (int(mcfg.get("verify_max_attempts") or 3)
                    if (verify and provider.name == "stock") else 1)
        new_id = ""

        # 上游免费换 IP 优先（P4 JIT）：Proxy-Seller 型上游自带每月更换配额——
        # 走 replace＝零成本换出口；回落 provision＝**再买一单**真花钱。
        # replace 配置了但失败 → 如实报错让运营查（静默回落=悄悄花钱，比报错更伤）。
        rep = None
        try:
            rep = await provider.replace(
                order_ref=str(sub.get("order_ref") or ""),
                country=str(sub.get("country") or ""),
                kind=str(sub.get("kind") or ""))
        except Exception:  # noqa: BLE001
            logger.warning("[proxy_managed] 上游换IP异常", exc_info=True)
            return {"ok": False, "error": "vendor_replace_failed"}
        if rep is not None:
            if not rep.ok:
                return {"ok": False, "error": rep.error or "vendor_replace_failed"}
            try:
                entry = pool.add(
                    scheme=rep.scheme, host=rep.host, port=rep.port,
                    username=rep.username, password=rep.password,
                    label=f"一键代理 {rep.country or sub.get('country') or ''}",
                    kind=(rep.kind if rep.kind in PROXY_KINDS
                          else str(sub.get("kind") or "unknown")),
                    country=rep.country or str(sub.get("country") or ""),
                )
                new_id = str(entry.get("proxy_id") or "")
            except Exception:  # noqa: BLE001
                logger.warning("[proxy_managed] 换货入池失败", exc_info=True)
                return {"ok": False, "error": "bind_failed"}
            if verify and new_id:
                await pool.test(new_id, timeout=6.0)  # http 只记录不拦（同首购）

        # 上游不支持 replace（None）→ 老路：重新出一条（对 stock 是换自有库存
        # 零成本；对 http 是再买一单）。已经 replace 成功则整个循环跳过。
        for _ in range(attempts if not new_id else 0):
            try:
                res = await provider.provision(
                    country=str(sub.get("country") or ""),
                    kind=str(sub.get("kind") or ""),
                    period_days=int(mcfg.get("period_days") or 30),
                    label=f"一键代理换货 {sub.get('country') or ''}",
                    # 同一次换货的重试共用键（sub+当前换货序号），上游不重复下单
                    idempotency_key=f"pxswap:{sub['sub_id']}:"
                                    f"{int(sub.get('swap_count') or 0)}",
                )
            except Exception:  # noqa: BLE001
                logger.warning("[proxy_managed] 换货供给异常", exc_info=True)
                return {"ok": False, "error": "upstream_error"}
            if not res.ok:
                return {"ok": False, "error": res.error or "out_of_stock"}
            try:
                if res.existing_proxy_id:
                    candidate = res.existing_proxy_id
                else:
                    entry = pool.add(
                        scheme=res.scheme, host=res.host, port=res.port,
                        username=res.username, password=res.password,
                        label=f"一键代理 {res.country or sub.get('country') or ''}",
                        kind=(res.kind if res.kind in PROXY_KINDS
                              else str(sub.get("kind") or "unknown")),
                        country=res.country or str(sub.get("country") or ""),
                    )
                    candidate = str(entry.get("proxy_id") or "")
            except Exception:  # noqa: BLE001
                logger.warning("[proxy_managed] 换货入池失败", exc_info=True)
                return {"ok": False, "error": "bind_failed"}
            if verify and candidate:
                if not await pool.test(candidate, timeout=6.0):
                    if provider.name == "stock":
                        continue
            new_id = candidate
            break
        if not new_id:
            return {"ok": False, "error": "no_healthy_stock"}

        pool.assign(new_id, str(sub.get("account_key") or "")
                    or f"pending:{sub['sub_id']}",
                    exclusive=bool(sub.get("account_key")))
        sub2 = store.record_swap(sub["sub_id"], new_proxy_id=new_id, now=now) or {}
        logger.info("[proxy_managed] 换货 sub=%s %s→%s",
                    sub.get("sub_id"), old_id, new_id)
        return {"ok": True, "proxy": pool.get(new_id) or {},
                "subscription": sub2, "old_proxy_id": old_id}

    @app.get("/api/proxies/managed/overview")
    async def api_proxies_managed_overview(request: Request):
        """运维总览（ops 卡数据源）：订阅台账 + 到期分布 + 库存水位 + 待对账。

        capability 探测失败也照常返回台账数据——「功能被临时关掉」不该让运营
        看不见还剩多少活跃订阅在计时。
        """
        api_auth(request)
        from src.integrations.proxy_lifecycle import collect_low_stock
        from src.integrations.proxy_provider import capability
        from src.integrations.proxy_subscription import get_proxy_subscriptions

        mcfg = _managed_cfg(request)
        store = get_proxy_subscriptions()
        now = time.time()
        stats = store.stats(now=now)
        expiring = store.expiring(within_sec=7 * 86400, now=now)
        pending = store.pending_renewals(now=now)
        # JIT（http 供给）模式下「上游余额」就是库存水位——一并出给 ops 卡
        vendor_balance = None
        try:
            from src.integrations.proxy_provider import build_provider

            provider = build_provider(mcfg, pool=get_proxy_pool())
            if provider is not None:
                vendor_balance = await provider.vendor_balance()
        except Exception:
            logger.debug("[proxy_managed] 上游余额读取失败（按未知）", exc_info=True)
        return {
            "ok": True,
            "capability": capability(mcfg),
            "stats": stats,
            "expiring_7d": [{
                "sub_id": s.get("sub_id"), "country": s.get("country"),
                "kind": s.get("kind"), "auto_renew": bool(s.get("auto_renew")),
                "remaining_days": int(s.get("remaining_days") or 0),
                "proxy_id": s.get("proxy_id"),
            } for s in expiring[:20]],
            "renew_pending": [{
                "sub_id": s.get("sub_id"), "renew_state": s.get("renew_state"),
                "tokens": int(s.get("tokens") or 0),
            } for s in pending[:20]],
            "low_stock": collect_low_stock(pool=get_proxy_pool(), mcfg=mcfg),
            "min_stock": int((mcfg.get("lifecycle") or {}).get("min_stock") or 0),
            "vendor_balance": vendor_balance,
            "vendor_min_balance": (
                ((mcfg.get("http") or {}).get("balance") or {}).get("min_alert")
                if isinstance((mcfg.get("http") or {}).get("balance"), dict)
                else None),
        }

    # ── 代理池（M4：用户自填，一号一代理） ──────────────────────────────────
    @app.get("/api/proxies")
    async def api_proxies_list(request: Request):
        api_auth(request)
        return {"ok": True, "proxies": get_proxy_pool().list()}

    @app.post("/api/proxies")
    async def api_proxies_add(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            entry = get_proxy_pool().add(
                scheme=str((body or {}).get("scheme") or "socks5"),
                host=str((body or {}).get("host") or "").strip(),
                port=int((body or {}).get("port") or 0),
                username=str((body or {}).get("username") or ""),
                password=str((body or {}).get("password") or ""),
                label=str((body or {}).get("label") or ""),
                kind=str((body or {}).get("kind") or "unknown"),
                country=str((body or {}).get("country") or ""),
                region=str((body or {}).get("region") or ""),
            )
            return {"ok": True, "proxy": entry}
        except (ValueError, TypeError) as ex:
            return {"ok": False, "detail": str(ex)}

    @app.post("/api/proxies/import")
    async def api_proxies_import(request: Request):
        """批量导入代理（一键代理 P3：把「供应商导出的 txt」变成可售库存）。

        没有它，运营买 50 条 IP 要在弹窗里手录 50 遍——「灌库存」这步太痛，
        stock 供给模式就永远只是纸面方案。行格式解析在 ``parse_import_lines``
        （纯函数，坏行逐行带行号回报）；country/kind 按**本批**统一打标——
        供应商本就按地区卖批次，与现实操作一致。批间去重按 (host, port,
        username) 对照现有池子，重复行如实计数不算错误。
        """
        api_auth(request)
        from src.integrations.proxy_pool import parse_import_lines

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        entries, bad = parse_import_lines(
            str(body.get("text") or ""),
            default_scheme=str(body.get("scheme") or "socks5"))
        if len(entries) > 500:
            # 错误码由前端映射 i18n（与本文件其他端点同约定，见 _ocErrText 先例）
            return {"ok": False, "error": "too_many", "parsed": len(entries)}
        country = str(body.get("country") or "").strip().upper()
        kind = str(body.get("kind") or "unknown").strip().lower()
        if kind not in PROXY_KINDS:
            kind = "unknown"
        label = str(body.get("label") or "").strip()

        pool = get_proxy_pool()
        existing = {(p.get("host"), int(p.get("port") or 0),
                     p.get("username") or "") for p in pool.list()}
        added, dup = 0, 0
        for e in entries:
            if (e["host"], e["port"], e["username"]) in existing:
                dup += 1
                continue
            try:
                pool.add(scheme=e["scheme"], host=e["host"], port=e["port"],
                         username=e["username"], password=e["password"],
                         label=label, kind=kind, country=country)
                added += 1
            except (ValueError, TypeError) as ex:
                bad.append({"line": 0, "text": e["host"], "reason": str(ex)})
        return {"ok": True, "added": added, "dup": dup,
                "bad": bad[:20], "bad_total": len(bad),
                "parsed": len(entries)}

    @app.delete("/api/proxies/{proxy_id}")
    async def api_proxies_remove(proxy_id: str, request: Request):
        api_auth(request)
        get_proxy_pool().remove(proxy_id)
        return {"ok": True}

    @app.post("/api/proxies/{proxy_id}/test")
    async def api_proxies_test(proxy_id: str, request: Request):
        api_auth(request)
        pool = get_proxy_pool()
        ok = await pool.test(proxy_id)
        entry = pool.get(proxy_id) or {}
        # 兼容旧响应（reachable/status）+ 补真握手探到的出口画像与冷却态。
        return {
            "ok": True,
            "reachable": ok,
            "status": entry.get("status") or ("ok" if ok else "fail"),
            "exit_ip": entry.get("exit_ip") or "",
            "country": entry.get("country") or "",
            "latency_ms": entry.get("latency_ms") or 0,
            "in_cooldown": bool(entry.get("in_cooldown")),
        }

    # ── 指纹（M4：自研，一号一指纹） ────────────────────────────────────────
    @app.get("/api/fingerprints")
    async def api_fingerprints_list(request: Request):
        api_auth(request)
        items = get_fingerprint_store().list()
        for it in items:
            it["summary"] = fp_summarize(it.get("profile") or {})
        return {"ok": True, "fingerprints": items}

    @app.post("/api/fingerprints/generate")
    async def api_fingerprints_generate(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        fp = get_fingerprint_store().create(
            seed=str((body or {}).get("seed") or "") or None,
            label=str((body or {}).get("label") or ""),
        )
        fp["summary"] = fp_summarize(fp.get("profile") or {})
        return {"ok": True, **fp}
