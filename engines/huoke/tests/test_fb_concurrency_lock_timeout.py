# -*- coding: utf-8 -*-
"""LockTimeoutError subclass — Phase 7c A1 (来自 round 3 review action item)。

契约:
  * device_section_lock 超时 raise LockTimeoutError (而非 raw RuntimeError)
  * LockTimeoutError 是 RuntimeError 子类 → 老代码 ``except RuntimeError`` 仍 catch
  * 错误信息仍含 ``"device_section_lock timeout"`` → B round 3 老 substring match 仍 work
  * B round 4 可改 ``except LockTimeoutError`` 拿清晰类型契约
"""
from __future__ import annotations

import threading
import time

import pytest


class TestLockTimeoutErrorSubclass:
    def test_is_runtimeerror_subclass(self):
        """向后兼容关键: 老 ``except RuntimeError`` 仍能捕获 LockTimeoutError."""
        from src.host.fb_concurrency import LockTimeoutError
        assert issubclass(LockTimeoutError, RuntimeError)

    def test_can_be_caught_as_runtimeerror(self):
        """实际 raise / catch 链路验证子类语义."""
        from src.host.fb_concurrency import LockTimeoutError

        try:
            raise LockTimeoutError("test")
        except RuntimeError as e:
            assert isinstance(e, LockTimeoutError)
        else:
            pytest.fail("LockTimeoutError 未被 except RuntimeError 捕获")


class TestLockTimeoutRaisedOnTimeout:
    def test_second_acquire_times_out_raises_subclass(self):
        """两个线程争同一 (device, section) 锁, 第二个超时 raise LockTimeoutError."""
        from src.host.fb_concurrency import (
            LockTimeoutError,
            device_section_lock,
        )

        # 线程 A 先拿锁 0.5s, 线程 B 用 0.05s timeout 抢必超时
        holder_in = threading.Event()
        holder_release = threading.Event()

        def holder():
            with device_section_lock("dev_t1", "sec_t1", timeout=2.0):
                holder_in.set()
                holder_release.wait(timeout=2.0)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert holder_in.wait(timeout=1.0), "holder 没拿到锁, 测试 setup 失败"

            with pytest.raises(LockTimeoutError) as ei:
                with device_section_lock("dev_t1", "sec_t1", timeout=0.05):
                    pytest.fail("不该拿到锁")

            # 错误信息契约: 含 'device_section_lock timeout' (B round 3 substring match 用)
            assert "device_section_lock timeout" in str(ei.value)
            # 子类断言双保险
            assert isinstance(ei.value, RuntimeError)
        finally:
            holder_release.set()
            t.join(timeout=2.0)


class TestLegacyStringMatchStillWorks:
    """B round 3 时的 _messenger_active_lock catch 用 substring match,
    本测试锁定向后兼容性。"""

    def test_substring_match_in_str_repr(self):
        """raise 之后, 老 ``"device_section_lock timeout" in str(e)`` 仍 truthy."""
        from src.host.fb_concurrency import LockTimeoutError

        try:
            raise LockTimeoutError(
                "device_section_lock timeout: device=abc12345 "
                "section=messenger_active after 30s"
            )
        except RuntimeError as e:
            # B round 3 _messenger_active_lock 上层 catch 的真实代码片段
            assert "device_section_lock timeout" in str(e)
            return
        pytest.fail("未捕获到异常")
