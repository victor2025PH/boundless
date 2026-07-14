# -*- coding: utf-8 -*-
"""Messenger fallback 细分归因 + messenger_active 锁 测试 (2026-04-23 post review)。

对应 INTEGRATION_CONTRACT:
  * §7.6 MessengerError 分流矩阵
  * §7.7 device_section_lock("messenger_active") 双方共用

本测试文件只覆盖 A 端 (send_greeting_after_add_friend 的 fallback 路径),
不涉及真实设备 UI, 通过 mock send_message 抛不同 code 验证分流动作。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _stub_fb():
    """构造一个 FacebookAutomation 实例, 但 device manager / u2 全部 stub。"""
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "test_dev_A"
    # 挂一个 greet reason 容器(正常由 __init__ 设, 这里手工补)
    fb._last_greet_skip_reason = ""
    return fb


class _FakeMessengerError(Exception):
    """模拟 B PR #1 的 MessengerError, 带 code 属性。"""
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


# ─── §7.6 分流矩阵 ──────────────────────────────────────────────────────
class TestMessengerErrorPolicy:
    def test_risk_detected_triggers_on_risk_and_reason(self, tmp_db):
        fb = _stub_fb()
        with patch("src.host.fb_account_phase.on_risk") as mock_risk:
            reason = fb._apply_messenger_error_policy(
                "D1", "risk_detected", "hello", "花子")
        assert reason == "fallback_risk_detected"
        mock_risk.assert_called_once_with("D1")

    def test_xspace_blocked_just_logs(self, tmp_db):
        fb = _stub_fb()
        with patch("src.host.fb_account_phase.on_risk") as mock_risk:
            reason = fb._apply_messenger_error_policy(
                "D1", "xspace_blocked", "hi", "花子")
        assert reason == "fallback_xspace_blocked"
        mock_risk.assert_not_called()   # 不触发 cooldown

    def test_recipient_not_found(self, tmp_db):
        fb = _stub_fb()
        reason = fb._apply_messenger_error_policy(
            "D1", "recipient_not_found", "hi", "花子")
        assert reason == "fallback_peer_not_found"

    def test_send_button_missing_records_content_hash(self, tmp_db):
        fb = _stub_fb()
        from src.host.fb_store import list_recent_risk_events
        reason = fb._apply_messenger_error_policy(
            "D1", "send_button_missing", "违禁内容 hello", "花子")
        assert reason.startswith("fallback_content_blocked")
        # 检查 risk event 有 content_blocked 入库
        events = list_recent_risk_events(device_id="D1", hours=1)
        kinds = [e.get("kind") for e in events]
        raw = [e.get("raw_message", "") for e in events]
        assert any("content_blocked" in r for r in raw)

    def test_messenger_unavailable_marks_device(self, tmp_db):
        fb = _stub_fb()
        reason = fb._apply_messenger_error_policy(
            "D1", "messenger_unavailable", "hi", "花子")
        assert reason == "fallback_messenger_unavailable"
        from src.host.fb_store import list_recent_risk_events
        events = list_recent_risk_events(device_id="D1", hours=1)
        raw = [e.get("raw_message", "") for e in events]
        assert any("messenger_not_ready" in r for r in raw)

    def test_unknown_code(self, tmp_db):
        fb = _stub_fb()
        reason = fb._apply_messenger_error_policy(
            "D1", "send_fail", "hi", "花子")
        assert reason == "fallback_fail:send_fail"


# ─── §7.7 messenger_active 锁 ─────────────────────────────────────────────
class TestMessengerActiveLock:
    def test_lock_timeout_raises_handled_as_skip(self, tmp_db):
        """device_section_lock 超时会抛 RuntimeError, fallback 应 catch
        并设 reason=fallback_locked_by_other, 不调 send_message。"""
        from contextlib import contextmanager
        fb = _stub_fb()

        @contextmanager
        def _raise_timeout(did, section, timeout=60.0):
            # 模拟 device_section_lock 超时行为
            raise RuntimeError(f"device_section_lock timeout: {section}")
            yield  # unreachable

        with patch("src.host.fb_concurrency.device_section_lock",
                    _raise_timeout):
            with patch.object(fb, "send_message") as mock_send:
                result = fb._send_greeting_messenger_fallback(
                    did="D1", profile_name="花子", greeting="hi",
                    template_id="yaml:jp:0", persona_key="jp_female_midlife",
                    eff_phase="mature", preset_key="",
                    ai_decision="greeting")
        assert result is False
        assert fb._last_greet_skip_reason == "fallback_locked_by_other"
        mock_send.assert_not_called()

    def test_lock_ok_proceeds_to_send(self, tmp_db):
        """拿到锁 → _send_messenger_greeting_to_peer 被正常调用。

        真 fb_concurrency.device_section_lock 契约: 成功 yield (无值),
        我们直接用真锁 (空锁池, 必然能拿) 验证 success path.
        Phase 7c 重构 (2026-04-24): fallback 走 _send_messenger_greeting_to_peer
        (返回 (ok, code) tuple), 而非旧 send_message (bool).
        """
        fb = _stub_fb()
        with patch.object(fb, "_send_messenger_greeting_to_peer",
                            return_value=(True, "")) as mock_send:
            with patch("src.host.fb_store.record_inbox_message"):
                with patch("src.host.fb_store.record_contact_event"):
                    result = fb._send_greeting_messenger_fallback(
                        did="D_lockok", profile_name="花子", greeting="hi",
                        template_id="yaml:jp:0",
                        persona_key="jp_female_midlife",
                        eff_phase="mature", preset_key="",
                        ai_decision="greeting")
        assert result is True
        assert fb._last_greet_skip_reason == "ok_via_fallback"
        mock_send.assert_called_once()


# ─── Fallback 成功路径 ─────────────────────────────────────────────────
class TestFallbackSuccess:
    def test_success_records_fallback_event(self, tmp_db):
        """成功 fallback → 入库行 template_id 含 |fallback 后缀 + contact event。"""
        fb = _stub_fb()
        with patch.object(fb, "_send_messenger_greeting_to_peer",
                            return_value=(True, "")):
            with patch("src.host.fb_store.record_inbox_message") as mock_inbox:
                with patch("src.host.fb_store.record_contact_event") as mock_evt:
                    result = fb._send_greeting_messenger_fallback(
                        did="D_success", profile_name="花子",
                        greeting="こんにちは", template_id="yaml:jp:1",
                        persona_key="jp_female_midlife",
                        eff_phase="mature", preset_key="name_hunter",
                        ai_decision="greeting")
        assert result is True
        assert fb._last_greet_skip_reason == "ok_via_fallback"
        # 入库参数 template_id 应有 |fallback 后缀
        assert mock_inbox.called
        kwargs = mock_inbox.call_args.kwargs
        assert kwargs.get("template_id", "").endswith("|fallback")
        # contact event 调用过, event_type (第 3 位置参数) 是 greeting_fallback
        assert mock_evt.called


# Phase 7c (2026-04-24): 不再依赖 send_message 的 raise_on_error 参数 —
# _send_greeting_messenger_fallback 现直接调 _send_messenger_greeting_to_peer
# (返回 (ok, code) tuple). 老的 TestBackwardCompatNoRaiseOnError 测试场景
# 不存在了, 删除.
