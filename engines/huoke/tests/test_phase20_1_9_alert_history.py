# -*- coding: utf-8 -*-
"""Phase 20.1.9 (2026-04-25): alert history 表 + per-region replied_rate +
latency anomaly z-score."""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    from src.host import executor as _ex
    _ex._REFERRAL_KEYWORDS_CACHE["data"] = None
    _ex._peer_region_cache_clear()
    yield


def _seed_l2_lead(name, persona="jp_female_midlife", region=None):
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    meta = {"l2_score": 80, "l2_persona_key": persona,
            "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime())}
    if region is not None:
        meta["region"] = region
    update_canonical_metadata(cid, meta, tags=["l2_verified"])
    return cid


def _seed_event(device, peer, etype, meta=None):
    from src.host.fb_store import record_contact_event
    return record_contact_event(device, peer, etype,
                                  meta=meta or {"via": "test"},
                                  skip_sanitize=True)


# ═══════════════════════════════════════════════════════════════════
# 20.1.9.1: alert history 表
# ═══════════════════════════════════════════════════════════════════

class TestAlertHistory:
    def test_record_and_read_basic(self, tmp_db):
        from src.host.fb_store import record_alert_fired, get_alert_history
        rid = record_alert_fired(
            {"type": "send_rate_low", "severity": "warning",
              "message": "send_rate=10%"},
            context={"funnel": {"planned": 6}})
        assert rid > 0
        h = get_alert_history(hours_window=24)
        assert h["total"] == 1
        assert h["by_type"]["send_rate_low"] == 1
        assert h["by_severity"]["warning"] == 1
        assert len(h["samples"]) == 1
        assert h["samples"][0]["alert_type"] == "send_rate_low"

    def test_filter_by_type(self, tmp_db):
        from src.host.fb_store import record_alert_fired, get_alert_history
        record_alert_fired({"type": "send_rate_low", "severity": "warning",
                              "message": "x"})
        record_alert_fired({"type": "no_dispatched", "severity": "critical",
                              "message": "y"})
        h = get_alert_history(hours_window=24, alert_type="no_dispatched")
        assert h["total"] == 1
        assert h["samples"][0]["alert_type"] == "no_dispatched"

    def test_filter_by_severity(self, tmp_db):
        from src.host.fb_store import record_alert_fired, get_alert_history
        record_alert_fired({"type": "x", "severity": "warning",
                              "message": "x"})
        record_alert_fired({"type": "y", "severity": "critical",
                              "message": "y"})
        h = get_alert_history(hours_window=24, severity="critical")
        assert h["total"] == 1
        assert h["samples"][0]["severity"] == "critical"

    def test_filter_by_region(self, tmp_db):
        from src.host.fb_store import record_alert_fired, get_alert_history
        record_alert_fired({"type": "rl", "severity": "warning", "message": "j"},
                              region="jp")
        record_alert_fired({"type": "rl", "severity": "warning", "message": "i"},
                              region="it")
        h = get_alert_history(hours_window=24, region="jp")
        assert h["total"] == 1
        assert h["samples"][0]["region"] == "jp"

    def test_by_day_aggregation(self, tmp_db):
        from src.host.fb_store import record_alert_fired, get_alert_history
        for _ in range(3):
            record_alert_fired({"type": "x", "severity": "warning",
                                  "message": "y"})
        h = get_alert_history(hours_window=24, by_day=True)
        # 今天 1 个 day key, count=3
        assert sum(h["by_day"].values()) == 3

    def test_record_skips_when_no_type(self, tmp_db):
        from src.host.fb_store import record_alert_fired
        rid = record_alert_fired({"type": "", "severity": "warning"})
        assert rid == 0

    def test_hourly_check_writes_to_history(self, tmp_db, tmp_path,
                                                  monkeypatch):
        """_fb_alert_check_hourly fire 后应有 history 记录."""
        from src.host.executor import _fb_alert_check_hourly
        from src.host.fb_store import get_alert_history
        # 6 planned 0 sent → no_dispatched fire
        for i in range(6):
            _seed_l2_lead(f"花子{i}")
            _seed_event("D1", f"花子{i}", "line_dispatch_planned")
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_alert_check_hourly({"hours_window": 24,
                                                  "cooldown_hours": 24})
        assert len(stats["fired_now"]) > 0
        h = get_alert_history(hours_window=1)
        assert h["total"] >= 1
        types = {s["alert_type"] for s in h["samples"]}
        assert "no_dispatched" in types

    def test_endpoint_returns_history(self, tmp_db, tmp_path, monkeypatch):
        """GET /line-pool/stats/alert-history."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.host.routers.line_pool import router
        from src.host.fb_store import record_alert_fired
        record_alert_fired({"type": "send_rate_low", "severity": "warning",
                              "message": "x"})
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        r = c.get("/line-pool/stats/alert-history?hours_window=24")
        assert r.status_code == 200
        assert r.json()["total"] >= 1


# ═══════════════════════════════════════════════════════════════════
# 20.1.9.2: per-region replied_rate alert
# ═══════════════════════════════════════════════════════════════════

class TestPerRegionAlerts:
    def test_region_label_tags_alerts(self):
        from src.host.executor import _detect_referral_alerts
        funnel = {"planned": 12, "sent": 12, "replied": 1,
                   "send_rate": 1.0}
        alerts = _detect_referral_alerts(funnel, 0, region_label="jp")
        assert all(a.get("region") == "jp" for a in alerts)
        # 至少有 replied_rate_low
        types = {a["type"] for a in alerts}
        assert "replied_rate_low" in types
        # message 应有 [jp] 前缀
        rrl = next(a for a in alerts if a["type"] == "replied_rate_low")
        assert rrl["message"].startswith("[jp]")

    def test_state_key_per_region_independent(self):
        """同 type 不同 region cooldown 互不抑制."""
        from src.host.executor import _filter_alerts_by_cooldown
        old = (dt.datetime.utcnow()
                - dt.timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = (dt.datetime.utcnow()
                   - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # state: replied_rate_low:jp 1h 前 (cooldown 内), :it 25h 前 (过 cd)
        state = {"replied_rate_low:jp": recent,
                  "replied_rate_low:it": old}
        alerts = [
            {"type": "replied_rate_low", "severity": "warning",
              "message": "j", "region": "jp"},
            {"type": "replied_rate_low", "severity": "warning",
              "message": "i", "region": "it"},
        ]
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        # jp 抑制, it 放行
        assert len(out) == 1
        assert out[0]["region"] == "it"

    def test_alert_state_key_helper(self):
        from src.host.executor import _alert_state_key
        assert _alert_state_key({"type": "x"}) == "x"
        assert _alert_state_key({"type": "x", "region": ""}) == "x"
        assert _alert_state_key({"type": "x", "region": "jp"}) == "x:jp"

    def test_daily_summary_per_region_alerts(self, tmp_db, tmp_path,
                                                  monkeypatch):
        """jp lead 12 sent 0 replied → daily summary 应吐 replied_rate_low jp."""
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        for i in range(12):
            _seed_l2_lead(f"花子{i}", persona="jp_female_midlife")
            record_contact_event("D1", f"花子{i}", "line_dispatch_planned",
                                   skip_sanitize=True)
            record_contact_event("D1", f"花子{i}", "wa_referral_sent",
                                   skip_sanitize=True)
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": ["jp", "it"]})
        # 应有 region=jp tagged replied_rate_low
        jp_alerts = [a for a in stats["summary"]["alerts"]
                      if a.get("region") == "jp"
                      and a.get("type") == "replied_rate_low"]
        assert len(jp_alerts) >= 1


# ═══════════════════════════════════════════════════════════════════
# 20.1.9.3: latency anomaly z-score
# ═══════════════════════════════════════════════════════════════════

class TestLatencyAnomaly:
    def test_returns_none_too_few_baseline(self, tmp_path, monkeypatch):
        from src.host.executor import _compute_latency_anomaly
        monkeypatch.chdir(tmp_path)
        result = _compute_latency_anomaly({"avg_min": 5.0})
        # logs/ 没文件 → None
        assert result is None

    def test_returns_none_when_no_today_avg(self, tmp_path, monkeypatch):
        from src.host.executor import _compute_latency_anomaly
        monkeypatch.chdir(tmp_path)
        assert _compute_latency_anomaly({"avg_min": None}) is None
        assert _compute_latency_anomaly({}) is None

    def test_stable_baseline_low_z(self, tmp_path, monkeypatch):
        """5 天历史平均都 10min, 今天 11min → |z| 接近 0."""
        from src.host.executor import _compute_latency_anomaly
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir()
        for i in range(1, 6):
            d = (dt.datetime.utcnow()
                  - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d}.json").write_text(
                json.dumps({"reply_latency": {"avg_min": 10.0}}),
                encoding="utf-8")
        r = _compute_latency_anomaly({"avg_min": 11.0})
        assert r is not None
        assert r["samples"] == 5
        # std 0 (全 10), 但今天 11 ≠ 10, std=0 路径返 z=None anomaly=False
        assert r["anomaly"] is False

    def test_huge_outlier_flags_anomaly(self, tmp_path, monkeypatch):
        """5 天 [10,12,8,11,9] avg≈10 stdev≈1.4, 今天 50min |z|≈28."""
        from src.host.executor import _compute_latency_anomaly
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir()
        for i, lat in enumerate([10, 12, 8, 11, 9], start=1):
            d = (dt.datetime.utcnow()
                  - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d}.json").write_text(
                json.dumps({"reply_latency": {"avg_min": lat}}),
                encoding="utf-8")
        r = _compute_latency_anomaly({"avg_min": 50.0})
        assert r is not None
        assert r["anomaly"] is True
        assert abs(r["z"]) > 2

    def test_anomaly_alert_added_to_daily_summary(self, tmp_db, tmp_path,
                                                       monkeypatch):
        """daily summary alerts 应包含 latency_anomaly."""
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir()
        # 5 天 avg 10min
        for i, lat in enumerate([10, 12, 8, 11, 9], start=1):
            d = (dt.datetime.utcnow()
                  - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d}.json").write_text(
                json.dumps({"reply_latency": {"avg_min": lat}}),
                encoding="utf-8")
        # 今天 1 个 reply, latency=100min (outlier)
        record_contact_event(
            "D1", "X", "wa_referral_replied",
            meta={"latency_min": 100, "latency_seconds": 6000},
            skip_sanitize=True)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        types = {a["type"] for a in stats["summary"]["alerts"]}
        assert "latency_anomaly" in types
