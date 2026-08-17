"""代理池防封增强回归网（2026-08）。

覆盖：基础 CRUD 向后兼容 / 幂等 migration / 代理类型校验 / 一号一 IP 排他绑定 /
真握手验活的注入式探针三态（成功·坏·回落 TCP）/ 黑名单健康状态机 / pick_available
过滤 / geo 校验纯函数 / health 聚合 / 与 orchestrator 的字段契约。

设计约束：全部用 tmp_path 独立 db，绝不碰全局单例 get_proxy_pool() 或仓库 config；
验活注入 probe_fn（零真实网络），TCP 回落用本地临时监听端口验证。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from src.integrations.proxy_pool import (
    PROXY_KINDS,
    ProbeResult,
    ProxyPool,
    default_proxy_probe,
    geo_mismatch,
)


def _pool(tmp_path, **kw) -> ProxyPool:
    return ProxyPool(tmp_path / "px.db", **kw)


# ── 基础 CRUD + 向后兼容契约 ─────────────────────────────────────────────────
def test_add_get_list_remove_backward_compat(tmp_path):
    pool = _pool(tmp_path)
    e = pool.add(scheme="socks5", host="1.2.3.4", port=1080, username="u", password="p", label="L")
    pid = e["proxy_id"]
    # orchestrator / _to_pyrogram_proxy 依赖的字段必须都在（这是硬契约）
    for k in ("proxy_id", "scheme", "host", "port", "username", "password", "url"):
        assert k in e
    assert e["host"] == "1.2.3.4" and e["port"] == 1080 and e["scheme"] == "socks5"
    # masked：密码隐藏、url 保留占位不泄明文
    assert e["password"] == "******"
    assert e["url"] == "socks5://u:******@1.2.3.4:1080"
    # mask=False 给 orchestrator 用：明文密码可取
    raw = pool.get(pid, mask=False)
    assert raw["password"] == "p"
    assert pool.list() and pool.list()[0]["proxy_id"] == pid
    pool.remove(pid)
    assert pool.get(pid) is None


def test_to_pyrogram_proxy_contract(tmp_path):
    """get(mask=False) 的输出必须能直接喂给 _to_pyrogram_proxy（真实调用方）。"""
    from src.integrations.telegram_protocol_login import _to_pyrogram_proxy

    pool = _pool(tmp_path)
    e = pool.add(scheme="socks5", host="9.9.9.9", port=1080, username="u", password="pw")
    px = _to_pyrogram_proxy(pool.get(e["proxy_id"], mask=False))
    assert px == {"scheme": "socks5", "hostname": "9.9.9.9", "port": 1080,
                  "username": "u", "password": "pw"}


def test_invalid_scheme_and_kind_rejected(tmp_path):
    pool = _pool(tmp_path)
    with pytest.raises(ValueError):
        pool.add(scheme="ftp", host="h", port=1)
    with pytest.raises(ValueError):
        pool.add(scheme="socks5", host="h", port=1, kind="banana")
    with pytest.raises(ValueError):
        pool.add(scheme="socks5", host="", port=0)


def test_add_with_kind_and_geo(tmp_path):
    pool = _pool(tmp_path)
    e = pool.add(scheme="http", host="h", port=8080, kind="residential", country="us", region="CA")
    assert e["kind"] == "residential"
    assert e["country"] == "US"  # 归一大写
    assert e["region"] == "CA"
    assert e["is_residential"] is True


# ── 幂等 migration（存量旧 db 缺列即补，旧数据保留）───────────────────────────
def test_migration_backfills_columns_on_legacy_db(tmp_path):
    db = tmp_path / "legacy.db"
    # 手工建「旧 schema」（原始 12 列，无新增列）+ 塞一行存量数据
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """CREATE TABLE proxies (
            proxy_id TEXT PRIMARY KEY, label TEXT DEFAULT '', scheme TEXT DEFAULT 'socks5',
            host TEXT DEFAULT '', port INTEGER DEFAULT 0, username TEXT DEFAULT '',
            password TEXT DEFAULT '', status TEXT DEFAULT 'unknown',
            assigned_account TEXT DEFAULT '', created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0, last_checked_at REAL DEFAULT 0);"""
    )
    conn.execute(
        "INSERT INTO proxies (proxy_id, host, port) VALUES ('px_legacy','5.5.5.5',1080)"
    )
    conn.commit()
    conn.close()

    pool = ProxyPool(db)  # 打开即 migrate
    e = pool.get("px_legacy")
    assert e is not None
    assert e["host"] == "5.5.5.5"  # 旧数据保留
    # 新列已补齐且有默认值
    for k in ("kind", "country", "region", "exit_ip", "fail_count", "cooldown_until", "latency_ms"):
        assert k in e
    assert e["kind"] == "unknown" and e["fail_count"] == 0
    # 二次打开不重复报错（幂等）
    ProxyPool(db)


# ── 一号一 IP 排他绑定 ───────────────────────────────────────────────────────
def test_assign_exclusive_releases_old_proxy(tmp_path):
    pool = _pool(tmp_path)
    a = pool.add(scheme="socks5", host="a", port=1)["proxy_id"]
    b = pool.add(scheme="socks5", host="b", port=2)["proxy_id"]
    acct = "telegram:123"
    pool.assign(a, acct)
    assert pool.get(a)["assigned_account"] == acct
    # 同一账号换绑 b → a 自动释放（一号只占一条出口）
    pool.assign(b, acct)
    assert pool.get(b)["assigned_account"] == acct
    assert pool.get(a)["assigned_account"] == ""


def test_assign_non_exclusive_keeps_old(tmp_path):
    pool = _pool(tmp_path)
    a = pool.add(scheme="socks5", host="a", port=1)["proxy_id"]
    b = pool.add(scheme="socks5", host="b", port=2)["proxy_id"]
    acct = "telegram:123"
    pool.assign(a, acct)
    pool.assign(b, acct, exclusive=False)
    assert pool.get(a)["assigned_account"] == acct  # 未释放
    assert pool.get(b)["assigned_account"] == acct


def test_unassign_and_release_account(tmp_path):
    pool = _pool(tmp_path)
    a = pool.add(scheme="socks5", host="a", port=1)["proxy_id"]
    b = pool.add(scheme="socks5", host="b", port=2)["proxy_id"]
    pool.assign(a, "acct", exclusive=False)
    pool.assign(b, "acct", exclusive=False)
    pool.unassign(a)
    assert pool.get(a)["assigned_account"] == ""
    assert pool.get(b)["assigned_account"] == "acct"
    pool.release_account("acct")
    assert pool.get(b)["assigned_account"] == ""


# ── 黑名单健康状态机 ─────────────────────────────────────────────────────────
def test_record_probe_state_machine(tmp_path):
    pool = _pool(tmp_path, fail_threshold=3, cooldown_sec=100)
    pid = pool.add(scheme="http", host="h", port=8080)["proxy_id"]
    # 前两次失败：status=fail，未进冷却
    pool.record_probe(pid, ProbeResult(ok=False, error="x"))
    pool.record_probe(pid, ProbeResult(ok=False, error="x"))
    e = pool.get(pid)
    assert e["status"] == "fail" and e["fail_count"] == 2 and e["in_cooldown"] is False
    # 第三次失败达阈值 → 进冷却（黑名单）
    pool.record_probe(pid, ProbeResult(ok=False, error="x"))
    e = pool.get(pid)
    assert e["status"] == "cooldown" and e["fail_count"] == 3
    assert e["in_cooldown"] is True and e["cooldown_remaining"] > 0
    # 一次成功 → 清零 + 回填 geo
    pool.record_probe(pid, ProbeResult(ok=True, exit_ip="8.8.8.8", country="US", latency_ms=42.0))
    e = pool.get(pid)
    assert e["status"] == "ok" and e["fail_count"] == 0 and e["in_cooldown"] is False
    assert e["exit_ip"] == "8.8.8.8" and e["country"] == "US" and e["latency_ms"] == 42.0
    assert e["healthy"] is True


def test_clear_cooldown(tmp_path):
    pool = _pool(tmp_path, fail_threshold=1, cooldown_sec=100)
    pid = pool.add(scheme="http", host="h", port=8080)["proxy_id"]
    pool.record_probe(pid, ProbeResult(ok=False))
    assert pool.get(pid)["in_cooldown"] is True
    pool.clear_cooldown(pid)
    e = pool.get(pid)
    assert e["in_cooldown"] is False and e["fail_count"] == 0 and e["status"] == "unknown"


# ── 真握手验活（注入式探针三态）─────────────────────────────────────────────
async def test_test_uses_injected_probe_success(tmp_path):
    pool = _pool(tmp_path)
    pid = pool.add(scheme="http", host="h", port=8080)["proxy_id"]

    async def probe(proxy):
        assert proxy["host"] == "h"  # 探针拿到明文条目
        return ProbeResult(ok=True, exit_ip="1.1.1.1", country="SG", latency_ms=12.5)

    ok = await pool.test(pid, probe_fn=probe)
    assert ok is True
    e = pool.get(pid)
    assert e["status"] == "ok" and e["country"] == "SG" and e["exit_ip"] == "1.1.1.1"


async def test_test_probe_reports_bad_proxy(tmp_path):
    pool = _pool(tmp_path, fail_threshold=2, cooldown_sec=50)
    pid = pool.add(scheme="http", host="h", port=8080)["proxy_id"]

    async def probe(proxy):
        return ProbeResult(ok=False, error="proxy refused")

    assert await pool.test(pid, probe_fn=probe) is False
    assert await pool.test(pid, probe_fn=probe) is False
    e = pool.get(pid)
    assert e["fail_count"] == 2 and e["in_cooldown"] is True  # 探到坏 → 不回落，直接进状态机


async def test_test_falls_back_to_tcp_when_probe_unavailable(tmp_path):
    pool = _pool(tmp_path)
    # 起一个本地监听端口 = TCP 可达
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def no_probe(proxy):
        return None  # 探测器不可用 → 触发 TCP 回落

    try:
        pid = pool.add(scheme="socks5", host="127.0.0.1", port=port)["proxy_id"]
        assert await pool.test(pid, probe_fn=no_probe, timeout=3.0) is True
        assert pool.get(pid)["status"] == "ok"
    finally:
        server.close()
        await server.wait_closed()
    # server 关闭后同一端口 TCP 不通 → 回落判 fail
    assert await pool.test(pid, probe_fn=no_probe, timeout=1.0) is False


async def test_probe_exception_falls_back_not_crash(tmp_path):
    pool = _pool(tmp_path)
    pid = pool.add(scheme="socks5", host="127.0.0.1", port=1)["proxy_id"]

    async def boom(proxy):
        raise RuntimeError("probe blew up")

    # 探针抛错不应崩，应回落 TCP（此端口不通 → False），且不抛
    assert await pool.test(pid, probe_fn=boom, timeout=1.0) is False


async def test_default_probe_missing_hostport_no_network(tmp_path):
    # host/port 缺失应直接判坏（不发网络），是纯逻辑分支
    r = await default_proxy_probe({"scheme": "http", "host": "", "port": 0})
    assert r is not None and r.ok is False


# ── pick_available 过滤 ──────────────────────────────────────────────────────
def test_pick_available_skips_assigned_and_cooldown(tmp_path):
    pool = _pool(tmp_path, fail_threshold=1, cooldown_sec=100)
    good = pool.add(scheme="socks5", host="good", port=1)["proxy_id"]
    assigned = pool.add(scheme="socks5", host="assigned", port=2)["proxy_id"]
    cooled = pool.add(scheme="socks5", host="cooled", port=3)["proxy_id"]
    pool.record_probe(good, ProbeResult(ok=True))  # status ok
    pool.assign(assigned, "acct")
    pool.record_probe(cooled, ProbeResult(ok=False))  # 进冷却
    picked = pool.pick_available()
    assert picked is not None and picked["proxy_id"] == good


def test_pick_available_kind_country_residential_filters(tmp_path):
    pool = _pool(tmp_path)
    dc = pool.add(scheme="socks5", host="dc", port=1, kind="datacenter", country="US")["proxy_id"]
    res = pool.add(scheme="socks5", host="res", port=2, kind="residential", country="VN")["proxy_id"]
    # residential_only 只挑住宅/移动
    assert pool.pick_available(residential_only=True)["proxy_id"] == res
    # country 过滤
    assert pool.pick_available(country="us")["proxy_id"] == dc
    # kind 过滤
    assert pool.pick_available(kind="residential")["proxy_id"] == res
    # 无匹配 → None
    assert pool.pick_available(country="JP") is None


# ── geo 校验纯函数 ───────────────────────────────────────────────────────────
def test_geo_mismatch_pure():
    assert geo_mismatch("US", "US") is False
    assert geo_mismatch("us", "US") is False  # 归一
    assert geo_mismatch("US", "VN") is True
    # 信息不足不判（宁可漏报不误报）
    assert geo_mismatch("", "VN") is False
    assert geo_mismatch("US", "") is False
    assert geo_mismatch("", "") is False


# ── health 聚合 ──────────────────────────────────────────────────────────────
def test_health_summary(tmp_path):
    pool = _pool(tmp_path, fail_threshold=1, cooldown_sec=100)
    h1 = pool.add(scheme="socks5", host="h1", port=1, kind="residential")["proxy_id"]
    pool.add(scheme="socks5", host="h2", port=2, kind="datacenter")
    bad = pool.add(scheme="socks5", host="h3", port=3, kind="mobile")["proxy_id"]
    pool.record_probe(h1, ProbeResult(ok=True))
    pool.record_probe(bad, ProbeResult(ok=False))  # 进冷却
    pool.assign(h1, "acct")
    s = pool.health_summary()
    assert s["total"] == 3
    assert s["healthy"] == 1  # 只有 h1 ok 且不在冷却
    assert s["in_cooldown"] == 1
    assert s["assigned"] == 1
    assert s["residential"] == 2  # residential + mobile
    assert s["residential_pct"] == pytest.approx(66.7, abs=0.2)


def test_proxy_kinds_constant_sane():
    assert "residential" in PROXY_KINDS and "datacenter" in PROXY_KINDS
    assert PROXY_KINDS[0] == "unknown"
