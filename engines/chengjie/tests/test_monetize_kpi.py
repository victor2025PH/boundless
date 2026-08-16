"""P4 变现 KPI 轻端点（/api/monetize/kpi）—— 路由烟雾测试。

覆盖：
- 空库 + 未挂 require_role → 200，7 键契约（ok/enabled/window_days/revenue_total/
  tx_count/currency/active_subscriptions），数值全 0/False。
- app.state.require_role 抛 403 → 端点透传 403（角色门控接线），且 page_key
  传 "monetization"（与 /monetization 页面同权）。
- days 参数钳制到 [1, 365]。

组装参照 test_rpa_overview.py：裸 FastAPI + TestClient + noop auth。
entitlement_store 走 _store 的懒建分支：fixture 预建 :memory: 进程单例，懒建
即复用之、不落盘（无 config_manager 时懒建算出的 db_path 是 ./entitlements.db
而非 :memory:——Path("").parent == "." 为真值——故不能放任其真建库）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.utils.entitlement_store import (
    get_entitlement_store,
    reset_entitlement_store,
)
from src.web.routes.monetization_routes import register_monetization_routes


def _noop_auth(request: Request):
    return True


@pytest.fixture(autouse=True)
def _mem_store_singleton():
    """前后清空进程单例并预建 :memory: 库，隔离串测且保证懒建不落盘。"""
    reset_entitlement_store()
    get_entitlement_store(":memory:")
    yield
    reset_entitlement_store()


def _make_client() -> TestClient:
    """裸 app：不传 config_manager、不挂 app.state.entitlement_store。"""
    app = FastAPI()
    register_monetization_routes(app, api_auth=_noop_auth)
    return TestClient(app)


def test_kpi_empty_store_returns_zeroed_contract():
    client = _make_client()
    r = client.get("/api/monetize/kpi")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {
        "ok", "enabled", "window_days", "revenue_total",
        "tx_count", "currency", "active_subscriptions",
    }
    assert d["ok"] is True
    assert d["enabled"] is False  # 无 config → 变现总开关默认关
    assert d["window_days"] == 30.0
    assert d["revenue_total"] == 0.0
    assert d["tx_count"] == 0
    assert d["currency"] == "USD"
    assert d["active_subscriptions"] == 0


def test_kpi_respects_require_role_gate_403():
    client = _make_client()
    seen: list = []

    def _deny(request: Request, page_key: str):
        seen.append(page_key)
        raise HTTPException(status_code=403, detail="无权访问此页面")

    client.app.state.require_role = _deny
    r = client.get("/api/monetize/kpi")
    assert r.status_code == 403
    assert seen == ["monetization"]  # 与 /monetization 页面同一权限键


def test_kpi_clamps_days_to_1_365():
    client = _make_client()
    r = client.get("/api/monetize/kpi?days=9999")
    assert r.status_code == 200
    assert r.json()["window_days"] == 365.0
    assert client.get("/api/monetize/kpi?days=0.5").json()["window_days"] == 1.0
