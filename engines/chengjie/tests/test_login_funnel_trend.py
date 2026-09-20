"""账号接入漏斗按日趋势落库门禁。

覆盖：
- 未 configure → record 恒 no-op、读端点 enabled:false
- started/authorized/failed 增量 + 失败原因分桶
- 未知原因码折叠 login_failed；qr_shown/pin_issued 不落库
- daily 补零不断点；prune 砍旧日
- record_login_stage 旁路接线（启用后一次调用两边都涨）
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.integrations.login_funnel_trend as lft
from src.integrations.login_funnel_stats import (
    get_login_funnel_stats,
    record_login_stage,
)
from src.integrations.login_funnel_trend import (
    LoginFunnelTrendStore,
    configure_login_funnel_trend,
    record_login_funnel_trend,
    reset_login_funnel_trend,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_login_funnel_trend()
    get_login_funnel_stats().reset()
    yield
    reset_login_funnel_trend()
    get_login_funnel_stats().reset()


def test_disabled_is_noop():
    record_login_funnel_trend("started")
    assert lft.get_login_funnel_trend_store() is None
    configure_login_funnel_trend(enabled=False)
    record_login_funnel_trend("started")
    assert lft.get_login_funnel_trend_store() is None


def test_store_increments_and_folds_reason(tmp_path):
    store = LoginFunnelTrendStore(tmp_path / "t.db")
    now = time.time()
    store.add("started", now=now)
    store.add("started", now=now)
    store.add("authorized", now=now)
    store.add("failed", reason_code="checkpoint", now=now)
    store.add("failed", reason_code="not_a_real_code", now=now)
    store.add("qr_shown", now=now)  # 不落趋势
    store.add("pin_issued", now=now)

    rows = store.daily(days=1, now=now)
    assert len(rows) == 1
    r = rows[0]
    assert r["started"] == 2
    assert r["authorized"] == 1
    assert r["failed"] == 2
    assert r["by_reason"].get("checkpoint") == 1
    assert r["by_reason"].get("login_failed") == 1
    assert "qr_shown" not in r


def test_daily_pads_zeros_and_prune(tmp_path):
    store = LoginFunnelTrendStore(tmp_path / "t.db")
    now = time.time()
    store.add("started", now=now)
    store.add("started", now=now - 3 * 86400)
    days = store.daily(days=5, now=now)
    assert len(days) == 5
    # 两端有量、中间可空
    assert days[-1]["started"] == 1
    assert sum(d["started"] for d in days) == 2
    # retention=1 → 只留今天（切点 day < today-1）
    deleted = store.prune(retention_days=1, now=now)
    assert deleted >= 1
    days2 = store.daily(days=5, now=now)
    assert sum(d["started"] for d in days2) == 1


def test_record_login_stage_wires_trend(tmp_path):
    configure_login_funnel_trend(
        enabled=True, db_path=tmp_path / "wire.db", retention_days=30,
    )
    record_login_stage("messenger", "web", "started")
    record_login_stage("messenger", "web", "failed", reason_code="two_factor")
    proc = get_login_funnel_stats().dump()
    assert proc["rows"][0]["started"] == 1
    assert proc["rows"][0]["reasons"].get("two_factor") == 1
    store = lft.get_login_funnel_trend_store()
    assert store is not None
    days = store.daily(days=1)
    assert days[-1]["started"] == 1
    assert days[-1]["failed"] == 1
    assert days[-1]["by_reason"].get("two_factor") == 1


def test_admin_route_enabled_flag(tmp_path):
    app = FastAPI()

    @app.get("/api/admin/login-funnel-trend")
    async def _ep(days: int = 7):
        store = lft.get_login_funnel_trend_store()
        if store is None:
            return {"ok": True, "enabled": False, "days": []}
        return {"ok": True, "enabled": True, "days": store.daily(days=days)}

    c = TestClient(app)
    d = c.get("/api/admin/login-funnel-trend?days=2").json()
    assert d["enabled"] is False
    assert d["days"] == []

    configure_login_funnel_trend(enabled=True, db_path=tmp_path / "r.db")
    record_login_funnel_trend("started")
    d2 = c.get("/api/admin/login-funnel-trend?days=2").json()
    assert d2["enabled"] is True
    assert len(d2["days"]) == 2
    assert sum(x["started"] for x in d2["days"]) == 1


def test_ops_template_wires_trend_endpoint():
    """ops 卡必须拉趋势端点 + 渲染 sparkline 容器（热更新直上生产）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "src" / "web" / "templates" / "ops_overview.html").read_text(
                encoding="utf-8")
    assert "loginFunnelTrend" in html
    assert "/api/admin/login-funnel-trend" in html
    assert "loadLoginFunnelTrend" in html
    assert "ov2_js_lf_trend_title" in html
