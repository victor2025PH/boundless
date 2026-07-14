# -*- coding: utf-8 -*-
"""Phase 19 (2026-04-25): daily summary trend / alerts / per-region funnel."""
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


def _seed_l2_lead(name: str, persona: str = "jp_female_midlife") -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    update_canonical_metadata(cid, {
        "l2_score": 80, "l2_persona_key": persona,
        "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
    }, tags=["l2_verified"])
    return cid


def _seed_event(device_id, peer_name, event_type):
    from src.host.fb_store import record_contact_event
    return record_contact_event(device_id, peer_name, event_type,
                                  meta={"via": "test"},
                                  skip_sanitize=True)


# ═══════════════════════════════════════════════════════════════════
# 19.1: trend
# ═══════════════════════════════════════════════════════════════════

class TestTrendDiff:
    def test_no_yesterday_file_trend_null(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": True, "send_webhook": False,
            "regions": []})
        assert ok
        assert stats["summary"]["trend"] is None

    def test_yesterday_file_present_trend_diff(self, tmp_db, tmp_path,
                                                  monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        # 写昨天的文件
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        yest = (dt.datetime.utcnow() - dt.timedelta(days=1)).strftime("%Y%m%d")
        yest_file = logs / f"daily_summary_{yest}.json"
        yest_file.write_text(json.dumps({
            "funnel": {"planned": 5, "sent": 3, "replied": 1, "send_rate": 0.6}
        }), encoding="utf-8")
        # 当前 funnel 应为 0/0/0 (空 DB) → diff = 0-5/0-3/0-1
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": True, "send_webhook": False,
            "regions": []})
        t = stats["summary"]["trend"]
        assert t is not None
        assert t["planned_delta"] == -5
        assert t["sent_delta"] == -3
        assert t["replied_delta"] == -1
        assert t["yesterday_planned"] == 5


# ═══════════════════════════════════════════════════════════════════
# 19.2: alerts
# ═══════════════════════════════════════════════════════════════════

class TestAlertsDetection:
    def test_no_alerts_when_empty(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        assert stats["summary"]["alerts"] == []

    def test_send_rate_low_triggers_alert(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        # seed: 6 planned 但 0 sent (send_rate=0)
        for i in range(6):
            _seed_l2_lead(f"花子{i}")
            _seed_event("D1", f"花子{i}", "line_dispatch_planned")
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        alerts = stats["summary"]["alerts"]
        # planned=6 sent=0: 触发 send_rate_low + no_dispatched
        types = {a["type"] for a in alerts}
        assert "send_rate_low" in types
        assert "no_dispatched" in types
        # severity critical for no_dispatched
        nd = next(a for a in alerts if a["type"] == "no_dispatched")
        assert nd["severity"] == "critical"

    def test_reject_threshold_triggers_alert(self, tmp_db, tmp_path,
                                                monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        # 12 个 reject (> 阈值 10)
        for i in range(12):
            record_contact_event("D1", "查看翻译", "greeting_replied")
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        types = {a["type"] for a in stats["summary"]["alerts"]}
        assert "reject_rate_high" in types

    def test_threshold_params_overridable(self, tmp_db, tmp_path, monkeypatch):
        """params 可覆盖默认阈值."""
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        for _ in range(3):
            record_contact_event("D", "查看翻译", "greeting_replied")
        monkeypatch.chdir(tmp_path)
        # 阈值 2 应该触发 (3 reject > 2)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": [], "alert_reject_threshold": 2})
        types = {a["type"] for a in stats["summary"]["alerts"]}
        assert "reject_rate_high" in types


# ═══════════════════════════════════════════════════════════════════
# 19.3: per-region funnel
# ═══════════════════════════════════════════════════════════════════

class TestPerRegionFunnel:
    def test_funnel_no_filter_returns_all(self, tmp_db):
        from src.host.line_pool import referral_funnel
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_event("D1", "花子", "line_dispatch_planned")
        f = referral_funnel(hours_window=24)
        assert f["planned"] == 1
        assert f["filter_applied"] is False

    def test_funnel_jp_filter_keeps_jp_persona(self, tmp_db):
        from src.host.line_pool import referral_funnel
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_l2_lead("Maria", persona="it_female_midlife")
        _seed_event("D1", "花子", "line_dispatch_planned")
        _seed_event("D1", "Maria", "line_dispatch_planned")
        # region=jp 只留 jp persona
        f = referral_funnel(hours_window=24, region="jp")
        assert f["planned"] == 1  # 只 花子
        assert f["filter_applied"] is True
        # region=it 只留 it persona
        f2 = referral_funnel(hours_window=24, region="it")
        assert f2["planned"] == 1

    def test_funnel_persona_key_filter(self, tmp_db):
        from src.host.line_pool import referral_funnel
        _seed_l2_lead("A", persona="jp_female_midlife")
        _seed_l2_lead("B", persona="jp_male_youth")
        _seed_event("D1", "A", "line_dispatch_planned")
        _seed_event("D1", "B", "line_dispatch_planned")
        f = referral_funnel(hours_window=24,
                              persona_key="jp_female_midlife")
        assert f["planned"] == 1

    def test_summary_by_region_dict(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_l2_lead("Maria", persona="it_female_midlife")
        _seed_event("D1", "花子", "line_dispatch_planned")
        _seed_event("D1", "Maria", "line_dispatch_planned")
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": ["jp", "it"]})
        by_region = stats["summary"]["by_region"]
        assert "jp" in by_region
        assert "it" in by_region
        assert by_region["jp"].get("planned") == 1
        assert by_region["it"].get("planned") == 1
