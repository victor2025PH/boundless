"""P9 前端错误/意图落空「按日落库」趋势门禁。

覆盖：
- FrontendErrorTrendStore：upsert 聚合 / 类型消毒 / 缺日补零 / prune；
- 默认关闸门：未 configure → record 恒 no-op；
- beacon 路由旁路端到端：POST /api/telemetry/frontend-error → 趋势库出数；
- 读端点 /api/admin/frontend-error-trend：未启用 enabled:false / 启用出近 N 天。
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.frontend_error_trend as fet
from src.web.frontend_error_trend import (
    FrontendErrorTrendStore,
    configure_frontend_error_trend,
    record_frontend_error_trend,
    reset_frontend_error_trend,
)
from src.web.routes.drafts_routes import register_telemetry_route


DAY = 86400.0


def test_store_upsert_and_daily_zero_fill():
    s = FrontendErrorTrendStore(":memory:")
    now = 1_753_800_000.0
    s.add("scoped_fail", now=now)
    s.add("scoped_fail", now=now)
    s.add("dead_intent", now=now)
    s.add("ReferenceError", now=now - DAY)
    days = s.daily(days=3, now=now)
    assert len(days) == 3
    assert days[0]["total"] == 0                      # 前天无数据补零
    assert days[1]["by_type"] == {"ReferenceError": 1}
    assert days[2]["by_type"] == {"dead_intent": 1, "scoped_fail": 2}
    assert days[2]["total"] == 3


def test_store_sanitizes_unknown_type_to_error():
    s = FrontendErrorTrendStore(":memory:")
    now = 1_753_800_000.0
    s.add("WeirdInjection<script>", now=now)
    s.add("conv_not_found", now=now)   # 意图落空类型在白名单，须保留原名
    d = s.daily(days=1, now=now)[0]
    assert d["by_type"] == {"Error": 1, "conv_not_found": 1}


def test_store_prune_drops_old_days():
    s = FrontendErrorTrendStore(":memory:")
    now = 1_753_800_000.0
    s.add("timeout", now=now - 10 * DAY)
    s.add("timeout", now=now)
    assert s.prune(retention_days=7, now=now) == 1
    days = s.daily(days=14, now=now)
    assert sum(x["total"] for x in days) == 1


def test_record_noop_when_disabled():
    reset_frontend_error_trend()
    record_frontend_error_trend("scoped_fail")   # 未 configure → 不建库不抛
    assert fet.get_frontend_error_trend_store() is None
    configure_frontend_error_trend(enabled=False)
    record_frontend_error_trend("scoped_fail")
    assert fet.get_frontend_error_trend_store() is None
    reset_frontend_error_trend()


def _make_app():
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_telemetry_route(app, api_auth=api_auth)

    # 读端点与 ops_overview_routes 同逻辑（那边整文件依赖重，这里等价内联）
    @app.get("/api/admin/frontend-error-trend")
    async def api_fe_trend(request: Request, days: int = 7):
        store = fet.get_frontend_error_trend_store()
        if store is None:
            return {"ok": True, "enabled": False, "days": []}
        return {"ok": True, "enabled": True, "days": store.daily(days=int(days or 7))}

    return TestClient(app, raise_server_exceptions=True)


def test_beacon_writes_trend_when_enabled(tmp_path):
    reset_frontend_error_trend()
    configure_frontend_error_trend(enabled=True, db_path=tmp_path / "fe_trend.db")
    c = _make_app()
    r = c.post("/api/telemetry/frontend-error",
               json={"page": "/workspace", "fn": "scopedFetch", "type": "scoped_fail"})
    assert r.status_code == 200 and r.json().get("ok") is True
    c.post("/api/telemetry/frontend-error",
           json={"page": "/workspace", "fn": "viewAcctChats", "type": "dead_intent"})
    d = c.get("/api/admin/frontend-error-trend?days=2").json()
    assert d["enabled"] is True
    today = d["days"][-1]
    assert today["by_type"].get("scoped_fail") == 1
    assert today["by_type"].get("dead_intent") == 1
    assert today["total"] == 2
    reset_frontend_error_trend()


def test_trend_endpoint_disabled_contract():
    reset_frontend_error_trend()
    c = _make_app()
    # 未启用：beacon 照收（进程内 stats 仍计数），趋势端点如实 enabled:false
    r = c.post("/api/telemetry/frontend-error",
               json={"page": "/workspace", "fn": "x", "type": "conv_not_found"})
    assert r.status_code == 200
    d = c.get("/api/admin/frontend-error-trend").json()
    assert d == {"ok": True, "enabled": False, "days": []}


def test_intent_types_not_collapsed_in_stats():
    """P9 附带修：意图落空三类此前不在 _KNOWN_TYPES，被折叠成 Error → by_type 不可见。"""
    from src.web.frontend_error_stats import FrontendErrorStats
    s = FrontendErrorStats()
    s.record(page="/workspace", fn="scopedFetch", etype="scoped_fail")
    s.record(page="/workspace", fn="viewAcctChats", etype="dead_intent")
    s.record(page="/workspace", fn="openConvByCid", etype="conv_not_found")
    bt = s.dump()["by_type"]
    assert bt == {"scoped_fail": 1, "dead_intent": 1, "conv_not_found": 1}
