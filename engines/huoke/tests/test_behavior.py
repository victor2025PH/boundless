"""
Tests for HumanBehavior engine and ComplianceGuard.
"""
import os
import sys
import time
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.behavior.human_behavior import (
    HumanBehavior, BehaviorProfile, get_profile,
    _bezier_curve, TypingProfile, SessionProfile,
    PROFILES,
)
from src.behavior.compliance_guard import (
    ComplianceGuard, QuotaExceeded, ActionLimit, PlatformLimits,
)


# ---------------------------------------------------------------------------
# HumanBehavior
# ---------------------------------------------------------------------------

class TestBehaviorProfiles:
    def test_default_profiles_exist(self):
        for name in ("telegram", "linkedin", "whatsapp"):
            p = get_profile(name)
            assert p.name == name
            assert p.typing.mean_interval_ms > 0
            assert p.session.active_mean_min > 0

    def test_linkedin_is_slowest(self):
        tg = get_profile("telegram")
        li = get_profile("linkedin")
        assert li.typing.mean_interval_ms > tg.typing.mean_interval_ms
        assert li.action_delay_mean > tg.action_delay_mean

    def test_unknown_profile_returns_default(self):
        p = get_profile("unknown_platform")
        assert p.name == "unknown_platform"
        assert p.typing.mean_interval_ms == BehaviorProfile().typing.mean_interval_ms


class TestBezierCurve:
    def test_curve_endpoints(self):
        pts = _bezier_curve((100, 200), (500, 800), (20, 50), 10)
        assert len(pts) == 11
        assert pts[0] == (100, 200)
        assert pts[-1] == (500, 800)

    def test_curve_has_intermediate_points(self):
        pts = _bezier_curve((0, 0), (1000, 1000), (30, 70), 20)
        assert len(pts) == 21
        for x, y in pts:
            assert isinstance(x, int) and isinstance(y, int)


class TestHumanBehaviorEngine:
    def test_session_lifecycle(self):
        hb = HumanBehavior(get_profile("telegram"))
        hb.session_start()
        assert hb.session_elapsed >= 0
        assert hb._session_actions == 0
        assert not hb.should_rest()

    def test_warmup_multiplier(self):
        hb = HumanBehavior(BehaviorProfile(
            session=SessionProfile(warmup_duration_min=10, warmup_rate_factor=0.3)
        ))
        hb.session_start()
        mult = hb._warmup_multiplier
        assert 0.28 <= mult <= 0.35, f"At session start, multiplier should be ~0.3, got {mult}"

    def test_session_stats(self):
        hb = HumanBehavior()
        hb.session_start()
        stats = hb.session_stats()
        assert "elapsed_sec" in stats
        assert "actions" in stats
        assert stats["actions"] == 0

    def test_reading_time_proportional(self):
        hb = HumanBehavior()
        hb.session_start()
        t_short = hb._reading_time_for_length(50)
        t_long = hb._reading_time_for_length(5000)
        assert t_long > t_short


# ---------------------------------------------------------------------------
# ComplianceGuard
# ---------------------------------------------------------------------------

class TestComplianceGuard:
    @pytest.fixture
    def guard(self, tmp_path):
        db = str(tmp_path / "test_compliance.db")
        limits = {
            "test_platform": PlatformLimits(
                actions={
                    "send": ActionLimit(hourly=3, daily=10, cooldown_sec=0.1),
                    "search": ActionLimit(hourly=5, daily=20, cooldown_sec=0),
                },
                daily_total=15,
                hourly_total=8,
            )
        }
        return ComplianceGuard(db_path=db, limits=limits)

    def test_check_and_record(self, guard):
        guard.check_and_record("test_platform", "send", "acct1")
        remaining = guard.get_remaining("test_platform", "send", "acct1")
        assert remaining["hourly_used"] == 1
        assert remaining["daily_used"] == 1
        assert remaining["hourly_remaining"] == 2

    def test_hourly_quota_exceeded(self, guard):
        for i in range(3):
            time.sleep(0.12)
            guard.check_and_record("test_platform", "send", "acct1")
        time.sleep(0.12)
        with pytest.raises(QuotaExceeded) as exc:
            guard.check("test_platform", "send", "acct1")
        assert exc.value.window == "hourly"
        assert exc.value.limit == 3

    def test_cooldown_wait(self, guard):
        guard.check_and_record("test_platform", "send", "acct1")
        cd = guard.get_cooldown("test_platform", "send", "acct1")
        assert cd >= 0

    def test_different_accounts_independent(self, guard):
        guard.check_and_record("test_platform", "send", "acct1")
        guard.check_and_record("test_platform", "send", "acct1")
        time.sleep(0.12)
        guard.check_and_record("test_platform", "send", "acct1")
        time.sleep(0.12)
        with pytest.raises(QuotaExceeded):
            guard.check("test_platform", "send", "acct1")
        guard.check("test_platform", "send", "acct2")

    def test_platform_status(self, guard):
        guard.record("test_platform", "send", "acct1")
        status = guard.get_platform_status("test_platform", "acct1")
        assert status["platform"] == "test_platform"
        assert "send" in status["actions"]
        assert "totals" in status

    def test_unknown_platform_unlimited(self, guard):
        guard.check("unknown", "anything", "acct")

    def test_cleanup_old(self, guard):
        guard.record("test_platform", "send", "acct1")
        guard.cleanup_old(days=0)
        remaining = guard.get_remaining("test_platform", "send", "acct1")
        assert remaining["hourly_used"] == 0

    def test_platform_total_limit(self):
        """Platform hourly_total=4 with per-action limits of 10 each.
        Hit platform total before any action limit."""
        db = str(tempfile.mktemp(suffix=".db"))
        limits = {
            "plat": PlatformLimits(
                actions={
                    "a": ActionLimit(hourly=10, daily=50, cooldown_sec=0),
                    "b": ActionLimit(hourly=10, daily=50, cooldown_sec=0),
                },
                daily_total=50,
                hourly_total=4,
            )
        }
        g = ComplianceGuard(db_path=db, limits=limits)
        for _ in range(2):
            g.record("plat", "a", "x")
        for _ in range(2):
            g.record("plat", "b", "x")
        with pytest.raises(QuotaExceeded) as exc:
            g.check("plat", "a", "x")
        assert "total" in exc.value.window
