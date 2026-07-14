# -*- coding: utf-8 -*-
"""Phase 19.x.3 (2026-04-25): polish — severity cooldown / stdev z-score /
dashboard URL endpoint / region 三级 lookup."""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
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


def _seed_event(device_id, peer_name, event_type):
    from src.host.fb_store import record_contact_event
    return record_contact_event(device_id, peer_name, event_type,
                                  meta={"via": "test"},
                                  skip_sanitize=True)


# ═══════════════════════════════════════════════════════════════════
# 19.x.3.1: severity-based cooldown
# ═══════════════════════════════════════════════════════════════════

class TestSeverityCooldown:
    def test_critical_uses_4h_default(self):
        """critical severity 默认 cooldown=4h, 5h 前 fired 应放行."""
        from src.host.executor import _filter_alerts_by_cooldown
        old_iso = (dt.datetime.utcnow()
                    - dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"no_dispatched": old_iso}
        alerts = [{"type": "no_dispatched", "severity": "critical",
                    "message": "x"}]
        # cooldown_hours=24 但 critical 走 severity 4h 默认 → 5h > 4h, 放行
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        assert len(out) == 1

    def test_critical_within_4h_suppressed(self):
        """critical 3h 前 fired, 仍在 4h cooldown 内 → 抑制."""
        from src.host.executor import _filter_alerts_by_cooldown
        recent_iso = (dt.datetime.utcnow()
                       - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"no_dispatched": recent_iso}
        alerts = [{"type": "no_dispatched", "severity": "critical",
                    "message": "x"}]
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        assert out == []

    def test_warning_uses_24h_default(self):
        """warning severity 默认 24h, 1h 前 fired 应抑制."""
        from src.host.executor import _filter_alerts_by_cooldown
        recent = (dt.datetime.utcnow()
                   - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"send_rate_low": recent}
        alerts = [{"type": "send_rate_low", "severity": "warning",
                    "message": "x"}]
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        assert out == []

    def test_severity_cooldowns_override(self):
        """caller 可完全覆盖默认 severity_cooldowns."""
        from src.host.executor import _filter_alerts_by_cooldown
        # 1h 前 fired
        recent = (dt.datetime.utcnow()
                   - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"x": recent}
        alerts = [{"type": "x", "severity": "critical", "message": "x"}]
        # 自定 critical=0.5h → 1h > 0.5h 放行 (但默认 4h 会抑制)
        out = _filter_alerts_by_cooldown(
            alerts, state, cooldown_hours=24,
            severity_cooldowns={"critical": 0})
        assert len(out) == 1

    def test_unknown_severity_falls_back_to_cooldown_hours(self):
        """severity 不在 map 内 → 退化用 cooldown_hours."""
        from src.host.executor import _filter_alerts_by_cooldown
        recent = (dt.datetime.utcnow()
                   - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"weird": recent}
        # severity=foo 不在 default map → 用 cooldown_hours=1
        # 2h > 1h 放行
        alerts = [{"type": "weird", "severity": "foo", "message": "x"}]
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=1)
        assert len(out) == 1


# ═══════════════════════════════════════════════════════════════════
# 19.x.3.2: stdev / z-score / anomaly
# ═══════════════════════════════════════════════════════════════════

class TestTrend7dAnomaly:
    def test_stable_history_low_stdev(self, tmp_db, tmp_path, monkeypatch):
        """5 天 planned 全 10 → stdev 0, z=None, anomaly=False."""
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        for i in range(1, 6):
            d = (dt.datetime.utcnow()
                  - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d}.json").write_text(
                json.dumps({"funnel": {"planned": 10, "sent": 5,
                                          "replied": 1, "send_rate": 0.5}}),
                encoding="utf-8")
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        t7 = stats["summary"]["trend_7d"]
        assert t7["stdev_planned"] == 0.0
        # std=0 → z 不可计算 → None
        assert t7["z_planned"] is None
        assert t7["anomaly"] is False

    def test_huge_outlier_flags_anomaly(self, tmp_db, tmp_path, monkeypatch):
        """seed 6 planned + 5 历史 [10,12,8,6,4] 平均 8 stdev~2.83 →
        |z|=(6-8)/2.83 ~ 0.7, 不触发. 改 seed 100 planned: |z|=(100-8)/2.83 ~ 32 → anomaly."""
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        for i, planned in enumerate([10, 12, 8, 6, 4], start=1):
            d = (dt.datetime.utcnow()
                  - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d}.json").write_text(
                json.dumps({"funnel": {
                    "planned": planned, "sent": planned // 2,
                    "replied": 0, "send_rate": 0.5}}),
                encoding="utf-8")
        # seed 大量 events 让今天 planned 很大
        for i in range(50):
            _seed_l2_lead(f"花子{i}")
            _seed_event("D1", f"花子{i}", "line_dispatch_planned")
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        t7 = stats["summary"]["trend_7d"]
        # stdev 应非 0
        assert t7["stdev_planned"] > 0
        assert t7["z_planned"] is not None
        # |z| 远大于 2 → anomaly
        assert abs(t7["z_planned"]) > 2
        assert t7["anomaly"] is True

    def test_stdev_fields_present(self, tmp_db, tmp_path, monkeypatch):
        """trend_7d 必须有 stdev_planned/sent/replied 三个字段."""
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        for i in range(1, 6):
            d = (dt.datetime.utcnow()
                  - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d}.json").write_text(
                json.dumps({"funnel": {"planned": i, "sent": i,
                                          "replied": 0, "send_rate": 0.5}}),
                encoding="utf-8")
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        t7 = stats["summary"]["trend_7d"]
        for k in ("stdev_planned", "stdev_sent", "stdev_replied",
                   "z_planned", "z_sent", "z_replied", "anomaly"):
            assert k in t7, f"missing key {k}"


# ═══════════════════════════════════════════════════════════════════
# 19.x.3.3: dashboard URL endpoint
# ═══════════════════════════════════════════════════════════════════

class TestDashboardURLEndpoint:
    def _client(self):
        from fastapi.testclient import TestClient
        from src.host.routers.line_pool import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_returns_404_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        c = self._client()
        r = c.get("/line-pool/stats/daily-summary?date=20990101")
        assert r.status_code == 404

    def test_returns_json_when_present(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        payload = {"funnel": {"planned": 5, "sent": 3}, "marker": "ok"}
        (logs / "daily_summary_20260424.json").write_text(
            json.dumps(payload), encoding="utf-8")
        c = self._client()
        r = c.get("/line-pool/stats/daily-summary?date=20260424")
        assert r.status_code == 200
        assert r.json()["marker"] == "ok"

    def test_invalid_date_format_400(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        c = self._client()
        r = c.get("/line-pool/stats/daily-summary?date=2026-04-24")
        assert r.status_code == 400

    def test_default_date_today(self, tmp_path, monkeypatch):
        """无 date param 默认今天 UTC."""
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        today = dt.datetime.utcnow().strftime("%Y%m%d")
        (logs / f"daily_summary_{today}.json").write_text(
            json.dumps({"marker": "today"}), encoding="utf-8")
        c = self._client()
        r = c.get("/line-pool/stats/daily-summary")
        assert r.status_code == 200
        assert r.json()["marker"] == "today"


# ═══════════════════════════════════════════════════════════════════
# 19.x.3.4: region 三级 lookup helper
# ═══════════════════════════════════════════════════════════════════

class TestGetLeadRegion:
    def test_level1_metadata_region_wins(self, tmp_db):
        """meta.region='jp' 直接返 jp, 即使 persona 是 it_."""
        from src.host.line_pool import _get_lead_region
        cid = _seed_l2_lead("Alice", persona="it_female_midlife",
                              region="jp")
        assert _get_lead_region(cid) == "jp"

    def test_level2_persona_prefix_inferred(self, tmp_db):
        """无 meta.region, persona=jp_* → jp."""
        from src.host.line_pool import _get_lead_region
        cid = _seed_l2_lead("Bob", persona="jp_female_midlife")
        # meta 不带 region
        assert _get_lead_region(cid) == "jp"

    def test_level2_it_prefix(self, tmp_db):
        from src.host.line_pool import _get_lead_region
        cid = _seed_l2_lead("Maria", persona="it_female_midlife")
        assert _get_lead_region(cid) == "it"

    def test_returns_empty_when_no_signal(self, tmp_db):
        """无 region/persona 任何线索 → ""."""
        from src.host.lead_mesh import (resolve_identity,
                                          update_canonical_metadata)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:Naked",
                                display_name="Naked")
        # 完全不写 metadata
        from src.host.line_pool import _get_lead_region
        assert _get_lead_region(cid) == ""

    def test_unknown_canonical_id_empty(self, tmp_db):
        from src.host.line_pool import _get_lead_region
        assert _get_lead_region("nonexistent-id") == ""

    def test_infer_helper_known_prefixes(self):
        from src.host.line_pool import _infer_region_from_persona
        assert _infer_region_from_persona("jp_female_midlife") == "jp"
        assert _infer_region_from_persona("it_male_youth") == "it"
        assert _infer_region_from_persona("fr_x") == "fr"
        assert _infer_region_from_persona("zz_x") == ""
        assert _infer_region_from_persona("") == ""
