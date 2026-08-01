# -*- coding: utf-8 -*-
"""UI 事件按日落库（ui_event_trend）门禁。

动机（2026-08-01）：进程内 ui_events 计数重启即清零——施工日实测一天 4 次重启把
AI 回复漏斗（dpick.* 取消率/采纳率）的首批真实数据清洗掉。本套用例钉住：
upsert 跨「重启」（两个 store 实例同一文件）累计、动作消毒、按日补零、prefix
命名空间过滤（漏斗读数不被其他埋点稀释）、日基数封顶折叠、默认关零副作用、
beacon 路由端到端写入。
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.ui_event_trend as uet
from src.web.ui_event_trend import (
    UiEventTrendStore,
    configure_ui_event_trend,
    record_ui_event_trend,
    reset_ui_event_trend,
)
from src.web.routes.drafts_routes import register_telemetry_route

DAY = 86400.0
T0 = 1_754_000_000.0


def test_store_upsert_daily_zero_fill_and_prefix():
    s = UiEventTrendStore(":memory:")
    s.add("dpick.open", now=T0)
    s.add("dpick.open", now=T0)
    s.add("dpick.cancel", now=T0)
    s.add("empty.show_all", now=T0)
    s.add("dpick.open", now=T0 - 2 * DAY)
    days = s.daily(days=3, now=T0)
    assert [d["day"] for d in days] == [uet._day_str(T0 - 2 * DAY),
                                        uet._day_str(T0 - DAY),
                                        uet._day_str(T0)]
    assert days[1]["total"] == 0                       # 中间缺数据补零不断点
    assert days[2]["by_action"]["dpick.open"] == 2
    assert days[2]["total"] == 4
    # prefix 过滤：漏斗读数不被 empty.* 稀释
    dp = s.daily(days=1, prefix="dpick.", now=T0)
    assert dp[0]["total"] == 3
    assert "empty.show_all" not in dp[0]["by_action"]


def test_store_survives_process_restart(tmp_path):
    """同一文件两个实例（模拟重启）：计数累计不清零——存在理由本身。"""
    fp = tmp_path / "uiev.db"
    s1 = UiEventTrendStore(fp)
    s1.add("dpick.open", now=T0)
    s2 = UiEventTrendStore(fp)                          # 「重启后」的新进程
    s2.add("dpick.open", now=T0)
    assert s2.daily(days=1, now=T0)[0]["by_action"]["dpick.open"] == 2


def test_action_sanitized_and_day_cap_folds():
    s = UiEventTrendStore(":memory:")
    s.add("<script>alert(1)</script>", now=T0)          # 非法动作 → unknown
    assert s.daily(days=1, now=T0)[0]["by_action"] == {"unknown": 1}
    # 日基数封顶：塞满 cap 后新动作折叠 __other__（防刷量撑爆库）
    s2 = UiEventTrendStore(":memory:")
    for i in range(uet._DAY_ACTION_CAP):
        s2.add(f"a{i}", now=T0)
    s2.add("brand_new_action", now=T0)
    by = s2.daily(days=1, now=T0)[0]["by_action"]
    assert by.get("__other__") == 1
    assert "brand_new_action" not in by
    # 已有动作不受封顶影响（继续累计原键）
    s2.add("a0", now=T0)
    assert s2.daily(days=1, now=T0)[0]["by_action"]["a0"] == 2


def test_record_noop_when_disabled():
    reset_ui_event_trend()
    record_ui_event_trend("dpick.open")                 # 未 configure → 不建库不抛
    assert uet.get_ui_event_trend_store() is None
    configure_ui_event_trend(enabled=False)
    record_ui_event_trend("dpick.open")
    assert uet.get_ui_event_trend_store() is None
    reset_ui_event_trend()


def test_prune_removes_old_days():
    s = UiEventTrendStore(":memory:")
    s.add("dpick.open", now=T0 - 40 * DAY)
    s.add("dpick.open", now=T0)
    assert s.prune(retention_days=30, now=T0) == 1
    assert s.daily(days=1, now=T0)[0]["total"] == 1


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
    @app.get("/api/admin/ui-event-trend")
    async def api_uiev_trend(request: Request, days: int = 14, prefix: str = ""):
        store = uet.get_ui_event_trend_store()
        if store is None:
            return {"ok": True, "enabled": False, "days": []}
        return {"ok": True, "enabled": True,
                "days": store.daily(days=int(days or 14), prefix=prefix)}

    return TestClient(app, raise_server_exceptions=True)


def test_beacon_writes_trend_when_enabled(tmp_path):
    reset_ui_event_trend()
    configure_ui_event_trend(enabled=True, db_path=tmp_path / "uiev.db")
    c = _make_app()
    r = c.post("/api/telemetry/ui-event",
               json={"page": "/workspace", "action": "dpick.open"})
    assert r.status_code == 200 and r.json().get("ok") is True
    c.post("/api/telemetry/ui-event",
           json={"page": "/workspace", "action": "dpick.cancel"})
    c.post("/api/telemetry/ui-event",
           json={"page": "/workspace", "action": "grp.mode_all"})
    d = c.get("/api/admin/ui-event-trend?days=2&prefix=dpick.").json()
    assert d["enabled"] is True
    today = d["days"][-1]
    assert today["by_action"] == {"dpick.cancel": 1, "dpick.open": 1}
    assert today["total"] == 2                          # grp.* 被 prefix 滤掉
    reset_ui_event_trend()


def test_trend_endpoint_disabled_contract():
    reset_ui_event_trend()
    c = _make_app()
    r = c.post("/api/telemetry/ui-event",
               json={"page": "/workspace", "action": "dpick.open"})
    assert r.status_code == 200                         # beacon 照收（进程内 stats 仍计数）
    d = c.get("/api/admin/ui-event-trend").json()
    assert d == {"ok": True, "enabled": False, "days": []}
