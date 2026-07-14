# -*- coding: utf-8 -*-
"""F3 `_messenger_active_lock` wrapper + check_messenger_inbox/
check_message_requests 的锁超时降级测试。

锁契约来自 A→B Q10: 双方 Messenger UI 操作按 (device, 'messenger_active')
section 串行,超时 raise RuntimeError 由调用方 catch 降级。
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from unittest.mock import MagicMock, patch

import pytest


# ─── _messenger_active_lock 单测 ────────────────────────────────────────────

class TestMessengerActiveLock:
    def test_returns_nullcontext_when_module_missing(self):
        """A 的 Phase 5 未 merge 前, fb_concurrency 不存在 → nullcontext。"""
        from src.app_automation import facebook as fb_mod
        # 模拟 ImportError
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "src.host.fb_concurrency":
                raise ImportError("not yet")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=fake_import):
            cm = fb_mod._messenger_active_lock("devA", timeout=1.0)
        # nullcontext 可进可出
        assert cm is not None
        with cm:
            pass  # no raise

    def test_delegates_to_real_lock_when_available(self):
        """A 的 device_section_lock 可导入 → wrapper 转发。"""
        from src.app_automation import facebook as fb_mod

        fake_lock = MagicMock()
        fake_lock.__enter__ = MagicMock(return_value=None)
        fake_lock.__exit__ = MagicMock(return_value=None)

        fake_module = MagicMock()
        fake_module.device_section_lock = MagicMock(return_value=fake_lock)

        with patch.dict("sys.modules",
                        {"src.host.fb_concurrency": fake_module}):
            cm = fb_mod._messenger_active_lock("devA", timeout=5.0)
        assert cm is fake_lock
        fake_module.device_section_lock.assert_called_once_with(
            "devA", "messenger_active", timeout=5.0)

    def test_propagates_runtime_error_from_real_lock_enter(self):
        """A 的实现在超时时 raise RuntimeError — wrapper 不吞。"""
        from src.app_automation import facebook as fb_mod

        @contextmanager
        def timeout_cm(*a, **kw):
            raise RuntimeError("device_section_lock timeout: demo")
            yield  # unreachable

        fake_module = MagicMock()
        fake_module.device_section_lock = timeout_cm

        with patch.dict("sys.modules",
                        {"src.host.fb_concurrency": fake_module}):
            cm = fb_mod._messenger_active_lock("devA", timeout=0.01)
            with pytest.raises(RuntimeError) as ei:
                with cm:
                    pass
            assert "timeout" in str(ei.value)


# ─── check_messenger_inbox 锁超时降级 ───────────────────────────────────────

class TestCheckMessengerInboxLockTimeout:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.wait_think = MagicMock()
        return fb

    def test_timeout_sets_lock_flag_and_returns_stats(self):
        from src.app_automation import facebook as fb_mod
        fb = self._make_fb()

        @contextmanager
        def timeout_cm(device_id, section, timeout):
            raise RuntimeError(
                f"device_section_lock timeout: device={device_id[:8]} "
                f"section={section} after {timeout:.0f}s")
            yield  # unreachable

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=MagicMock()), \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=("growth", {})), \
             patch.dict("sys.modules",
                        {"src.host.fb_concurrency": MagicMock(
                            device_section_lock=timeout_cm)}):
            stats = fb.check_messenger_inbox(max_conversations=5)

        assert stats["lock_timeout"] is True
        assert stats["error"] == "device_busy_messenger_active"
        assert stats["opened"] is False  # 锁都没拿到就没执行主体


# ─── check_message_requests 锁超时降级 ─────────────────────────────────────

class TestCheckMessageRequestsLockTimeout:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.wait_think = MagicMock()
        return fb

    def test_timeout_sets_lock_flag(self):
        from src.app_automation import facebook as fb_mod
        fb = self._make_fb()

        @contextmanager
        def timeout_cm(device_id, section, timeout):
            raise RuntimeError("device_section_lock timeout: demo")
            yield  # unreachable

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=MagicMock()), \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=("growth", {})), \
             patch.dict("sys.modules",
                        {"src.host.fb_concurrency": MagicMock(
                            device_section_lock=timeout_cm)}):
            stats = fb.check_message_requests(max_requests=5)

        assert stats["lock_timeout"] is True
        assert stats["error"] == "device_busy_messenger_active"
        assert stats["opened"] is False


# ─── 锁可用时主流程正常走 ──────────────────────────────────────────────────

class TestNoConflictWhenLockAvailable:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.wait_think = MagicMock()
        return fb

    def test_no_concurrency_module_wraps_with_nullcontext(self):
        """fb_concurrency 未 merge,走 nullcontext,主流程不受影响。"""
        from src.app_automation import facebook as fb_mod
        fb = self._make_fb()

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "src.host.fb_concurrency":
                raise ImportError("Phase 5 not merged yet")
            return real_import(name, *a, **kw)

        fake_u2 = MagicMock()
        fake_u2.app_start = MagicMock()
        fake_u2.app_stop = MagicMock()
        fake_u2.press = MagicMock()

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=fake_u2), \
             patch.object(fb, "_dismiss_dialogs"), \
             patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_list_messenger_conversations",
                          return_value=[]), \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=("growth", {})), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("builtins.__import__", side_effect=fake_import):
            stats = fb.check_messenger_inbox(max_conversations=5)

        # 锁降级为 nullcontext,流程正常走到 opened=True
        assert stats["opened"] is True
        assert stats.get("lock_timeout") is not True
        assert "error" not in stats
