# -*- coding: utf-8 -*-
"""Phase 19.x (2026-04-25): alert_state 抑制 + hourly task + 7d moving avg."""
from __future__ import annotations

import datetime as dt
import json

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    yield


def _seed_l2_lead(name: str, persona: str = "jp_female_midlife") -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    import time
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
# alert_state 抑制
# ═══════════════════════════════════════════════════════════════════

class TestAlertStateSuppression:
    def test_load_save_round_trip(self, tmp_path, monkeypatch):
        from src.host import executor as ex_mod
        monkeypatch.chdir(tmp_path)
        # 初始为空
        assert ex_mod._load_alert_state() == {}
        # 写入
        state = {"send_rate_low": "2026-04-25T10:00:00Z"}
        ex_mod._save_alert_state(state)
        # 读回
        assert ex_mod._load_alert_state() == state

    def test_filter_within_cooldown_suppressed(self, monkeypatch):
        from src.host.executor import _filter_alerts_by_cooldown
        # state: 1 小时前 fired
        recent_iso = (dt.datetime.utcnow()
                       - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"send_rate_low": recent_iso}
        alerts = [{"type": "send_rate_low", "severity": "warning",
                    "message": "x"}]
        # cooldown 24h: 1h 前还没过, 应抑制
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        assert out == []

    def test_filter_after_cooldown_passes(self):
        from src.host.executor import _filter_alerts_by_cooldown
        # state: 25 小时前 fired (过 24h cooldown)
        old_iso = (dt.datetime.utcnow()
                    - dt.timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"send_rate_low": old_iso}
        alerts = [{"type": "send_rate_low", "severity": "warning",
                    "message": "x"}]
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        assert len(out) == 1

    def test_new_alert_type_passes(self):
        """state 里没记录的新 alert type 立即触发."""
        from src.host.executor import _filter_alerts_by_cooldown
        state = {"send_rate_low": (dt.datetime.utcnow()
                                     - dt.timedelta(hours=1)).strftime(
                                       "%Y-%m-%dT%H:%M:%SZ")}
        alerts = [{"type": "no_dispatched", "severity": "critical",
                    "message": "y"}]
        out = _filter_alerts_by_cooldown(alerts, state, cooldown_hours=24)
        assert len(out) == 1


# ═══════════════════════════════════════════════════════════════════
# hourly alert task
# ═══════════════════════════════════════════════════════════════════

class TestHourlyAlertTask:
    def test_no_alerts_no_webhook(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_alert_check_hourly
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_alert_check_hourly({"hours_window": 24})
        assert ok
        assert stats["all_alerts"] == []
        assert stats["fired_now"] == []
        assert stats["webhook_sent"] is False

    def test_alerts_fire_first_time(self, tmp_db, tmp_path, monkeypatch):
        """6 planned 0 sent → no_dispatched + send_rate_low + 状态变化, fire."""
        from src.host.executor import _fb_alert_check_hourly
        monkeypatch.chdir(tmp_path)
        for i in range(6):
            _seed_l2_lead(f"花子{i}")
            _seed_event("D1", f"花子{i}", "line_dispatch_planned")
        ok, _, stats = _fb_alert_check_hourly({"hours_window": 24})
        types = {a["type"] for a in stats["fired_now"]}
        # 应至少 fire send_rate_low 和 no_dispatched (2 个 alert)
        assert "no_dispatched" in types
        assert stats["suppressed"] == 0  # 首次 fire 无抑制

    def test_alerts_suppressed_second_run(self, tmp_db, tmp_path, monkeypatch):
        """连跑 2 次: 第 1 次 fire, 第 2 次 (cooldown 内) 全抑制."""
        from src.host.executor import _fb_alert_check_hourly
        monkeypatch.chdir(tmp_path)
        for i in range(6):
            _seed_l2_lead(f"花子{i}")
            _seed_event("D1", f"花子{i}", "line_dispatch_planned")
        # 第 1 次
        _, _, s1 = _fb_alert_check_hourly({"hours_window": 24,
                                              "cooldown_hours": 24})
        assert len(s1["fired_now"]) > 0
        # 第 2 次 (立即跑) — 都在 cooldown 内
        _, _, s2 = _fb_alert_check_hourly({"hours_window": 24,
                                              "cooldown_hours": 24})
        assert s2["fired_now"] == []
        assert s2["suppressed"] >= 1  # 至少抑制了 1 个


# ═══════════════════════════════════════════════════════════════════
# trend_7d
# ═══════════════════════════════════════════════════════════════════

class TestTrend7d:
    def test_too_few_files_returns_null(self, tmp_db, tmp_path, monkeypatch):
        """< 3 个历史文件 → trend_7d=null."""
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        # 只放 1 个昨天的 (< 3)
        yest = (dt.datetime.utcnow() - dt.timedelta(days=1)).strftime("%Y%m%d")
        (logs / f"daily_summary_{yest}.json").write_text(
            json.dumps({"funnel": {"planned": 5, "sent": 3}}),
            encoding="utf-8")
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        # trend (vs 昨天) 有, trend_7d (3+ samples) 无
        assert stats["summary"]["trend"] is not None
        assert stats["summary"]["trend_7d"] is None

    def test_three_plus_files_returns_avg_and_ratio(self, tmp_db, tmp_path,
                                                       monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        monkeypatch.chdir(tmp_path)
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        # 5 天历史: planned 平均 (10+12+8+6+4)/5 = 8
        for i, planned in enumerate([10, 12, 8, 6, 4], start=1):
            d_str = (dt.datetime.utcnow()
                       - dt.timedelta(days=i)).strftime("%Y%m%d")
            (logs / f"daily_summary_{d_str}.json").write_text(
                json.dumps({"funnel": {
                    "planned": planned, "sent": planned // 2,
                    "replied": 0, "send_rate": 0.5,
                }}), encoding="utf-8")
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        t7 = stats["summary"]["trend_7d"]
        assert t7 is not None
        assert t7["samples"] == 5
        assert t7["avg_planned"] == 8.0
        assert t7["avg_sent"] == round((5+6+4+3+2)/5, 2)
        # 当天 funnel = 0/0 (空 DB) → ratio 0/8 = 0.0
        assert t7["ratio_planned"] == 0.0
