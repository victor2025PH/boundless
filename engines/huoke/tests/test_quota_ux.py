# -*- coding: utf-8 -*-
"""
P4: quota 调度优化 — get_next_slot_eta + executor catch + quota-status API.

回归保护:
- ComplianceGuard.get_next_slot_eta sliding window 计算正确
- _execute_facebook QuotaExceeded → 友好中文 message + meta 字段
- /facebook/devices/{id}/quota-status 返回结构正确
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.behavior.compliance_guard import (
    ActionLimit,
    ComplianceGuard,
    PlatformLimits,
    QuotaExceeded,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def guard(tmp_path):
    db = str(tmp_path / "quota_ux_compliance.db")
    limits = {
        "facebook": PlatformLimits(
            actions={
                "join_group": ActionLimit(hourly=3, daily=10, cooldown_sec=0),
                "add_friend": ActionLimit(hourly=5, daily=30, cooldown_sec=0),
            },
            daily_total=20,
            hourly_total=10,
        )
    }
    return ComplianceGuard(db_path=db, limits=limits)


# ── ComplianceGuard.get_next_slot_eta ────────────────────────────────

class TestGetNextSlotEta:
    def test_zero_when_no_records(self, guard):
        """从未派过任务 → ETA = 0 (现在就能派)."""
        eta = guard.get_next_slot_eta("facebook", "join_group", "")
        assert eta == 0

    def test_zero_when_quota_partial(self, guard):
        """quota 没满 → ETA = 0."""
        for _ in range(2):  # 3/3 才满, 现在只 2/3
            guard.check_and_record("facebook", "join_group", "")
        eta = guard.get_next_slot_eta("facebook", "join_group", "")
        assert eta == 0

    def test_positive_when_hourly_full(self, guard):
        """hourly 满 → ETA > 0 (基于 oldest log + 3600)."""
        for _ in range(3):  # 3/3 满
            guard.check_and_record("facebook", "join_group", "")
        eta = guard.get_next_slot_eta("facebook", "join_group", "")
        # 刚 record 完, oldest ts ≈ now → ETA ≈ 3600
        assert 3590 < eta <= 3600, f"hourly 满后 ETA 应接近 3600s 但是 {eta}"

    def test_independent_per_action(self, guard):
        """join_group 满不影响 add_friend 的 ETA."""
        for _ in range(3):
            guard.check_and_record("facebook", "join_group", "")
        eta_join = guard.get_next_slot_eta("facebook", "join_group", "")
        eta_friend = guard.get_next_slot_eta("facebook", "add_friend", "")
        assert eta_join > 0
        assert eta_friend == 0

    def test_takes_max_of_hourly_and_daily_window(self, guard, tmp_path):
        """hourly + daily 都满时, ETA 取较大者 (后 unblock 的那个)."""
        # 用 daily 限制更紧的小 fixture 验证
        db = str(tmp_path / "max_window.db")
        guard2 = ComplianceGuard(db_path=db, limits={
            "facebook": PlatformLimits(
                actions={"daily_short": ActionLimit(hourly=2, daily=2, cooldown_sec=0)},
                daily_total=10, hourly_total=10,
            )
        })
        for _ in range(2):
            guard2.check_and_record("facebook", "daily_short", "")
        eta = guard2.get_next_slot_eta("facebook", "daily_short", "")
        # daily window=86400 比 hourly 大, 应取 daily 的 ETA (~86400)
        assert eta > 3700, f"双 window 满应取 daily ETA 但 {eta}"


# ── /facebook/devices/{id}/quota-status API 结构 ─────────────────────

class TestQuotaStatusEndpoint:
    """直接调路由函数 (不需 FastAPI TestClient, 因 endpoint 是 plain function)."""

    def test_returns_actions_dict(self, guard):
        from src.host.routers.facebook import fb_quota_status

        with patch("src.behavior.compliance_guard.get_compliance_guard",
                   return_value=guard):
            result = fb_quota_status("DEVICE1")

        assert result["device_id"] == "DEVICE1"
        assert "actions" in result
        # 至少应有 join_group / add_friend (我们 fixture 配的)
        assert "join_group" in result["actions"]
        assert "add_friend" in result["actions"]

    def test_action_schema_complete(self, guard):
        from src.host.routers.facebook import fb_quota_status

        # join_group 执行链路按 device_id 作为 quota account 记录
        for _ in range(3):
            guard.check_and_record("facebook", "join_group", "DEVICE1")

        with patch("src.behavior.compliance_guard.get_compliance_guard",
                   return_value=guard):
            result = fb_quota_status("DEVICE1")

        action = result["actions"]["join_group"]
        # 完整字段
        for k in ("quota_account",
                  "hourly_used", "hourly_limit", "hourly_remaining",
                  "daily_used", "daily_limit", "daily_remaining",
                  "next_slot_eta_seconds", "next_slot_eta_minutes",
                  "available_now"):
            assert k in action, f"missing field: {k}"

        # 满了应该 not available_now
        assert action["quota_account"] == "DEVICE1"
        assert action["hourly_used"] == 3
        assert action["hourly_remaining"] == 0
        assert action["available_now"] is False
        assert action["next_slot_eta_seconds"] > 0
        assert action["next_slot_eta_minutes"] >= 1

    def test_partial_quota_action_available(self, guard):
        """没满时 available_now=True, eta=0."""
        from src.host.routers.facebook import fb_quota_status

        guard.check_and_record("facebook", "join_group", "DEVICE1")  # 1/3

        with patch("src.behavior.compliance_guard.get_compliance_guard",
                   return_value=guard):
            result = fb_quota_status("DEVICE1")

        action = result["actions"]["join_group"]
        assert action["available_now"] is True
        assert action["next_slot_eta_seconds"] == 0
        assert action["hourly_remaining"] == 2

    def test_join_group_status_uses_device_account_not_global_blank(self, guard):
        """旧空账号 join_group 记录不应误判当前设备入群 quota 已满。"""
        from src.host.routers.facebook import fb_quota_status

        for _ in range(3):
            guard.check_and_record("facebook", "join_group", "")

        with patch("src.behavior.compliance_guard.get_compliance_guard",
                   return_value=guard):
            result = fb_quota_status("DEVICE1")

        action = result["actions"]["join_group"]
        assert action["quota_account"] == "DEVICE1"
        assert action["hourly_used"] == 0
        assert action["hourly_remaining"] == 3
        assert action["available_now"] is True


# ── _execute_facebook catch QuotaExceeded ────────────────────────────

class TestExecutorQuotaCatch:
    """_execute_facebook 顶层 catch QuotaExceeded → 友好中文 + meta."""

    def test_quota_exceeded_returns_friendly_msg_with_meta(self):
        """fb.join_group raise QuotaExceeded → return (False, friendly, meta)."""
        from src.host.executor import _execute_facebook

        # mock fb 实例 → join_group raise QuotaExceeded
        manager = MagicMock()

        def raise_quota(*args, **kwargs):
            raise QuotaExceeded("facebook", "join_group", "", "hourly", 3, 3)

        with patch("src.host.executor._fresh_facebook") as mock_fresh:
            fb_mock = MagicMock()
            fb_mock.join_group.side_effect = raise_quota
            mock_fresh.return_value = fb_mock

            with patch("src.behavior.compliance_guard.get_compliance_guard") as mock_guard:
                mock_guard.return_value.get_next_slot_eta.return_value = 1234.5
                ok, msg, meta = _execute_facebook(
                    manager, "DEVICE1", "facebook_join_group",
                    {"group_name": "test_group"},
                )

        assert ok is False
        # 友好中文消息, 不应是 "facebook_join_group 异常: ..." 旧路径
        assert "异常" not in msg, f"不该走 generic exception 路径: {msg}"
        assert "[quota]" in msg
        assert "facebook" in msg and "join_group" in msg
        assert "3/3" in msg
        # meta 含完整 quota 信息
        assert meta is not None
        assert "quota" in meta
        q = meta["quota"]
        assert q["platform"] == "facebook"
        assert q["action"] == "join_group"
        assert q["window"] == "hourly"
        assert q["current"] == 3
        assert q["limit"] == 3
        assert q["eta_seconds"] == 1234
        assert q["eta_minutes"] >= 20  # 1234s ≈ 21 min

    def test_eta_zero_omits_minutes_hint(self):
        """eta_seconds=0 时不应在 message 里加 '约 X 分钟后可派' 误导."""
        from src.host.executor import _execute_facebook

        manager = MagicMock()

        def raise_quota(*args, **kwargs):
            raise QuotaExceeded("facebook", "join_group", "", "hourly", 3, 3)

        with patch("src.host.executor._fresh_facebook") as mock_fresh:
            fb_mock = MagicMock()
            fb_mock.join_group.side_effect = raise_quota
            mock_fresh.return_value = fb_mock

            with patch("src.behavior.compliance_guard.get_compliance_guard") as mock_guard:
                mock_guard.return_value.get_next_slot_eta.return_value = 0
                _ok, msg, meta = _execute_facebook(
                    manager, "DEVICE1", "facebook_join_group",
                    {"group_name": "test_group"},
                )

        assert "分钟后可派" not in msg
        assert meta["quota"]["eta_minutes"] == 0

    def test_extract_members_keyword_flow_preserves_groups_on_join_quota(self):
        """宽关键词已发现群组后，入群 quota 满应返回群组状态而不是丢失现场。"""
        from src.host.executor import _execute_facebook

        manager = MagicMock()

        def raise_quota(*args, **kwargs):
            raise QuotaExceeded(
                "facebook", "join_group", "DEVICE1", "hourly", 3, 3
            )

        with patch("src.host.executor._fresh_facebook") as mock_fresh:
            fb_mock = MagicMock()
            fb_mock.discover_groups_by_keyword.return_value = [
                {
                    "group_name": "猫好き集合",
                    "keyword": "ペット",
                    "member_count": 1200,
                    "requires_join": True,
                }
            ]
            fb_mock.join_group.side_effect = raise_quota
            mock_fresh.return_value = fb_mock

            with patch("src.behavior.compliance_guard.get_compliance_guard") as mock_guard:
                mock_guard.return_value.get_next_slot_eta.return_value = 2220
                ok, msg, meta = _execute_facebook(
                    manager, "DEVICE1", "facebook_extract_members",
                    {
                        "group_name": "ペット",
                        "broad_keyword": True,
                        "max_groups": 1,
                        "join_if_needed": True,
                    },
                )

        assert ok is False
        assert "[quota]" in msg
        assert meta["outcome"] == "join_quota_blocked_after_discovery"
        assert meta["quota"]["eta_minutes"] >= 37
        assert meta["groups"][0]["status"] == "join_quota_blocked"
        assert meta["groups"][0]["next_action"] == "retry_after_join_group_quota"
        assert meta["discovered_groups"][0]["group_name"] == "猫好き集合"
