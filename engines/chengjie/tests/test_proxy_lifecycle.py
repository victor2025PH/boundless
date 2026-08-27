# -*- coding: utf-8 -*-
"""一键代理 P2 门禁——生命周期（续期/到期/低库存）+ 开通即体检 + 免费换货。

与 P1（test_proxy_managed.py）同一哲学：**钱的路径占最重的门禁**。P2 新增的
钱事故形态是「续期」：巡检重入/进程重启会不会把同一期扣两次（CAS 幂等）、
余额不够会不会先延期再欠着（必须不延期）、扣费失败会不会把服务掐掉（必须
先交付后扣费、留 unbilled 待对账）。

体检/换货侧的硬不变量：
1. stock 验活失败自动换下一条，坏的进健康状态机（fail），绝不原地打转；
2. 全部验不过 → 诚实失败**不扣费**、占坑释放（同键可重试）；
3. 换货零扣费、受窗口与次数双限；旧代理标 fail 不会被自己换回来。
"""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations import proxy_pool as pp_mod
from src.integrations import proxy_subscription as sub_mod
from src.integrations.proxy_lifecycle import (
    KIND_EXPIRED,
    KIND_EXPIRING,
    KIND_RENEW_BLOCKED,
    KIND_RENEW_UNBILLED,
    RENEW_GRACE_SEC,
    collect_low_stock,
    run_lifecycle_sweep,
)
from src.integrations.proxy_pool import ProbeResult, ProxyPool, parse_import_lines
from src.integrations.proxy_provider import (
    HttpProxyProvider,
    normalize_country_any,
    parse_managed_cfg,
    reset_http_probe_cache,
)
from src.integrations.proxy_subscription import (
    RENEW_PENDING,
    RENEW_UNBILLED,
    ProxySubscriptionStore,
)
from src.licensing.token_ledger import (
    TokenLedgerStore,
    configure_token_ledger,
    reset_token_ledger,
)
from src.web.routes.unified_inbox_proxy_routes import (
    MANAGED_SPEND_ACTION,
    register_proxy_fingerprint_routes,
)

DAY = 86400.0
NOW = 1_800_000_000.0


# ── 夹具（与 P1 同款隔离：绝不碰生产库）─────────────────────────────────────

@pytest.fixture
def pool(tmp_path, monkeypatch):
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
    reset_token_ledger()
    store = TokenLedgerStore(":memory:")
    configure_token_ledger(store=store, enabled=True)
    yield store
    reset_token_ledger()


def _mcfg(**over):
    base = {
        "enabled": True,
        "provider": "stock",
        "default_kind": "isp",
        "default_country": "US",
        "period_days": 30,
        "expiry_warn_days": 5,
        "pricing": {"by_kind": {"isp": 300}},
        "lifecycle": {"renew_before_days": 3, "min_stock": 2},
    }
    base.update(over)
    return parse_managed_cfg({"proxies": {"managed": base}})


def _active_sub(subs, pool, *, end, auto_renew, country="JP", kind="isp",
                wallet="default", charged=True, provider="stock",
                order_ref=""):
    """造一条已激活订阅 + 池内已绑定代理（生命周期用例的标准起点）。"""
    e = pool.add(scheme="socks5", host=f"10.9.{int(end) % 250}.{len(pool.list()) + 1}",
                 port=1080, kind=kind, country=country)
    pid = e["proxy_id"]
    row = subs.reserve(charge_ref=f"ref-{pid}", wallet=wallet, provider=provider,
                       country=country, kind=kind, tokens=300,
                       auto_renew=auto_renew, now=NOW - 10 * DAY)
    subs.activate(row["sub_id"], proxy_id=pid, order_ref=order_ref,
                  period_end=end, charged=charged, now=NOW - 10 * DAY)
    pool.assign(pid, "acct:x", exclusive=False)
    return subs.get(row["sub_id"]), pid


def _sweep(subs, pool, mcfg, *, balance=None, charge=None, prolong=None,
           now=NOW):
    calls = []

    def _charge(wallet, tokens):
        calls.append((wallet, tokens))
        return tokens if charge is None else charge(wallet, tokens)

    summary = run_lifecycle_sweep(
        store=subs, pool=pool, mcfg=mcfg,
        balance_fn=(lambda w: balance) if not callable(balance) else balance,
        charge_fn=_charge, prolong_fn=prolong, now=now)
    summary["_charge_calls"] = calls
    return summary


# ── 1) 台账：迁移 + CAS 续期幂等 ─────────────────────────────────────────────

def test_migration_backfills_new_columns_on_old_db(tmp_path):
    """比 P2 早建的库（P1 已装载生产）打开即补列，旧行零丢失。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE proxy_subscriptions ("
        " sub_id TEXT PRIMARY KEY, charge_ref TEXT NOT NULL UNIQUE,"
        " wallet TEXT NOT NULL DEFAULT '', account_key TEXT NOT NULL DEFAULT '',"
        " proxy_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',"
        " order_ref TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT '', tokens INTEGER NOT NULL DEFAULT 0,"
        " status TEXT NOT NULL DEFAULT 'reserved', charged INTEGER NOT NULL DEFAULT 0,"
        " auto_renew INTEGER NOT NULL DEFAULT 0, renew_count INTEGER NOT NULL DEFAULT 0,"
        " period_start REAL NOT NULL DEFAULT 0, period_end REAL NOT NULL DEFAULT 0,"
        " created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,"
        " note TEXT NOT NULL DEFAULT '')")
    conn.execute(
        "INSERT INTO proxy_subscriptions (sub_id, charge_ref, status) "
        "VALUES ('pxs_old', 'ref_old', 'active')")
    conn.commit()
    conn.close()

    store = ProxySubscriptionStore(db)
    row = store.get("pxs_old")
    assert row is not None
    assert row["renew_state"] == "" and row["swap_count"] == 0
    assert float(row["expiry_warned_at"]) == 0.0


def test_renew_cas_wins_exactly_once_per_period(subs, pool):
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)
    first = subs.renew_cas(sub["sub_id"], expected_end=NOW + DAY,
                           new_end=NOW + 31 * DAY, now=NOW)
    assert first is not None and first["renew_state"] == RENEW_PENDING
    assert first["renew_count"] == 1
    # 同一期第二次 CAS（巡检重入/进程重启重跑）必须输：这就是「绝不双扣」的闸
    again = subs.renew_cas(sub["sub_id"], expected_end=NOW + DAY,
                           new_end=NOW + 31 * DAY, now=NOW)
    assert again is None
    assert subs.get(sub["sub_id"])["renew_count"] == 1


def test_finalize_renewal_settles_or_flags_unbilled(subs, pool):
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)
    subs.renew_cas(sub["sub_id"], expected_end=NOW + DAY,
                   new_end=NOW + 31 * DAY, now=NOW)
    subs.finalize_renewal(sub["sub_id"], tokens=300, unbilled=False, now=NOW)
    row = subs.get(sub["sub_id"])
    assert row["renew_state"] == "" and row["tokens"] == 600
    # unbilled 路径：钱没到账要**留痕**（pending_renewals 是对账 CLI 的输入）
    subs.renew_cas(sub["sub_id"], expected_end=NOW + 31 * DAY,
                   new_end=NOW + 61 * DAY, now=NOW)
    subs.finalize_renewal(sub["sub_id"], tokens=0, unbilled=True, now=NOW)
    row = subs.get(sub["sub_id"])
    assert row["renew_state"] == RENEW_UNBILLED
    assert [r["sub_id"] for r in subs.pending_renewals()] == [sub["sub_id"]]


# ── 2) 生命周期巡检：续期的钱路径 ────────────────────────────────────────────

def test_sweep_renews_due_sub_and_charges_once(subs, pool):
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s["renewed"] == 1 and s["renew_unbilled"] == 0
    assert s["_charge_calls"] == [("default", 300)]
    row = subs.get(sub["sub_id"])
    assert row["period_end"] == pytest.approx(NOW + 31 * DAY)
    assert row["renew_state"] == "" and row["tokens"] == 600
    # 立刻重跑（重入语义）：新到期已出窗，零动作零扣费
    s2 = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s2["renewed"] == 0 and s2["_charge_calls"] == []


def test_sweep_insufficient_balance_never_extends(subs, pool):
    """余额不够 → **不延期**（到期自然停）+ 提前量内轰一次；绝不先续再欠。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=10)
    assert s["renewed"] == 0 and s["renew_blocked"] == 1
    assert s["_charge_calls"] == []
    assert subs.get(sub["sub_id"])["period_end"] == pytest.approx(NOW + DAY)
    alert = next(a for a in s["alerts"] if a["kind"] == KIND_RENEW_BLOCKED)
    assert alert["reasons"] == {"insufficient": 1}
    assert alert["need_tokens"] == 300
    # 同一期第二轮不重复轰（expiry_warned_at 标记）
    s2 = _sweep(subs, pool, _mcfg(), balance=10)
    assert s2["renew_blocked"] == 0 and s2["alerts"] == []


def test_sweep_charge_failure_keeps_service_flags_unbilled(subs, pool):
    """扣费失败 → 服务照续（先交付后扣费）+ unbilled 留痕告警，绝不回滚延期。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=1000, charge=lambda w, t: 0)
    assert s["renewed"] == 1 and s["renew_unbilled"] == 1
    row = subs.get(sub["sub_id"])
    assert row["period_end"] == pytest.approx(NOW + 31 * DAY)
    assert row["renew_state"] == RENEW_UNBILLED
    assert any(a["kind"] == KIND_RENEW_UNBILLED for a in s["alerts"])


def test_sweep_billing_off_renews_free(subs, pool):
    """未启用账本（balance_fn→None）＝免扣续期，与首购 billing=off 同口径。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=None)
    assert s["renewed"] == 1 and s["_charge_calls"] == []
    row = subs.get(sub["sub_id"])
    assert row["renew_state"] == "" and row["tokens"] == 300  # 没多记一分钱


def test_sweep_no_pricing_blocks_renewal_not_free_ride(subs, pool):
    """价格档被下架 → 绝不按 0 元白续，也绝不扣一个未知数；预警后到期正常回收。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True, kind="mobile")
    s = _sweep(subs, pool, _mcfg(), balance=1000)  # pricing 只配了 isp
    assert s["renewed"] == 0 and s["renew_blocked"] == 1
    alert = next(a for a in s["alerts"] if a["kind"] == KIND_RENEW_BLOCKED)
    assert alert["reasons"] == {"no_pricing": 1}
    assert subs.get(sub["sub_id"])["period_end"] == pytest.approx(NOW + DAY)


# ── 3) 生命周期巡检：到期回收 / 宽限 / 预警 ──────────────────────────────────

def test_sweep_expires_and_returns_stock(subs, pool):
    sub, pid = _active_sub(subs, pool, end=NOW - 3600, auto_renew=False)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s["expired"] == 1
    assert subs.get(sub["sub_id"])["status"] == "expired"
    entry = pool.get(pid)
    assert entry["assigned"] is False  # 解绑归还库存，可再售
    alert = next(a for a in s["alerts"] if a["kind"] == KIND_EXPIRED)
    assert alert["countries"] == {"JP": 1}
    # 重跑幂等：不再重复回收/重复告警
    s2 = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s2["expired"] == 0 and s2["alerts"] == []


def test_sweep_grace_renews_recently_expired_auto_renew(subs, pool):
    """巡检器宕机不连坐用户：auto_renew 过期未超宽限仍续上，且从旧到期起算。"""
    sub, _ = _active_sub(subs, pool, end=NOW - 3600, auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s["expired"] == 0 and s["renewed"] == 1
    assert subs.get(sub["sub_id"])["period_end"] == pytest.approx(
        NOW - 3600 + 30 * DAY)


def test_sweep_grace_exhausted_expires_even_with_auto_renew(subs, pool):
    sub, pid = _active_sub(subs, pool, end=NOW - RENEW_GRACE_SEC - 3600,
                           auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s["expired"] == 1 and s["renewed"] == 0
    assert pool.get(pid)["assigned"] is False


def test_sweep_warns_no_autorenew_once_per_period(subs, pool):
    sub, _ = _active_sub(subs, pool, end=NOW + 2 * DAY, auto_renew=False)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s["warned"] == 1
    alert = next(a for a in s["alerts"] if a["kind"] == KIND_EXPIRING)
    assert alert["count"] == 1 and alert["subs"][0]["country"] == "JP"
    assert _sweep(subs, pool, _mcfg(), balance=1000)["warned"] == 0


def test_sweep_leaves_far_future_subs_alone(subs, pool):
    _active_sub(subs, pool, end=NOW + 20 * DAY, auto_renew=True)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert (s["renewed"], s["expired"], s["warned"], s["alerts"]) == (0, 0, 0, [])


# ── 4) 低库存水位 ────────────────────────────────────────────────────────────

def test_low_stock_uses_watchlist_and_threshold(pool):
    pool.add(scheme="socks5", host="10.1.0.1", port=1080, kind="isp", country="JP")
    m = _mcfg(allow_countries=["JP", "US"], lifecycle={"min_stock": 2})
    assert collect_low_stock(pool=pool, mcfg=m) == {"JP": 1, "US": 0}
    # 阈值满足后不再点名
    pool.add(scheme="socks5", host="10.1.0.2", port=1080, kind="isp", country="JP")
    assert collect_low_stock(pool=pool, mcfg=m) == {"US": 0}


def test_low_stock_silent_when_not_stock_or_disabled(pool):
    m = _mcfg(provider="mock", lifecycle={"min_stock": 2})
    assert collect_low_stock(pool=pool, mcfg=m) == {}
    m2 = _mcfg(lifecycle={"min_stock": 0})
    assert collect_low_stock(pool=pool, mcfg=m2) == {}


# ── 5) 路由：开通即体检（stock 换挑 / 全败退款位）───────────────────────────

def _client(pool, cfg_over=None):
    cfg = {"enabled": True, "provider": "stock", "default_kind": "isp",
           "default_country": "US", "period_days": 30,
           "swap_window_days": 3, "max_swaps": 1,
           "pricing": {"by_kind": {"isp": 300}}}
    cfg.update(cfg_over or {})
    app = FastAPI()
    register_proxy_fingerprint_routes(app, api_auth=lambda request: True)
    app.state.config_manager = SimpleNamespace(
        config={"proxies": {"managed": cfg}})
    return TestClient(app)


def _fake_probe(ok_hosts):
    async def probe(entry):
        return ProbeResult(ok=entry["host"] in ok_hosts,
                           error="" if entry["host"] in ok_hosts else "refused")
    return probe


def test_provision_verify_rotates_to_healthy_stock(pool, subs, ledger):
    ledger.grant_pack("default", 1000, ref="o1")
    bad = pool.add(scheme="socks5", host="10.2.0.1", port=1080,
                   kind="isp", country="JP")
    good = pool.add(scheme="socks5", host="10.2.0.2", port=1080,
                    kind="isp", country="JP")
    pool._default_probe = _fake_probe({"10.2.0.2"})
    r = _client(pool).post("/api/proxies/managed/provision", json={
        "phone": "+819012345678", "request_id": "r1"}).json()
    assert r["ok"] is True and r["verified"] is True
    assert r["proxy"]["proxy_id"] == good["proxy_id"]
    # 坏的那条进了健康状态机（下次挑选自动跳过），好的被绑定
    assert pool.get(bad["proxy_id"])["status"] in ("fail", "cooldown")
    assert ledger.usage("default")["by_action"][MANAGED_SPEND_ACTION] == 300


def test_provision_all_unhealthy_fails_without_charge(pool, subs, ledger):
    """全部验不过 → 诚实失败：零扣费 + 占坑释放（补货后同键可原样重试）。"""
    ledger.grant_pack("default", 1000, ref="o1")
    for i in range(2):
        pool.add(scheme="socks5", host=f"10.3.0.{i + 1}", port=1080,
                 kind="isp", country="JP")
    pool._default_probe = _fake_probe(set())
    c = _client(pool)
    body = {"phone": "+819012345678", "request_id": "r-heal"}
    r = c.post("/api/proxies/managed/provision", json=body).json()
    assert r["ok"] is False and r["error"] == "no_healthy_stock"
    assert ledger.usage("default")["by_action"].get(MANAGED_SPEND_ACTION, 0) == 0
    # 补一条健康货 → 同一个 request_id 直接重试成功（占坑真的释放了）
    pool.add(scheme="socks5", host="10.3.0.9", port=1080, kind="isp", country="JP")
    pool._default_probe = _fake_probe({"10.3.0.9"})
    r2 = c.post("/api/proxies/managed/provision", json=body).json()
    assert r2["ok"] is True and r2["proxy"]["host"].endswith(".9")


def test_provision_verify_off_keeps_old_behavior(pool, subs, ledger):
    ledger.grant_pack("default", 1000, ref="o1")
    pool.add(scheme="socks5", host="10.4.0.1", port=1080, kind="isp", country="JP")
    pool._default_probe = _fake_probe(set())  # 就算探针全红
    r = _client(pool, {"verify_on_provision": False}).post(
        "/api/proxies/managed/provision",
        json={"phone": "+819012345678", "request_id": "r1"}).json()
    assert r["ok"] is True and r["verified"] is None


# ── 6) 路由：免费换货 ────────────────────────────────────────────────────────

def _provisioned(pool, ledger, c=None):
    ledger.grant_pack("default", 1000, ref="o1")
    pool.add(scheme="socks5", host="10.5.0.1", port=1080, kind="isp", country="JP")
    pool.add(scheme="socks5", host="10.5.0.2", port=1080, kind="isp", country="JP")
    c = c or _client(pool, {"verify_on_provision": False})
    r = c.post("/api/proxies/managed/provision", json={
        "phone": "+819012345678", "request_id": "r1"}).json()
    assert r["ok"] is True
    return c, r["subscription"]["sub_id"], r["proxy"]["proxy_id"]


def test_swap_is_free_and_retires_old_proxy(pool, subs, ledger):
    c, sub_id, old_pid = _provisioned(pool, ledger)
    r = c.post("/api/proxies/managed/swap", json={"sub_id": sub_id}).json()
    assert r["ok"] is True and r["proxy"]["proxy_id"] != old_pid
    old = pool.get(old_pid)
    assert old["status"] == "fail" and old["assigned"] is False
    row = subs.get(sub_id)
    assert row["swap_count"] == 1 and row["proxy_id"] == r["proxy"]["proxy_id"]
    # 零扣费：账本只有首购那一笔
    assert ledger.usage("default")["by_action"][MANAGED_SPEND_ACTION] == 300
    assert row["tokens"] == 300


def test_swap_respects_cap_and_window(pool, subs, ledger):
    c, sub_id, _ = _provisioned(pool, ledger)
    pool.add(scheme="socks5", host="10.5.0.3", port=1080, kind="isp", country="JP")
    assert c.post("/api/proxies/managed/swap", json={"sub_id": sub_id}).json()["ok"]
    r = c.post("/api/proxies/managed/swap", json={"sub_id": sub_id}).json()
    assert r["ok"] is False and r["error"] == "swap_limit_reached"  # max_swaps=1
    # 窗口关闭：把开通时间拨回 10 天前
    with subs._lock:
        subs._conn.execute(
            "UPDATE proxy_subscriptions SET period_start=?, swap_count=0 "
            "WHERE sub_id=?", (time.time() - 10 * DAY, sub_id))
        subs._conn.commit()
    r2 = c.post("/api/proxies/managed/swap", json={"sub_id": sub_id}).json()
    assert r2["ok"] is False and r2["error"] == "swap_window_closed"


def test_swap_unknown_sub_rejected(pool, subs):
    r = _client(pool).post("/api/proxies/managed/swap",
                           json={"sub_id": "pxs_nope"}).json()
    assert r["ok"] is False and r["error"] == "sub_not_found"


# ── 7) 路由：运维总览 ────────────────────────────────────────────────────────

def test_overview_reports_even_when_disabled(pool, subs, ledger):
    """功能被临时关掉 ≠ 运营看不见还在计时的订阅（台账数据照出）。"""
    c, sub_id, _ = _provisioned(pool, ledger)
    off = _client(pool, {"enabled": False})
    r = off.get("/api/proxies/managed/overview").json()
    assert r["ok"] is True and r["capability"]["available"] is False
    assert r["stats"]["active"] == 1


def test_overview_surfaces_pending_renewals_and_low_stock(pool, subs, ledger):
    c, sub_id, _ = _provisioned(pool, ledger)
    row = subs.get(sub_id)
    subs.renew_cas(sub_id, expected_end=row["period_end"],
                   new_end=row["period_end"] + 30 * DAY)
    r = _client(pool, {"allow_countries": ["JP", "US"],
                       "lifecycle": {"min_stock": 5}}).get(
        "/api/proxies/managed/overview").json()
    assert r["stats"]["renew_pending"] == 1
    assert [p["sub_id"] for p in r["renew_pending"]] == [sub_id]
    assert r["low_stock"].get("US") == 0 and r["min_stock"] == 5


# ── 8) watchdog 接线：稀疏节流 + 低库存签名去重 + 发布 ───────────────────────

def _wd(cfg_managed):
    from src.inbox.health_watchdog import HealthWatchdog

    cm = SimpleNamespace(config={"proxies": {"managed": cfg_managed}})
    return HealthWatchdog(app=SimpleNamespace(state=SimpleNamespace()),
                          config_manager=cm)


def test_watchdog_publishes_lifecycle_alerts(subs, pool, monkeypatch):
    _active_sub(subs, pool, end=NOW + 2 * DAY, auto_renew=False)
    published = []

    class _Bus:
        def publish(self, etype, payload):
            published.append((etype, payload))

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    reset_token_ledger()  # billing off → 巡检不会碰真账本

    wd = _wd({"enabled": True, "provider": "stock",
              "pricing": {"by_kind": {"isp": 300}},
              "allow_countries": ["JP"], "lifecycle": {"min_stock": 5}})
    wd._check_proxy_managed(now=NOW)
    kinds = sorted(p["kind"] for _, p in published)
    assert kinds == [KIND_EXPIRING, "low_stock"]
    assert all(t == "proxy_managed_alert" for t, _ in published)

    # 节流：间隔内第二次 tick 静默
    wd._check_proxy_managed(now=NOW + 60)
    assert len(published) == 2
    # 低库存签名没变：过节流窗后也不复读（4h 重提之前）
    wd._pxm_last_ts = 0.0
    wd._check_proxy_managed(now=NOW + 3700)
    assert sorted(p["kind"] for _, p in published) == [KIND_EXPIRING, "low_stock"]


def test_watchdog_silent_when_disabled(subs, pool, monkeypatch):
    published = []

    class _Bus:
        def publish(self, etype, payload):
            published.append(etype)

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    wd = _wd({"enabled": False})
    wd._check_proxy_managed(now=NOW)
    assert published == [] and wd._pxm_last_ts == 0.0


# ── 9) webhook 文案：五类 kind 都有人话标题 ──────────────────────────────────

@pytest.mark.parametrize("payload,frag", [
    ({"kind": "expiring", "count": 2,
      "subs": [{"country": "JP", "kind": "isp", "remaining_days": 2}]}, "即将到期"),
    ({"kind": "renew_blocked", "count": 1, "need_tokens": 300,
      "reasons": {"insufficient": 1}, "subs": []}, "续不了"),
    ({"kind": "renew_unbilled", "count": 1, "subs": []}, "待对账"),
    ({"kind": "expired", "count": 1, "countries": {"JP": 1}, "subs": []}, "到期回收"),
    ({"kind": "low_stock", "low": {"JP": 0}, "min_stock": 2}, "库存告急"),
])
def test_build_message_proxy_managed_kinds(payload, frag):
    from src.inbox.webhook_notifier import _build_message

    title, text = _build_message("proxy_managed_alert", payload)
    assert frag in title
    assert "/admin/ops" in text


# ── 10) P3：库存批量导入（parse 纯函数 + 路由 + 空库存禁售）──────────────────

def test_parse_import_lines_accepts_vendor_formats():
    text = (
        "# 供应商导出\n"
        "1.2.3.4:1080\n"
        "5.6.7.8:2080:u1:p1\n"
        "socks5://u2:p2@9.9.9.9:9999\n"
        "http://10.0.0.1:8080\n"
        "1.2.3.4:1080\n"          # 批内重复 → 静默跳过
        "bad:line:x\n"            # 3 段 → bad_format
        "7.7.7.7:notaport\n"      # bad_port
        "ftp://1.1.1.1:21\n"      # bad_scheme
    )
    entries, bad = parse_import_lines(text)
    assert [(e["host"], e["port"]) for e in entries] == [
        ("1.2.3.4", 1080), ("5.6.7.8", 2080), ("9.9.9.9", 9999),
        ("10.0.0.1", 8080)]
    assert entries[1]["username"] == "u1" and entries[1]["password"] == "p1"
    assert entries[2]["scheme"] == "socks5" and entries[2]["username"] == "u2"
    assert entries[3]["scheme"] == "http"
    assert entries[0]["scheme"] == "socks5"  # 默认协议
    assert sorted(b["reason"] for b in bad) == [
        "bad_format", "bad_port", "bad_scheme"]
    assert all(b["line"] > 0 for b in bad)


def test_import_route_tags_batch_and_dedupes(pool, subs):
    c = _client(pool)
    body = {"text": "1.2.3.4:1080\n5.6.7.8:2080:u:p", "country": "jp",
            "kind": "isp", "label": "zoo-8月批"}
    r = c.post("/api/proxies/import", json=body).json()
    assert r["ok"] is True and r["added"] == 2 and r["dup"] == 0
    rows = pool.list()
    assert all(p["country"] == "JP" and p["kind"] == "isp" for p in rows)
    # 同一份清单再粘一遍 → 全部按池内重复跳过，一条不重录
    r2 = c.post("/api/proxies/import", json=body).json()
    assert r2["added"] == 0 and r2["dup"] == 2
    # 超量护栏
    big = "\n".join(f"10.0.{i // 250}.{i % 250}:1080" for i in range(501))
    r3 = c.post("/api/proxies/import", json={"text": big}).json()
    assert r3["ok"] is False and r3["error"] == "too_many"


def test_status_empty_stock_blocks_order(pool, subs):
    """空池＝真缺货必须禁售（P3 修正：旧「空=不支持盘点」放行＝死路按钮）。"""
    c = _client(pool)
    r = c.get("/api/proxies/managed/status").json()
    assert r["available"] is True and r["in_stock"] is False
    # 灌一条 JP 货：默认地区（US）仍缺货，显式选 JP 即可售
    pool.add(scheme="socks5", host="10.8.0.1", port=1080, kind="isp", country="JP")
    assert c.get("/api/proxies/managed/status").json()["in_stock"] is False
    r2 = c.get("/api/proxies/managed/status", params={"country": "JP"}).json()
    assert r2["in_stock"] is True


def test_status_non_stock_provider_not_blocked_by_inventory(pool, subs):
    """http/mock 上游现开现给没有本地库存概念——不得因盘点为空而禁售。"""
    r = _client(pool, {"provider": "mock"}).get(
        "/api/proxies/managed/status").json()
    assert r["available"] is True and r["in_stock"] is True


# ── 11) P3：HttpProxyProvider 两步式上游 + 幂等键透传 ────────────────────────

def _two_step_cfg():
    return {
        "url": "https://api.vendor.example/orders",
        "body": {"type": "isp", "country": "{country}",
                 "idempotency_key": "{idempotency_key}"},
        "map": {"order_ref": "id"},
        "credentials": {
            "url": "https://api.vendor.example/orders/{order_ref}/proxies",
            "map": {"host": "proxies.0.ip", "port": "proxies.0.port",
                    "username": "proxies.0.login",
                    "password": "proxies.0.password"},
        },
    }


async def test_http_provider_two_step_fetches_credentials(monkeypatch):
    cfg = _two_step_cfg()
    calls = []

    async def fake_request(self, sess, section, repl, *, default_method):
        calls.append((section.get("url"), dict(repl), default_method))
        if section is cfg:
            return {"id": "ord9"}
        return {"proxies": [{"ip": "9.9.9.9", "port": 9999,
                             "login": "u", "password": "pw"}]}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    res = await HttpProxyProvider(cfg).provision(
        country="JP", kind="isp", period_days=30,
        idempotency_key="pxsub:default:r1")
    assert res.ok and (res.host, res.port) == ("9.9.9.9", 9999)
    assert (res.username, res.password) == ("u", "pw")
    assert res.order_ref == "ord9" and res.country == "JP"
    # 第一击带幂等键；第二击带 order_ref 且默认 GET
    assert calls[0][1]["idempotency_key"] == "pxsub:default:r1"
    assert calls[1][1]["order_ref"] == "ord9" and calls[1][2] == "GET"


async def test_http_provider_single_step_skips_credentials_call(monkeypatch):
    cfg = _two_step_cfg()
    cfg["map"] = {"host": "ip", "port": "port", "order_ref": "id"}
    calls = []

    async def fake_request(self, sess, section, repl, *, default_method):
        calls.append(section.get("url"))
        return {"id": "o1", "ip": "8.8.8.8", "port": 1080}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    res = await HttpProxyProvider(cfg).provision(
        country="US", kind="isp", period_days=30)
    assert res.ok and res.host == "8.8.8.8"
    assert len(calls) == 1  # 主响应已带凭据 → 绝不多打一次上游


async def test_http_provider_bad_payload_without_credentials_cfg(monkeypatch):
    cfg = _two_step_cfg()
    cfg.pop("credentials")

    async def fake_request(self, sess, section, repl, *, default_method):
        return {"id": "o2"}  # 只有订单号、没有凭据、也没配第二步

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    res = await HttpProxyProvider(cfg).provision(
        country="US", kind="isp", period_days=30)
    assert res.ok is False and res.error == "upstream_bad_payload"
    assert res.order_ref == "o2"  # 订单号带回：上游可能已计费，对账要用


# ── 12) P4（JIT 即买即用）：上游余量 / 余额 / 内部 ID 映射 ───────────────────

def test_normalize_country_any_handles_vendor_codes():
    assert normalize_country_any("jp") == "JP"
    assert normalize_country_any("JPN") == "JP"      # Proxy-Seller 回 alpha3
    assert normalize_country_any("FRA") == "FR"
    assert normalize_country_any("XXX") == ""        # 表外码丢弃不猜
    assert normalize_country_any("") == ""
    assert normalize_country_any("JAPAN") == ""


async def test_http_inventory_catalog_mode_and_counts(monkeypatch):
    """count 缺省＝目录级（-1 有货不给数）；配了 count＝精确存量；alpha3 归一。"""
    reset_http_probe_cache()
    cfg = {"url": "https://v1.example/order",
           "inventory": {"url": "https://v1.example/ref", "items": "data.items",
                         "country": "alpha3"}}

    async def fake_request(self, sess, section, repl, *, default_method):
        return {"data": {"items": [
            {"alpha3": "JPN"}, {"alpha3": "THA"}, {"alpha3": "XXX"}]}}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    inv = await HttpProxyProvider(cfg).inventory_async()
    assert inv == {"JP": -1, "TH": -1}  # 表外码 XXX 被丢弃

    reset_http_probe_cache()
    cfg2 = {"url": "https://v2.example/order",
            "inventory": {"url": "https://v2.example/avail", "items": "data",
                          "country": "cc", "count": "n"}}

    async def fake_request2(self, sess, section, repl, *, default_method):
        return {"data": [{"cc": "JP", "n": 12}, {"cc": "US", "n": 0}]}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request2)
    assert await HttpProxyProvider(cfg2).inventory_async() == {"JP": 12, "US": 0}


async def test_http_inventory_ttl_cache_protects_vendor(monkeypatch):
    """status 随卡片渲染高频轮询——TTL 窗内绝不重复打上游（上游有请求限频）。"""
    reset_http_probe_cache()
    calls = []
    cfg = {"url": "https://v3.example/order",
           "inventory": {"url": "https://v3.example/ref", "items": "data",
                         "country": "cc", "ttl_sec": 300}}

    async def fake_request(self, sess, section, repl, *, default_method):
        calls.append(1)
        return {"data": [{"cc": "JP"}]}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    p = HttpProxyProvider(cfg)
    assert await p.inventory_async() == {"JP": -1}
    assert await p.inventory_async() == {"JP": -1}
    assert len(calls) == 1  # 第二次走缓存
    reset_http_probe_cache()
    await p.inventory_async()
    assert len(calls) == 2


async def test_http_vendor_balance_reads_configured_field(monkeypatch):
    reset_http_probe_cache()
    cfg = {"url": "https://v4.example/order",
           "balance": {"url": "https://v4.example/balance",
                       "field": "data.balance", "min_alert": 10}}

    async def fake_request(self, sess, section, repl, *, default_method):
        return {"data": {"balance": "42.5"}}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    assert await HttpProxyProvider(cfg).vendor_balance() == 42.5
    # 未配置 → None（调用方据此不告警不显示）
    assert await HttpProxyProvider({"url": "x"}).vendor_balance() is None


async def test_http_provision_fills_vendor_internal_ids(monkeypatch):
    """Proxy-Seller 型厂商按内部 ID 下单：{country_id}/{period_id} 由静态映射推导。"""
    seen = {}
    cfg = {"url": "https://v5.example/order/make",
           "body": {"countryId": "{country_id}", "periodId": "{period_id}"},
           "map": {"host": "ip", "port": "port"},
           "country_ids": {"JP": 561}, "period_ids": {"30": "1m"}}

    async def fake_request(self, sess, section, repl, *, default_method):
        seen.update(repl)
        return {"ip": "9.9.9.9", "port": 1080}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    res = await HttpProxyProvider(cfg).provision(
        country="JP", kind="isp", period_days=30)
    assert res.ok
    assert seen["country_id"] == "561" and seen["period_id"] == "1m"


def test_status_http_inventory_gates_in_stock(pool, subs, monkeypatch):
    """JIT 上游给了目录 → 缺货地区照样禁售；-1（有货不给数）放行。"""
    async def fake_inv(self, *, kind=""):
        return {"JP": -1, "TH": 0}

    monkeypatch.setattr(HttpProxyProvider, "inventory_async", fake_inv)
    c = _client(pool, {"provider": "http",
                       "http": {"url": "https://v6.example/order"}})
    assert c.get("/api/proxies/managed/status",
                 params={"country": "JP"}).json()["in_stock"] is True
    assert c.get("/api/proxies/managed/status",
                 params={"country": "TH"}).json()["in_stock"] is False
    assert c.get("/api/proxies/managed/status",
                 params={"country": "US"}).json()["in_stock"] is False


def test_watchdog_vendor_balance_low_alert(subs, pool, monkeypatch):
    """JIT 世界里余额=库存：低于提醒线 → 告警；充值回升 → 自动清零不再提。"""
    published = []

    class _Bus:
        def publish(self, etype, payload):
            published.append(payload)

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    reset_token_ledger()

    bal_holder = {"v": 4.5}

    async def fake_balance(self):
        return bal_holder["v"]

    monkeypatch.setattr(HttpProxyProvider, "vendor_balance", fake_balance)
    wd = _wd({"enabled": True, "provider": "http",
              "pricing": {"by_kind": {"isp": 300}},
              "http": {"url": "https://v7.example/order",
                       "balance": {"url": "https://v7.example/bal",
                                   "min_alert": 10}}})
    wd._check_proxy_managed(now=NOW)
    assert [p["kind"] for p in published] == ["vendor_balance_low"]
    assert published[0]["balance"] == 4.5
    # 4h 内不重提（哪怕巡检节流被穿过）
    wd._pxm_last_ts = 0.0
    wd._check_proxy_managed(now=NOW + 3600)
    assert len(published) == 1
    # 充值回升 → 清零；再跌破 → 重新告警
    bal_holder["v"] = 50.0
    wd._pxm_last_ts = 0.0
    wd._check_proxy_managed(now=NOW + 7200)
    assert len(published) == 1 and wd._pxm_bal_alerted is False
    bal_holder["v"] = 3.0
    wd._pxm_last_ts = 0.0
    wd._check_proxy_managed(now=NOW + 10800)
    assert len(published) == 2


def test_diff_orders_flags_money_leaks():
    from tools.proxy_managed_review import diff_orders

    vendor = [{"order_ref": "A1", "cost": 4.0},
              {"order_ref": "A2", "cost": 2.5},
              {"order_ref": "A3", "cost": 1.0}]
    out = diff_orders(vendor, ["A1", "L9"])
    assert out["vendor_only"] == ["A2", "A3"]       # 上游有单我们没账=白付
    assert out["vendor_only_cost"] == 3.5
    assert out["local_only"] == ["L9"]              # 我们有账上游没单
    assert (out["vendor_total"], out["local_total"]) == (3, 2)


# ── 13) P4：上游续期直通 / 免费换 IP / 死 IP 隔离 ────────────────────────────

def test_sweep_http_sub_blocked_when_vendor_prolong_fails(subs, pool):
    """JIT 正确性：上游续不了 → 不延不扣（宁可不收「买到一段死期」的钱）。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True,
                         provider="http", order_ref="ord1")
    s = _sweep(subs, pool, _mcfg(), balance=1000,
               prolong=lambda ref, days, prov: (False, 0.0))
    assert s["renewed"] == 0 and s["renew_blocked"] == 1
    assert s["_charge_calls"] == []
    assert subs.get(sub["sub_id"])["period_end"] == pytest.approx(NOW + DAY)
    alert = next(a for a in s["alerts"] if a["kind"] == KIND_RENEW_BLOCKED)
    assert alert["reasons"] == {"vendor_prolong": 1}
    # 同一期只轰一次
    s2 = _sweep(subs, pool, _mcfg(), balance=1000,
                prolong=lambda ref, days, prov: (False, 0.0))
    assert s2["renew_blocked"] == 0 and s2["alerts"] == []


def test_sweep_vendor_expiry_wins_when_later(subs, pool):
    """上游回了权威到期 → 我们的账期跟上游（IP 的真实死期在上游时钟上）。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True,
                         provider="http", order_ref="ord2")
    vendor_end = NOW + 33 * DAY
    seen = []

    def _prolong(ref, days, prov):
        seen.append((ref, days, prov))
        return True, vendor_end

    s = _sweep(subs, pool, _mcfg(), balance=1000, prolong=_prolong)
    assert s["renewed"] == 1 and s["_charge_calls"] == [("default", 300)]
    assert subs.get(sub["sub_id"])["period_end"] == pytest.approx(vendor_end)
    assert seen == [("ord2", 30, "http")]  # 透传订阅行的 provider/order_ref


def test_sweep_stock_sub_passes_prolong_untouched(subs, pool):
    """stock 期旧订阅没有上游时钟：prolong_fn 放行（watchdog 注入的分流语义）。"""
    sub, _ = _active_sub(subs, pool, end=NOW + DAY, auto_renew=True)

    def _prolong(ref, days, prov):
        assert prov == "stock"
        return (True, 0.0) if prov != "http" else (False, 0.0)

    s = _sweep(subs, pool, _mcfg(), balance=1000, prolong=_prolong)
    assert s["renewed"] == 1
    assert subs.get(sub["sub_id"])["period_end"] == pytest.approx(NOW + 31 * DAY)


def test_sweep_expired_http_sub_quarantines_dead_ip(subs, pool):
    """http 单到期＝IP 在上游已作废 → 标 fail 防复卖；stock 自有货照常回池。"""
    sub_h, pid_h = _active_sub(subs, pool, end=NOW - 3600, auto_renew=False,
                               provider="http", order_ref="ord3")
    sub_s, pid_s = _active_sub(subs, pool, end=NOW - 3600, auto_renew=False)
    s = _sweep(subs, pool, _mcfg(), balance=1000)
    assert s["expired"] == 2
    assert pool.get(pid_h)["status"] == "fail"       # 死 IP 隔离
    assert pool.get(pid_s)["status"] != "fail"       # 自有货可复卖
    assert pool.get(pid_s)["assigned"] is False


async def test_http_prolong_unconfigured_blocks_and_assume_ok_passes():
    ok, _, reason = await HttpProxyProvider({"url": "x"}).prolong(
        order_ref="o1", period_days=30)
    assert ok is False and reason == "vendor_prolong_unconfigured"
    ok2, _, _ = await HttpProxyProvider(
        {"url": "x", "prolong": {"assume_ok": True}}).prolong(
        order_ref="o1", period_days=30)
    assert ok2 is True


async def test_http_prolong_calls_vendor_and_maps_expiry(monkeypatch):
    seen = {}
    cfg = {"url": "https://v8.example/order",
           "period_ids": {"30": "1m"},
           "prolong": {"url": "https://v8.example/prolong/{order_ref}",
                       "map": {"expires_at": "data.expires"}}}

    async def fake_request(self, sess, section, repl, *, default_method):
        seen.update(repl)
        seen["method"] = default_method
        return {"data": {"expires": 1_900_000_000}}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    ok, expires, reason = await HttpProxyProvider(cfg).prolong(
        order_ref="ord9", period_days=30)
    assert ok is True and expires == 1_900_000_000 and reason == ""
    assert seen["order_ref"] == "ord9" and seen["period_id"] == "1m"


async def test_http_replace_maps_credentials_and_keeps_order(monkeypatch):
    cfg = {"url": "https://v9.example/order",
           "replace": {"url": "https://v9.example/replace",
                       "map": {"host": "data.ip", "port": "data.port"}}}

    async def fake_request(self, sess, section, repl, *, default_method):
        return {"data": {"ip": "7.7.7.7", "port": 7777}}

    monkeypatch.setattr(HttpProxyProvider, "_request", fake_request)
    rep = await HttpProxyProvider(cfg).replace(order_ref="ordA", country="JP")
    assert rep is not None and rep.ok
    assert (rep.host, rep.port) == ("7.7.7.7", 7777)
    assert rep.order_ref == "ordA"  # 换 IP 不换单：对账口径还是同一张订单
    # 未配置 → None（调用方回落重新 provision）
    assert await HttpProxyProvider({"url": "x"}).replace(order_ref="o") is None


def test_swap_uses_vendor_replace_for_free(pool, subs, monkeypatch):
    """上游支持 replace → 换货零成本零新单；上游 replace 失败 → 如实报错不静默买新。"""
    sub, old_pid = _active_sub(subs, pool, end=NOW + 20 * DAY, auto_renew=True,
                               provider="http", order_ref="ordB")
    # period_start 在窗口内（_active_sub 造的是 10 天前 → 窗口 3 天已关）→ 拨新
    with subs._lock:
        subs._conn.execute(
            "UPDATE proxy_subscriptions SET period_start=? WHERE sub_id=?",
            (time.time() - 3600, sub["sub_id"]))
        subs._conn.commit()

    async def fake_replace(self, *, order_ref, country="", kind=""):
        from src.integrations.proxy_provider import ProvisionResult
        return ProvisionResult(ok=True, host="6.6.6.6", port=6666,
                               country=country or "JP", kind=kind or "isp",
                               order_ref=order_ref)

    monkeypatch.setattr(HttpProxyProvider, "replace", fake_replace)
    c = _client(pool, {"provider": "http", "verify_on_provision": False,
                       "max_swaps": 3,  # 本例要换两次（成功+失败路径）
                       "http": {"url": "https://vA.example/order"}})
    r = c.post("/api/proxies/managed/swap", json={"sub_id": sub["sub_id"]}).json()
    assert r["ok"] is True and r["proxy"]["host"] == "6.6.6.6"
    assert pool.get(old_pid)["status"] == "fail"
    assert subs.get(sub["sub_id"])["swap_count"] == 1

    async def fail_replace(self, *, order_ref, country="", kind=""):
        from src.integrations.proxy_provider import ProvisionResult
        return ProvisionResult(ok=False, error="vendor_replace_failed")

    monkeypatch.setattr(HttpProxyProvider, "replace", fail_replace)
    r2 = c.post("/api/proxies/managed/swap", json={"sub_id": sub["sub_id"]}).json()
    assert r2["ok"] is False and r2["error"] == "vendor_replace_failed"
    assert subs.get(sub["sub_id"])["swap_count"] == 1  # 失败不计次不换绑


def test_watchdog_wires_vendor_prolong(subs, pool, monkeypatch):
    """watchdog 注入的 prolong 分流：http 单真调上游、拿到 ok 才续。"""
    _active_sub(subs, pool, end=NOW + DAY, auto_renew=True,
                provider="http", order_ref="ordC")
    called = []

    async def fake_prolong(self, *, order_ref, period_days):
        called.append((order_ref, period_days))
        return True, 0.0, ""

    monkeypatch.setattr(HttpProxyProvider, "prolong", fake_prolong)

    class _Bus:
        def publish(self, etype, payload):
            pass

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    reset_token_ledger()  # billing off → 免扣续期
    wd = _wd({"enabled": True, "provider": "http",
              "pricing": {"by_kind": {"isp": 300}},
              "http": {"url": "https://vB.example/order"}})
    wd._check_proxy_managed(now=NOW)
    assert called == [("ordC", 30)]
    assert subs.get(subs.list_active()[0]["sub_id"])["renew_count"] == 1


# ── 14) 对账 CLI（只读）──────────────────────────────────────────────────────

def test_review_cli_collects_debts(tmp_path):
    from tools.proxy_managed_review import collect_review

    db = tmp_path / "config" / "proxy_subscriptions.db"
    store = ProxySubscriptionStore(db)
    a = store.reserve(charge_ref="r1", wallet="default", country="JP",
                      kind="isp", tokens=300, now=NOW)
    store.activate(a["sub_id"], proxy_id="p1", period_end=NOW + 3 * DAY,
                   charged=False, now=NOW)  # 首购未入账
    b = store.reserve(charge_ref="r2", wallet="default", country="US",
                      kind="isp", tokens=300, now=NOW)
    store.activate(b["sub_id"], proxy_id="p2", period_end=NOW + 20 * DAY,
                   charged=True, now=NOW)
    store.renew_cas(b["sub_id"], expected_end=NOW + 20 * DAY,
                    new_end=NOW + 50 * DAY, now=NOW)  # 续期悬挂

    rv = collect_review(db, now=NOW)
    assert rv["exists"] and rv["active"] == 2
    assert [r["sub_id"] for r in rv["unbilled_rows"]] == [a["sub_id"]]
    assert [r["sub_id"] for r in rv["renew_pending_rows"]] == [b["sub_id"]]
    assert [r["sub_id"] for r in rv["expiring_7d"]] == [a["sub_id"]]

    missing = collect_review(tmp_path / "nope.db", now=NOW)
    assert missing["exists"] is False
