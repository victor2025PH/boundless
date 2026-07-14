# -*- coding: utf-8 -*-
"""Phase 7a: _is_messenger_installed gate 单测 (2026-04-24).

验证 `allow_messenger_fallback: true` 且 Messenger app 未装时,
send_greeting 走 reason=messenger_not_installed, **不**盲目调
_send_greeting_messenger_fallback (下游 UI 查找全失败 reason 只能变 send_fail,
信息丢失).
"""
from __future__ import annotations

import pytest


def _stub_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "D_p7"
    fb._last_greet_skip_reason = ""
    fb._current_lead_cid = ""
    fb._current_lead_persona = ""
    fb._current_greet_template_id = ""
    fb._messenger_installed_cache = {}
    return fb


class FakeShell:
    """模拟 u2 .shell() 返回不同的 pm list 输出."""
    def __init__(self, installed: bool):
        self._installed = installed

    def __call__(self, cmd: str):
        class R:
            output = "package:com.facebook.orca\n" if self._installed else ""
        return R()


class TestMessengerInstalledDetection:
    def test_not_installed_returns_false(self):
        fb = _stub_fb()
        # 替 _u2 让其返回一个有 .shell 方法的 stub
        class FakeDev:
            shell = FakeShell(installed=False)
        fb._u2 = lambda did=None: FakeDev()
        assert fb._is_messenger_installed("D1") is False

    def test_installed_returns_true(self):
        fb = _stub_fb()
        class FakeDev:
            shell = FakeShell(installed=True)
        fb._u2 = lambda did=None: FakeDev()
        assert fb._is_messenger_installed("D1") is True

    def test_cache_avoids_second_query(self):
        """同一 device 5 分钟内缓存, 不再 call shell."""
        fb = _stub_fb()
        calls = []

        class FakeDev:
            def shell(self, cmd):
                calls.append(cmd)
                class R:
                    output = "package:com.facebook.orca\n"
                return R()
        fb._u2 = lambda did=None: FakeDev()
        fb._is_messenger_installed("D1")
        fb._is_messenger_installed("D1")
        fb._is_messenger_installed("D1")
        assert len(calls) == 1  # 只首次 call

    def test_different_devices_separate_cache(self):
        fb = _stub_fb()
        calls = []

        class FakeDev:
            def shell(self, cmd):
                calls.append(cmd)
                class R:
                    output = "package:com.facebook.orca\n"
                return R()
        fb._u2 = lambda did=None: FakeDev()
        fb._is_messenger_installed("D1")
        fb._is_messenger_installed("D2")
        assert len(calls) == 2

    def test_shell_exception_defaults_to_true(self):
        """shell 抛异常时保守返回 True (让 fallback 去尝试, 避免假阴性拦截)."""
        fb = _stub_fb()

        class FailDev:
            def shell(self, cmd):
                raise RuntimeError("adb server died")
        fb._u2 = lambda did=None: FailDev()
        assert fb._is_messenger_installed("D1") is True
