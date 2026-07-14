# -*- coding: utf-8 -*-
"""Phase 8b: /lead-mesh/funnel API endpoint 单测 (2026-04-24).

用 FastAPI TestClient 真调 route, 验证:
  * 空 DB 返回结构化 0 值
  * 有数据时返回 dict 含预期 key
  * 过滤参数 days / actor 生效
  * days 越界参数拒绝
"""
from __future__ import annotations

import os

import pytest

os.environ["OPENCLAW_API_KEY"] = ""

from fastapi.testclient import TestClient
from src.host.api import app
import src.host.database as db_mod


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    original = db_mod.DB_PATH
    db_mod.DB_PATH = tmp_path / "funnel_api_test.db"
    db_mod.init_db()
    yield
    db_mod.DB_PATH = original


@pytest.fixture
def api_client():
    return TestClient(app, raise_server_exceptions=False)


class TestFunnelApi:
    def test_api_empty(self, api_client, tmp_db):
        r = api_client.get("/lead-mesh/funnel")
        assert r.status_code == 200
        body = r.json()
        assert body["window_days"] == 7
        assert body["total_friend_requested"] == 0
        assert body["total_greeting_sent"] == 0
        assert body["rate_greet_after_friend"] == 0.0
        assert body["top_blocked_reason"] == ""

    def test_api_with_data(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        cid = resolve_identity(platform="facebook", account_id="fb:X",
                                 display_name="X")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp_female_midlife"})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message"})

        r = api_client.get("/lead-mesh/funnel?days=7")
        assert r.status_code == 200
        body = r.json()
        assert body["total_friend_requested"] == 1
        assert body["total_greeting_sent"] == 1
        assert body["greeting_via_inline"] == 1
        assert body["rate_greet_after_friend"] == 1.0
        # persona dict
        assert body["per_persona_friend_requested"].get("jp_female_midlife") == 1

    def test_api_actor_filter(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        cid = resolve_identity(platform="facebook", account_id="fb:Y",
                                 display_name="Y")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp"})
        append_journey(cid, actor="agent_b", action="friend_requested",
                         data={"persona_key": "jp"})

        r_all = api_client.get("/lead-mesh/funnel?days=7")
        r_a = api_client.get("/lead-mesh/funnel?days=7&actor=agent_a")
        assert r_all.json()["total_friend_requested"] == 2
        assert r_a.json()["total_friend_requested"] == 1

    def test_api_rejects_invalid_days(self, api_client, tmp_db):
        # days=0 < ge=1 → 422
        r = api_client.get("/lead-mesh/funnel?days=0")
        assert r.status_code == 422
        # days=365 > le=90 → 422
        r = api_client.get("/lead-mesh/funnel?days=365")
        assert r.status_code == 422

    def test_api_top_blocked_reason(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        cid = resolve_identity(platform="facebook", account_id="fb:BR",
                                 display_name="BR")
        for _ in range(3):
            append_journey(cid, actor="agent_a", action="greeting_blocked",
                             data={"reason": "no_message_button"})
        append_journey(cid, actor="agent_a", action="greeting_blocked",
                         data={"reason": "cap_hit"})

        r = api_client.get("/lead-mesh/funnel")
        body = r.json()
        assert body["total_greeting_blocked"] == 4
        assert body["top_blocked_reason"] == "no_message_button"
        assert body["blocked_reasons"]["no_message_button"] == 3


class TestDateFilterApi:
    """Phase 8g: date 参数 API 层."""

    def test_funnel_api_with_date(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        import datetime as dt

        cid = resolve_identity(platform="facebook", account_id="fb:DA1",
                                 display_name="DA1")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={})

        today = dt.datetime.utcnow().date().isoformat()
        r = api_client.get("/lead-mesh/funnel?date=" + today)
        body = r.json()
        assert body["window_days"] == 1
        assert body["total_friend_requested"] == 1

        other = (dt.datetime.utcnow() - dt.timedelta(days=10)).date().isoformat()
        r = api_client.get("/lead-mesh/funnel?date=" + other)
        assert r.json()["total_friend_requested"] == 0

    def test_blocked_peers_api_with_date(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        import datetime as dt

        cid = resolve_identity(platform="facebook", account_id="fb:DA2",
                                 display_name="DA2")
        append_journey(cid, actor="agent_a", action="greeting_blocked",
                         data={"reason": "cap_hit"})

        today = dt.datetime.utcnow().date().isoformat()
        r = api_client.get(
            "/lead-mesh/funnel/blocked-peers?reason=cap_hit&date=" + today)
        body = r.json()
        assert body["date"] == today
        assert body["count"] == 1


class TestTimeseriesApi:
    """Phase 8e: GET /lead-mesh/funnel/timeseries."""

    def test_api_empty_returns_days_entries(self, api_client, tmp_db):
        r = api_client.get("/lead-mesh/funnel/timeseries?days=5")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 5
        assert len(body["series"]) == 5
        for p in body["series"]:
            assert p["friend_req"] == 0
            assert p["greeting_sent"] == 0
            assert p["blocked"] == 0

    def test_api_counts_today(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        cid = resolve_identity(platform="facebook",
                                 account_id="fb:TSA", display_name="TSA")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message"})

        r = api_client.get("/lead-mesh/funnel/timeseries?days=3&actor=agent_a")
        body = r.json()
        assert len(body["series"]) == 3
        # 最后一天 = 今天有 counts
        today_pt = body["series"][-1]
        assert today_pt["friend_req"] == 1
        assert today_pt["greeting_sent"] == 1

    def test_api_rejects_invalid_days(self, api_client, tmp_db):
        r = api_client.get("/lead-mesh/funnel/timeseries?days=0")
        assert r.status_code == 422
        r = api_client.get("/lead-mesh/funnel/timeseries?days=365")
        assert r.status_code == 422


class TestBlockedPeersApi:
    """Phase 8d: GET /lead-mesh/funnel/blocked-peers."""

    def test_api_requires_reason(self, api_client, tmp_db):
        # reason 是 required query param
        r = api_client.get("/lead-mesh/funnel/blocked-peers")
        assert r.status_code == 422

    def test_api_empty_returns_empty_list(self, api_client, tmp_db):
        r = api_client.get("/lead-mesh/funnel/blocked-peers?reason=no_message_button")
        assert r.status_code == 200
        body = r.json()
        assert body["reason"] == "no_message_button"
        assert body["count"] == 0
        assert body["peers"] == []

    def test_api_returns_peers_with_reason(self, api_client, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        cid = resolve_identity(platform="facebook", account_id="fb:BP1",
                                 display_name="BP1")
        for _ in range(2):
            append_journey(cid, actor="agent_a", action="greeting_blocked",
                             data={"reason": "no_message_button",
                                   "persona_key": "jp_female_midlife"})

        r = api_client.get("/lead-mesh/funnel/blocked-peers?reason=no_message_button")
        body = r.json()
        assert body["count"] == 1  # 1 个唯一 peer
        assert body["peers"][0]["canonical_id"] == cid
        assert body["peers"][0]["n_blocked"] == 2
        assert body["peers"][0]["persona_key"] == "jp_female_midlife"

    def test_api_limit_enforced(self, api_client, tmp_db):
        r = api_client.get(
            "/lead-mesh/funnel/blocked-peers?reason=x&limit=500")
        assert r.status_code == 422   # le=200
