"""命理观测门禁：stats 计数/导出 + workspace metrics 并入 + Prometheus 文本。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.companion.bazi_stats import BaziStats, get_bazi_stats


@pytest.fixture(autouse=True)
def _clean():
    get_bazi_stats().reset()
    yield
    get_bazi_stats().reset()


def test_counters_and_dump():
    s = BaziStats()
    s.record_topic_turn()
    s.record_chart_injection(same_turn=True)
    s.record_chart_injection()
    s.record_ask_directive()
    s.record_birth_captured()
    s.record_gender_completed()
    s.record_daily_card("chat")
    s.record_daily_card("ritual")
    s.record_deep_reading(allowed=True)
    s.record_deep_reading(allowed=False)
    s.record_kline(ok=True)
    s.record_kline(ok=False)
    d = s.dump()
    assert d["topic_turns"] == 1
    assert d["chart_injections"] == 2 and d["same_turn_charts"] == 1
    assert d["ask_directives"] == 1 and d["birth_captured"] == 1
    assert d["gender_completed"] == 1
    assert d["daily_cards"] == {"chat": 1, "ritual": 1, "total": 2}
    assert d["deep_reading"] == {"allowed": 1, "upsell": 1}
    assert d["kline"] == {"sent": 1, "failed": 1}
    assert d["capture_rate"] == 1.0
    assert d["active"] is True


def test_dump_inactive_and_capture_rate_none():
    s = BaziStats()
    d = s.dump()
    assert d["active"] is False
    assert d["capture_rate"] is None


def test_dump_prom_format():
    s = BaziStats()
    s.record_topic_turn()
    s.record_deep_reading(allowed=False)
    txt = s.dump_prom()
    assert "bazi_topic_turns_total 1" in txt
    assert 'bazi_deep_reading_total{outcome="upsell"} 1' in txt
    assert 'bazi_daily_cards_total{source="ritual"} 0' in txt
    assert txt.endswith("\n")


def test_reset():
    s = BaziStats()
    s.record_topic_turn()
    s.reset()
    assert s.dump()["topic_turns"] == 0


def test_metrics_route_exposes_bazi():
    from src.web.routes.drafts_routes import register_metrics_route

    get_bazi_stats().record_topic_turn()
    get_bazi_stats().record_kline(ok=True)

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=_api_auth)
    c = TestClient(app, raise_server_exceptions=True)

    bz = c.get("/api/workspace/metrics").json().get("bazi")
    assert bz is not None
    assert bz["topic_turns"] == 1
    assert bz["kline"]["sent"] == 1
    assert bz["active"] is True

    txt = c.get("/api/workspace/metrics?format=prometheus").text
    assert "bazi_topic_turns_total 1" in txt
    assert 'bazi_kline_total{outcome="sent"} 1' in txt
