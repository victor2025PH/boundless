# -*- coding: utf-8 -*-
"""CSRF 准入/拒绝日趋势落库（P2，2026-07-31）。

进程计数器重启即清零（本机重启频繁），而「同源 Origin/Referer 回落能不能收口」
要看**跨两周**的通行侧证据。本文件钉住：
- store 的 upsert/补零/聚合语义；
- 旁路闸门（未启用恒 no-op）与写放大控制（csrf_pair/bearer 放行**不落库**）；
- 中间件 → 趋势库 → 读端点的端到端链。
"""

from __future__ import annotations

import src.web.csrf_trend as ct
from src.web.csrf_trend import (
    CsrfTrendStore,
    configure_csrf_trend,
    record_csrf_admit_trend,
    record_csrf_reject_trend,
    reset_csrf_trend,
)

DAY = 86400.0


def test_store_upsert_and_daily_zero_fill():
    s = CsrfTrendStore(":memory:")
    now = 1_753_800_000.0
    s.add("reject:bare", now=now)
    s.add("reject:bare", now=now)
    s.add("admit:referer", now=now)
    s.add("reject:cookie_no_header", now=now - DAY)
    days = s.daily(days=3, now=now)
    assert [d["day"] for d in days] == [
        ct._day_str(now - 2 * DAY), ct._day_str(now - DAY), ct._day_str(now)]
    assert days[0]["rejects"] == 0 and days[0]["admits"] == 0   # 缺数据补零
    assert days[1]["by_key"] == {"reject:cookie_no_header": 1}
    assert days[2]["rejects"] == 2 and days[2]["admits"] == 1
    assert days[2]["by_key"]["admit:referer"] == 1


def test_prune_drops_old_days():
    s = CsrfTrendStore(":memory:")
    now = 1_753_800_000.0
    s.add("reject:bare", now=now - 40 * DAY)
    s.add("reject:bare", now=now)
    assert s.prune(retention_days=30, now=now) == 1
    days = s.daily(days=2, now=now)
    assert days[-1]["rejects"] == 1


def test_record_noop_when_disabled():
    reset_csrf_trend()
    record_csrf_reject_trend("bare")            # 未 configure → 不建库不抛
    record_csrf_admit_trend("referer")
    assert ct.get_csrf_trend_store() is None
    configure_csrf_trend(enabled=False)
    record_csrf_reject_trend("bare")
    assert ct.get_csrf_trend_store() is None
    reset_csrf_trend()


def test_admit_trend_only_persists_fallback_tickets(tmp_path):
    """写放大控制：csrf_pair/bearer 放行绝不落库——落的只有收口决策要看的两张票。"""
    reset_csrf_trend()
    store = configure_csrf_trend(enabled=True, db_path=tmp_path / "t.db")
    record_csrf_admit_trend("csrf_pair")
    record_csrf_admit_trend("bearer")
    record_csrf_admit_trend("origin")
    record_csrf_admit_trend("referer")
    record_csrf_reject_trend("weird_kind")      # 白名单外折叠 bare
    today = store.daily(days=1)[-1]
    assert today["by_key"] == {
        "admit:origin": 1, "admit:referer": 1, "reject:bare": 1}
    reset_csrf_trend()


def test_middleware_to_endpoint_chain(tmp_path, client, auth_client):
    """端到端：中间件拒绝/同源放行 → 趋势库 → /api/admin/csrf-trend 可读。

    注意：``auth_client`` 依赖并共享 ``client`` 且设了**默认 Bearer header**（conftest
    里为绕过 CSRF）——写探针必须显式清空 Authorization，否则一律走 bearer 放行
    （既不 reject 也不落 origin/referer 趋势）。登录已补种 csrf_token cookie，故
    「无 X-CSRF-Token 裸写」的拒绝形态是 cookie_no_header——断言按「有 reject 落库」
    的鲁棒口径，不纠结具体 kind。读 trend 端点仍用 auth_client 的默认 Bearer 鉴权。"""
    reset_csrf_trend()
    configure_csrf_trend(enabled=True, db_path=tmp_path / "chain.db")
    try:
        # 清空 Bearer 的写 → 无凭证被拒（reject:* 落库）
        client.post("/api/persona/bind", json={"scope": "conversation"},
                    headers={"Authorization": ""})
        # 清空 Bearer + 同源 Referer → admit:referer（后续 401 不影响准入已记账）
        client.post("/api/persona/bind", json={"scope": "conversation"},
                    headers={"Authorization": "", "Referer": "http://testserver/workspace"})
        d = auth_client.get("/api/admin/csrf-trend?days=2").json()
        assert d["ok"] is True and d["enabled"] is True
        today = d["days"][-1]
        assert today["rejects"] >= 1, today
        assert today["by_key"].get("admit:referer", 0) >= 1, today
    finally:
        reset_csrf_trend()


def test_endpoint_reports_disabled_when_off(auth_client):
    reset_csrf_trend()
    d = auth_client.get("/api/admin/csrf-trend").json()
    assert d["ok"] is True and d["enabled"] is False and d["days"] == []
