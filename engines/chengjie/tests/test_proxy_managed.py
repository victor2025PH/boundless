# -*- coding: utf-8 -*-
"""一键代理（托管代理）门禁——**重点全在钱的路径**（2026-08-21 P1）。

这个功能会花用户真金白银买来的余额，故门禁的分配不是均匀的：选区/报价这类纯函数
穷举到边界，而**「会不会多扣一笔」「失败了钱退没退」「余额不够会不会照买」**
这几条各有独立用例，且刻意用「跑两遍看账本总额」这种端到端断言——只测
``reserve`` 返回 None 是不够的，那证明不了没扣钱。

四条硬不变量（红了先看是不是钱出了问题）：
1. 同 ``request_id`` 重放 → 只买一条、只扣一次，第二次返回**同一条**代理。
2. 供给失败 / 入池失败 → 一分钱不扣，且占坑被释放（同键可原样重试）。
3. 余额不足 → 根本不向供给方要货（不能出现「货领了钱不够」）。
4. 价格 ≤ 0 一律当「没配好」拒绝，绝不按 0 元放行。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.integrations import proxy_pool as pp_mod
from src.integrations import proxy_subscription as sub_mod
from src.integrations.proxy_pool import ProxyPool
from src.integrations.proxy_provider import (
    MockProxyProvider,
    StockProxyProvider,
    build_provider,
    capability,
    country_for_phone,
    normalize_phone,
    parse_managed_cfg,
    period_end,
    quote_tokens,
    resolve_country,
)
from src.integrations.proxy_subscription import (
    STATUS_ACTIVE,
    STATUS_RESERVED,
    ProxySubscriptionStore,
    build_charge_ref,
)
from src.licensing.token_ledger import (
    TokenLedgerStore,
    configure_token_ledger,
    record_fixed_spend,
    reset_token_ledger,
)
from src.web.routes.unified_inbox_proxy_routes import (
    MANAGED_SPEND_ACTION,
    register_proxy_fingerprint_routes,
)


# ── 夹具 ─────────────────────────────────────────────────────────────────────

def _mcfg(**over):
    """构造一份「配好了、可购买」的 managed 配置（各例按需覆盖）。"""
    base = {
        "enabled": True,
        "provider": "stock",
        "default_kind": "isp",
        "default_country": "US",
        "period_days": 30,
        "pricing": {"default": 100, "by_kind": {"isp": 300, "mobile": 900}},
    }
    base.update(over)
    return parse_managed_cfg({"proxies": {"managed": base}})


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """独立代理池（绝不碰生产 config/proxy_pool.db）。"""
    p = ProxyPool(tmp_path / "px.db")
    monkeypatch.setattr(pp_mod, "_pool", p, raising=False)
    return p


@pytest.fixture
def subs(monkeypatch):
    s = ProxySubscriptionStore(":memory:")
    monkeypatch.setattr(sub_mod, "_store", s, raising=False)
    return s


@pytest.fixture
def ledger():
    """内存 Token 账本 + 总闸开。yield 后必复位，防串味到其他测试文件。"""
    reset_token_ledger()
    store = TokenLedgerStore(":memory:")
    configure_token_ledger(store=store, enabled=True)
    yield store
    reset_token_ledger()


def _client(cfg_managed):
    """本文件的门禁全部对准**钱的路径** → 基线关掉开通即体检（P2）：
    假库存主机（10.0.0.x）真验活只会白吃 TCP 超时把钱侧断言拖慢/带偏。
    体检+换货有自己的专项（tests/test_proxy_lifecycle.py，注入探针离线跑）。"""
    cfg_managed.setdefault("verify_on_provision", False)
    app = FastAPI()
    register_proxy_fingerprint_routes(app, api_auth=lambda request: True)
    app.state.config_manager = SimpleNamespace(
        config={"proxies": {"managed": cfg_managed}})
    return TestClient(app)


def _stock(pool, *, country="JP", kind="isp", n=1):
    out = []
    for i in range(n):
        e = pool.add(scheme="socks5", host=f"10.0.0.{i + 1}", port=1080,
                     kind=kind, country=country)
        out.append(e["proxy_id"])
    return out


def _spent(ledger, wallet="default"):
    return int(ledger.usage(wallet).get("by_action", {}).get(MANAGED_SPEND_ACTION, 0))


# ── 1) 选区：手机号 → 国家 ───────────────────────────────────────────────────

@pytest.mark.parametrize("phone,expect", [
    ("+81 90-1234-5678", "JP"),
    ("008613800138000", "CN"),        # 国际拨出前缀 00 要剥掉
    ("+852 6123 4567", "HK"),         # 长号优先：852 不能被 85/8 抢走
    ("+1 (415) 555-0100", "US"),
    ("+65 8123 4567", "SG"),
    ("+998 90 123 4567", "UZ"),
    ("", ""),
    ("12345", "US"),                  # 1 开头 → US（共享区号按主要市场归一）
    ("+999 000", ""),                 # 无此区号 → 判不出
])
def test_country_for_phone(phone, expect):
    assert country_for_phone(phone) == expect


def test_normalize_phone_strips_formatting():
    assert normalize_phone("+81 (90) 1234-5678") == "819012345678"


def test_unknown_phone_returns_empty_not_a_guess():
    """判不出必须返回空串而不是兜底国家。

    调用方要区分「按号码定位到 JP」与「不知道、用了默认」——前者可以对用户说
    「已按你的号码匹配日本 IP」，后者说这句话就是撒谎。
    """
    assert country_for_phone("+999123") == ""


# ── 2) 选区优先级与白名单 ────────────────────────────────────────────────────

def test_resolve_country_priority():
    m = _mcfg()
    assert resolve_country(m, requested="DE", phone="+8190123") == ("DE", "requested")
    assert resolve_country(m, phone="+8190123") == ("JP", "phone")
    assert resolve_country(m, phone="+999123") == ("US", "default")


def test_allowlist_rejects_unstocked_region():
    """只囤了几个地区的货时，用户不该选到一个永远开不出来的地区。"""
    m = _mcfg(allow_countries=["US", "JP"])
    assert resolve_country(m, requested="DE") == ("US", "default")
    assert resolve_country(m, phone="+4915112345") == ("US", "default")  # DE 不在名单
    assert resolve_country(m, phone="+8190123") == ("JP", "phone")


# ── 3) 报价（金额入口，只此一处） ────────────────────────────────────────────

def test_quote_uses_by_kind_then_default():
    m = _mcfg()
    assert quote_tokens(m, kind="isp", country="US") == 300
    assert quote_tokens(m, kind="mobile", country="US") == 900
    assert quote_tokens(m, kind="datacenter", country="US") == 100  # 回落 default


def test_quote_zero_means_unbuyable_never_free():
    """未配价 = 不可购买，**绝不**当免费放行。

    白送真金白银买来的出口是本模块最需要防的事故；「配置笔误 → 白送」必须在
    这一层就死掉。
    """
    m = _mcfg(pricing={"default": 0, "by_kind": {}})
    assert quote_tokens(m, kind="isp", country="US") == 0
    assert capability(m)["available"] is False
    assert capability(m)["reason"] == "no_pricing"


def test_quote_multiplier_is_clamped():
    """倍率越界自动夹紧：多打一个 0 不该变成 10 倍账单或白送。"""
    m = _mcfg(pricing={"default": 100, "by_kind": {"isp": 100},
                       "country_multiplier": {"JP": 999, "US": 0.0001, "DE": 1.5}})
    assert quote_tokens(m, kind="isp", country="JP") == 1000   # 夹到 10x
    assert quote_tokens(m, kind="isp", country="US") == 10     # 夹到 0.1x
    assert quote_tokens(m, kind="isp", country="DE") == 150


def test_quote_rounds_up():
    m = _mcfg(pricing={"default": 100, "by_kind": {"isp": 101},
                       "country_multiplier": {"JP": 1.005}})
    assert quote_tokens(m, kind="isp", country="JP") == 102  # ceil(101.505)


# ── 4) 能力探测（fail-hidden 的判据） ────────────────────────────────────────

@pytest.mark.parametrize("over,reason", [
    ({"enabled": False}, "disabled"),
    ({"provider": "nope"}, "no_provider"),
    ({"pricing": {"default": 0, "by_kind": {}}}, "no_pricing"),
    ({"provider": "http", "http": {}}, "no_credentials"),
])
def test_capability_reasons(over, reason):
    cap = capability(_mcfg(**over))
    assert cap["available"] is False and cap["reason"] == reason


def test_capability_available_when_configured():
    assert capability(_mcfg())["available"] is True


def test_build_provider_none_when_unavailable(pool):
    assert build_provider(_mcfg(enabled=False), pool=pool) is None
    assert build_provider(_mcfg(provider="http", http={}), pool=pool) is None
    assert isinstance(build_provider(_mcfg(), pool=pool), StockProxyProvider)
    assert isinstance(build_provider(_mcfg(provider="mock"), pool=pool),
                      MockProxyProvider)


# ── 5) 库存供给方 ────────────────────────────────────────────────────────────

async def test_stock_provider_delivers_matching_region(pool):
    [pid] = _stock(pool, country="JP")
    res = await StockProxyProvider(pool).provision(
        country="JP", kind="isp", period_days=30)
    assert res.ok and res.existing_proxy_id == pid and res.country == "JP"


async def test_stock_provider_never_substitutes_region(pool):
    """要日本给美国属于货不对板——宁可缺货失败，绝不静默顶包。"""
    _stock(pool, country="US", n=3)
    res = await StockProxyProvider(pool).provision(
        country="JP", kind="isp", period_days=30)
    assert res.ok is False and res.error == "out_of_stock"


async def test_stock_provider_relaxes_kind_but_not_region(pool):
    """类型多是内部分级（住宅↔ISP 对用户是同一件事）→ 可放宽；地区不行。"""
    _stock(pool, country="JP", kind="residential")
    res = await StockProxyProvider(pool).provision(
        country="JP", kind="isp", period_days=30)
    assert res.ok and res.kind == "residential"


async def test_stock_provider_skips_assigned_and_failed(pool):
    ids = _stock(pool, country="JP", n=2)
    pool.assign(ids[0], "acct:1")
    pool.set_status(ids[1], "fail")
    res = await StockProxyProvider(pool).provision(
        country="JP", kind="isp", period_days=30)
    assert res.ok is False and res.error == "out_of_stock"


def test_inventory_counts_only_deliverable(pool):
    ids = _stock(pool, country="JP", n=3)
    _stock(pool, country="US", n=1)
    pool.assign(ids[0], "acct:1")
    inv = StockProxyProvider(pool).inventory()
    assert inv == {"JP": 2, "US": 1}


# ── 6) 订阅台账：幂等闸 ──────────────────────────────────────────────────────

def test_reserve_is_idempotent(subs):
    a = subs.reserve(charge_ref="ref1", wallet="w", tokens=300)
    b = subs.reserve(charge_ref="ref1", wallet="w", tokens=300)
    assert a is not None and b is None
    assert subs.by_charge_ref("ref1")["sub_id"] == a["sub_id"]


def test_release_frees_the_key_for_retry(subs):
    """开通失败 → 同一个 charge_ref 必须能原样重试（一分钱没扣，交易从未发生）。"""
    a = subs.reserve(charge_ref="ref1", wallet="w", tokens=300)
    subs.release(a["sub_id"])
    assert subs.by_charge_ref("ref1") is None
    assert subs.reserve(charge_ref="ref1", wallet="w", tokens=300) is not None


def test_release_never_touches_active(subs):
    """已激活的订阅 = 货已交付，release 绝不能把它删掉。"""
    a = subs.reserve(charge_ref="ref1", wallet="w", tokens=300)
    subs.activate(a["sub_id"], proxy_id="px_1", period_end=time.time() + 86400)
    subs.release(a["sub_id"])
    assert subs.get(a["sub_id"])["status"] == STATUS_ACTIVE


def test_activate_records_remaining_days(subs):
    now = time.time()
    a = subs.reserve(charge_ref="r", wallet="w", tokens=300, now=now)
    assert a["status"] == STATUS_RESERVED
    row = subs.activate(a["sub_id"], proxy_id="px_1",
                        period_end=now + 30 * 86400, now=now)
    assert row["status"] == STATUS_ACTIVE and row["remaining_days"] == 30
    assert row["charged"] is True and row["expired"] is False


def test_expiring_and_by_proxy(subs):
    now = time.time()
    a = subs.reserve(charge_ref="r1", wallet="w", tokens=1, now=now)
    subs.activate(a["sub_id"], proxy_id="px_a", period_end=now + 2 * 86400, now=now)
    b = subs.reserve(charge_ref="r2", wallet="w", tokens=1, now=now)
    subs.activate(b["sub_id"], proxy_id="px_b", period_end=now + 40 * 86400, now=now)
    assert [r["proxy_id"] for r in subs.expiring(within_sec=5 * 86400, now=now)] == ["px_a"]
    assert subs.by_proxy_id("px_b")["sub_id"] == b["sub_id"]
    assert subs.stats(now=now)["active"] == 2


def test_charge_ref_shape():
    """带 request_id → 稳定键；不带 → 30 秒时间桶（挡连点与网络重发）。"""
    assert build_charge_ref(wallet="w", account_key="a", request_id="r1") == "pxsub:w:r1"
    k1 = build_charge_ref(wallet="w", account_key="a", now=1000.0)
    k2 = build_charge_ref(wallet="w", account_key="a", now=1010.0)
    k3 = build_charge_ref(wallet="w", account_key="a", now=1100.0)
    assert k1 == k2 and k1 != k3


def test_period_end_follows_config():
    now = 1_000_000.0
    assert period_end(_mcfg(period_days=7), now=now) == now + 7 * 86400


# ── 7) 路由：状态探测 ────────────────────────────────────────────────────────

def test_status_hides_feature_when_unconfigured(pool, subs):
    r = _client({"enabled": False}).get("/api/proxies/managed/status").json()
    assert r["ok"] is True and r["available"] is False and r["reason"] == "disabled"


def test_status_quotes_by_phone_region(pool, subs):
    _stock(pool, country="JP", n=2)
    c = _client({"enabled": True, "provider": "stock", "default_kind": "isp",
                 "default_country": "US",
                 "pricing": {"by_kind": {"isp": 300},
                             "country_multiplier": {"JP": 2}}})
    r = c.get("/api/proxies/managed/status", params={"phone": "+819012345678"}).json()
    assert r["available"] is True
    assert (r["country"], r["country_source"]) == ("JP", "phone")
    assert r["price_tokens"] == 600 and r["in_stock"] is True
    assert r["inventory"] == {"JP": 2}


def test_status_reports_out_of_stock_region(pool, subs):
    _stock(pool, country="US", n=1)
    c = _client({"enabled": True, "provider": "stock",
                 "pricing": {"by_kind": {"isp": 300}}})
    r = c.get("/api/proxies/managed/status", params={"phone": "+819012345678"}).json()
    assert r["country"] == "JP" and r["in_stock"] is False


def test_status_affordable_when_billing_off(pool, subs):
    """未启用 Token 账本的自建部署：不扣费照常可用，别被「余额 0」挡住。"""
    _stock(pool, country="US")
    c = _client({"enabled": True, "provider": "stock",
                 "pricing": {"by_kind": {"isp": 300}}})
    r = c.get("/api/proxies/managed/status").json()
    assert r["billing"] == "off" and r["affordable"] is True


def test_status_affordability_tracks_balance(pool, subs, ledger):
    ledger.grant_pack("default", 500, ref="o1")
    _stock(pool, country="US")
    c = _client({"enabled": True, "provider": "stock",
                 "pricing": {"by_kind": {"isp": 300, "mobile": 900}}})
    assert c.get("/api/proxies/managed/status").json()["affordable"] is True
    r = c.get("/api/proxies/managed/status", params={"kind": "mobile"}).json()
    assert r["price_tokens"] == 900 and r["affordable"] is False


# ── 8) 路由：开通——**钱的路径** ─────────────────────────────────────────────

def _prov_client(pool, price=300):
    return _client({"enabled": True, "provider": "stock", "default_kind": "isp",
                    "default_country": "US", "period_days": 30,
                    "pricing": {"by_kind": {"isp": price}}})


def test_provision_happy_path_charges_once(pool, subs, ledger):
    ledger.grant_pack("default", 1000, ref="o1")
    [pid] = _stock(pool, country="JP")
    r = _prov_client(pool).post("/api/proxies/managed/provision",
                                json={"phone": "+819012345678", "request_id": "r1"}).json()
    assert r["ok"] is True and r["proxy"]["proxy_id"] == pid
    assert r["tokens"] == 300 and r["balance"] == 700
    assert _spent(ledger) == 300
    assert subs.by_proxy_id(pid)["status"] == STATUS_ACTIVE


def test_double_click_buys_one_proxy_and_charges_once(pool, subs, ledger):
    """**最重要的一条**：连点两下 → 同一条代理、只扣一次。

    ``record_spend`` 按 (钱包,日,动作) 累加、没有幂等键，所以这里断言的是账本
    **总额**而不只是「第二次返回了 replay」——后者证明不了钱没被扣第二遍。
    """
    ledger.grant_pack("default", 1000, ref="o1")
    _stock(pool, country="JP", n=3)
    c = _prov_client(pool)
    body = {"phone": "+819012345678", "request_id": "same-click"}
    a = c.post("/api/proxies/managed/provision", json=body).json()
    b = c.post("/api/proxies/managed/provision", json=body).json()
    assert a["ok"] and b["ok"] and b["replay"] is True
    assert a["proxy"]["proxy_id"] == b["proxy"]["proxy_id"]
    assert _spent(ledger) == 300
    assert len(subs.list_active()) == 1


def test_insufficient_balance_never_touches_stock(pool, subs, ledger):
    """余额不够必须**在要货之前**拒绝——不能出现「货领了钱不够」。"""
    ledger.grant_pack("default", 100, ref="o1")
    [pid] = _stock(pool, country="JP")
    r = _prov_client(pool).post("/api/proxies/managed/provision",
                                json={"phone": "+819012345678"}).json()
    assert r["ok"] is False and r["error"] == "insufficient_tokens"
    assert (r["need"], r["balance"]) == (300, 100)
    assert _spent(ledger) == 0
    assert pool.get(pid)["assigned"] is False  # 库存一点没动
    assert subs.list_active() == []


def test_out_of_stock_costs_nothing_and_is_retryable(pool, subs, ledger):
    """开通失败 = 交易从未发生：不扣钱、占坑释放、同键可原样重试。"""
    ledger.grant_pack("default", 1000, ref="o1")
    c = _prov_client(pool)
    body = {"phone": "+819012345678", "request_id": "r1"}
    r = c.post("/api/proxies/managed/provision", json=body).json()
    assert r["ok"] is False and r["error"] == "out_of_stock"
    assert _spent(ledger) == 0 and subs.by_charge_ref("pxsub:default:r1") is None

    _stock(pool, country="JP")  # 运营补货后，同一个键必须还能用
    r2 = c.post("/api/proxies/managed/provision", json=body).json()
    assert r2["ok"] is True and _spent(ledger) == 300


def test_zero_price_region_rejected_not_free(pool, subs, ledger):
    """默认档配了价、所选类型没配 → 仍然拒绝，绝不按 0 元发货。"""
    ledger.grant_pack("default", 1000, ref="o1")
    _stock(pool, country="JP", kind="mobile")
    c = _client({"enabled": True, "provider": "stock",
                 "pricing": {"by_kind": {"isp": 300}}})  # mobile 未配且无 default
    r = c.post("/api/proxies/managed/provision",
               json={"phone": "+819012345678", "kind": "mobile"}).json()
    assert r["ok"] is False and r["reason"] == "no_pricing"
    assert _spent(ledger) == 0


def test_provision_disabled_returns_unavailable(pool, subs):
    r = _client({"enabled": False}).post(
        "/api/proxies/managed/provision", json={}).json()
    assert r["ok"] is False and r["error"] == "unavailable"


def test_provision_without_account_key_holds_stock(pool, subs, ledger):
    """连号弹层里账号还没建 → 用订阅号占位持有，防并发把同一条库存卖两次。"""
    ledger.grant_pack("default", 1000, ref="o1")
    [pid] = _stock(pool, country="JP")
    c = _prov_client(pool)
    a = c.post("/api/proxies/managed/provision",
               json={"phone": "+819012345678", "request_id": "r1"}).json()
    assert a["ok"] and pool.get(pid)["assigned"] is True

    # 第二个用户（不同 request_id）不该拿到同一条
    b = c.post("/api/proxies/managed/provision",
               json={"phone": "+819012345678", "request_id": "r2"}).json()
    assert b["ok"] is False and b["error"] == "out_of_stock"
    assert _spent(ledger) == 300


def test_provision_binds_to_account_when_known(pool, subs, ledger):
    ledger.grant_pack("default", 1000, ref="o1")
    [pid] = _stock(pool, country="JP")
    r = _prov_client(pool).post("/api/proxies/managed/provision", json={
        "phone": "+819012345678", "account_key": "tg:123", "request_id": "r1"}).json()
    assert r["ok"] and pool.get(pid)["assigned_account"] == "tg:123"


def test_billing_off_provisions_free_and_marks_note(pool, subs):
    """账本未启用 → 不扣费照常开通，订阅行如实标 billing_off（对账时一眼看出）。"""
    [pid] = _stock(pool, country="JP")
    r = _prov_client(pool).post("/api/proxies/managed/provision",
                                json={"phone": "+819012345678"}).json()
    assert r["ok"] is True and r["tokens"] == 0 and r["price_tokens"] == 300
    assert subs.by_proxy_id(pid)["note"] == "billing_off"


def test_mock_provider_adds_new_proxy_row(pool, subs, ledger):
    """非库存供给方：开通结果要真的落进代理池并绑定。"""
    ledger.grant_pack("default", 1000, ref="o1")
    c = _client({"enabled": True, "provider": "mock", "default_country": "JP",
                 "pricing": {"by_kind": {"isp": 300}}})
    r = c.post("/api/proxies/managed/provision", json={"request_id": "r1"}).json()
    assert r["ok"] is True and r["country"] == "JP"
    assert len(pool.list()) == 1 and pool.list()[0]["assigned"] is True


# ── 9) 定额扣费入口本身 ──────────────────────────────────────────────────────

def test_record_fixed_spend_does_not_consult_rate_table(ledger):
    """定额扣费不查 TOKEN_RATES——代理价格随供应商浮动，不该污染那张金标表。"""
    assert record_fixed_spend("w", MANAGED_SPEND_ACTION, 777) == 777
    assert record_fixed_spend("w", MANAGED_SPEND_ACTION, 0) == 0
    assert record_fixed_spend("w", MANAGED_SPEND_ACTION, -5) == 0
    assert _spent(ledger, "w") == 777
