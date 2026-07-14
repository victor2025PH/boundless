# -*- coding: utf-8 -*-
"""Phase 12.6 (2026-04-25): simulated_duration_ms 估算单测."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def _fast_mode():
    with patch("random.uniform", return_value=0), \
         patch("time.sleep"):
        yield


def _seed_planned_event(device_id, peer_name, *,
                          line_id="along2026", line_account_id=1,
                          canonical_id="", original_device_id=""):
    from src.host.fb_store import record_contact_event
    # Phase 16: skip_sanitize for fake peer names
    return record_contact_event(
        device_id, peer_name, "line_dispatch_planned",
        preset_key=f"line_pool:{line_account_id}",
        meta={"line_id": line_id, "line_account_id": line_account_id,
              "dispatch_mode": "messenger_text",
              "message_template": f"LINE: {line_id}",
              "canonical_id": canonical_id,
              "original_device_id": original_device_id or device_id},
        skip_sanitize=True)


class TestSimulatedDuration:
    def test_zero_sends_zero_duration(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        # 无 events → 0 sent
        fb = MagicMock()
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "dry_run": True,
            "min_interval_sec": 30, "max_interval_sec": 90,
            "max_retry": 0})
        assert stats["sent"] == 0
        assert stats["simulated_duration_ms"] == 0
        assert "0 秒" in stats["simulated_duration_human"]

    def test_single_send_estimates_send_time_only(self, tmp_db):
        """n=1: 无 interval, 只估 send 时间."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "P1", line_account_id=aid,
                             original_device_id="DEV1")
        fb = MagicMock(); fb.send_message.return_value = True
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "dry_run": True,
            "min_interval_sec": 30, "max_interval_sec": 90,
            "max_retry": 0, "estimated_send_ms": 8000})
        assert stats["simulated_duration_ms"] == 8000  # 1 × 8000, 无 sleep

    def test_multi_send_estimates_interval_plus_send(self, tmp_db):
        """n=3, min=30, max=90, avg=60, send=8s: (3-1)*60*1000 + 3*8000 = 144000ms"""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp", daily_cap=100)
        for i in range(3):
            _seed_planned_event("DEV1", f"Q{i}", line_account_id=aid,
                                 original_device_id="DEV1")
        fb = MagicMock(); fb.send_message.return_value = True
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "dry_run": True,
            "min_interval_sec": 30, "max_interval_sec": 90,
            "max_retry": 0, "estimated_send_ms": 8000})
        # 2 * 60 * 1000 + 3 * 8000 = 144000
        assert stats["simulated_duration_ms"] == 144000
        assert "2 分" in stats["simulated_duration_human"]  # 144 s = 2 分 24 秒

    def test_custom_estimated_send_ms(self, tmp_db):
        """允许 caller 覆盖 estimated_send_ms (真机调校后)."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "X", line_account_id=aid,
                             original_device_id="DEV1")
        fb = MagicMock(); fb.send_message.return_value = True
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "dry_run": True,
            "min_interval_sec": 0, "max_interval_sec": 0,
            "max_retry": 0, "estimated_send_ms": 15000})
        assert stats["simulated_duration_ms"] == 15000

    def test_real_run_also_returns_estimate(self, tmp_db):
        """非 dry_run 也带 simulated_duration_ms 字段 (方便对比预估 vs 实际)."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "Real", line_account_id=aid,
                             original_device_id="DEV1")
        fb = MagicMock(); fb.send_message.return_value = True
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24,
            "min_interval_sec": 0, "max_interval_sec": 0,
            "max_retry": 0})
        # 实际 sent=1, 估算 1 * 8000 = 8000
        assert stats["sent"] == 1
        assert stats["simulated_duration_ms"] == 8000
        assert stats["dry_run"] is False
