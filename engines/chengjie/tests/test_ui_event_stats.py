"""前端 UI 交互事件埋点门禁。

覆盖：
- UiEventStats 记录/消毒/上限/dump/dump_prom/reset；
- POST /api/telemetry/ui-event → GET /api/workspace/metrics.ui_events 端到端。
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.web.ui_event_stats import UiEventStats, get_ui_event_stats
from src.web.routes.drafts_routes import register_metrics_route, register_telemetry_route


@pytest.fixture(autouse=True)
def _isolate_singleton():
    """进程级单例前后清零，防串测（beacon 端到端用例写的是同一单例）。"""
    get_ui_event_stats().reset()
    yield
    get_ui_event_stats().reset()


# ── 单元：计数 / 消毒 / 上限 ───────────────────────────────────────────

def test_record_and_dump():
    s = UiEventStats()
    s.record(page="/unified-inbox", action="empty_state_cta")
    s.record(page="/unified-inbox", action="empty_state_cta")
    s.record(page="/group-show", action="group_mode.toggle")
    d = s.dump()
    assert d["total"] == 3
    assert d["by_action"]["empty_state_cta"] == 2
    assert d["by_action"]["group_mode.toggle"] == 1
    assert d["by_page"]["/unified-inbox"] == 2
    assert d["overflow"] == 0
    assert d["last_record_ts"] > 0
    assert d["last_record_ts"] >= d["started_at"]
    # by_action 按次数降序
    assert list(d["by_action"].keys())[0] == "empty_state_cta"


def test_action_sanitization():
    s = UiEventStats()
    s.record(page="/x", action="Not-Valid()")       # 大写/括号/连字符 → unknown
    s.record(page="/x", action="1starts_with_digit")  # 数字开头 → unknown
    s.record(page="/x", action="a" * 65)             # 超长（>64）→ unknown
    s.record(page="/x", action="a" * 64)             # 恰好 64 → 合法保留
    d = s.dump()
    assert d["by_action"].get("unknown") == 3
    assert d["by_action"].get("a" * 64) == 1


def test_page_sanitization_strips_query_and_hash():
    s = UiEventStats()
    s.record(page="/unified-inbox?tab=x#frag", action="empty_state_cta")
    d = s.dump()
    assert "/unified-inbox" in d["by_page"]
    assert all("?" not in p and "#" not in p for p in d["by_page"])


def test_empty_defaults_to_unknown():
    s = UiEventStats()
    s.record()
    d = s.dump()
    assert d["by_page"].get("unknown") == 1
    assert d["by_action"].get("unknown") == 1


def test_distinct_key_cap_overflows():
    s = UiEventStats()
    # 灌 150 个不同 action（上限 100）→ 超出的归 __other__ 并计 overflow
    for i in range(150):
        s.record(page="/p", action=f"act_{i}")
    d = s.dump()
    assert d["total"] == 150
    assert len(d["by_action"]) <= 101  # 100 distinct + __other__
    assert "__other__" in d["by_action"]
    assert d["overflow"] >= 49


def test_dump_prom_shape():
    s = UiEventStats()
    s.record(page="/unified-inbox", action="empty_state_cta")
    txt = s.dump_prom()
    assert "ui_events_total 1" in txt
    assert 'ui_events_by_action_total{action="empty_state_cta"} 1' in txt
    assert 'ui_events_by_page_total{page="/unified-inbox"} 1' in txt
    # HELP/TYPE 行齐全
    for name in ("ui_events_total", "ui_events_by_action_total", "ui_events_by_page_total"):
        assert f"# HELP {name} " in txt
        assert f"# TYPE {name} counter" in txt


def test_prom_label_escaping():
    s = UiEventStats()
    # page 消毒会去掉引号/反斜杠，但 _esc 仍应保证输出合法
    s.record(page='/a"b\\c', action="x")
    txt = s.dump_prom()
    assert "ui_events_by_page_total" in txt


def test_reset_clears():
    s = UiEventStats()
    s.record(page="/p", action="empty_state_cta")
    s.reset()
    d = s.dump()
    assert d["total"] == 0
    assert d["overflow"] == 0
    assert d["by_action"] == {}
    assert d["by_page"] == {}
    assert d["last_record_ts"] == 0.0


# ── 端到端：beacon 写入 → metrics 读出 ───────────────────────────────

def _make_app(role="admin"):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_telemetry_route(app, api_auth=api_auth)
    register_metrics_route(app, api_auth=api_auth)
    return TestClient(app, raise_server_exceptions=True)


def test_beacon_then_metrics_roundtrip():
    c = _make_app(role="admin")

    r = c.post("/api/telemetry/ui-event",
               json={"page": "/unified-inbox", "action": "empty_state_cta"})
    assert r.status_code == 200 and r.json().get("ok") is True

    m = c.get("/api/workspace/metrics").json()
    ue = m.get("ui_events")
    assert ue is not None
    assert ue["total"] >= 1
    assert ue["by_action"].get("empty_state_cta") == 1
    assert ue["by_page"].get("/unified-inbox") == 1


def test_malformed_beacon_is_ok():
    """坏 body 不得 500——telemetry 绝不因脏输入影响前端。"""
    c = _make_app(role="admin")
    r = c.post("/api/telemetry/ui-event", data="not json",
               headers={"content-type": "text/plain"})
    assert r.status_code == 200 and r.json().get("ok") is True
    # 解析失败按 {} 走 record（与 frontend-error 同款）→ 记一条 unknown/unknown
    d = get_ui_event_stats().dump()
    assert d["total"] == 1
    assert d["by_action"].get("unknown") == 1
    assert d["by_page"].get("unknown") == 1
    # 非 dict 的合法 JSON → 跳过 record，同样恒 ok
    r2 = c.post("/api/telemetry/ui-event", json=[1, 2, 3])
    assert r2.status_code == 200 and r2.json().get("ok") is True
    assert get_ui_event_stats().dump()["total"] == 1


def test_metrics_prometheus_includes_ui_events():
    c = _make_app(role="admin")
    c.post("/api/telemetry/ui-event",
           json={"page": "/group-show", "action": "group_mode.toggle"})
    r = c.get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert "ui_events_total" in r.text
    assert 'action="group_mode.toggle"' in r.text
