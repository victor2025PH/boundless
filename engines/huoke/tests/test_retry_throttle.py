"""P2-④ smart_retry 风暴防护单测.

覆盖:
- _retry_throttle_check 5min 窗口内 ≤3 次允许, 第 4 次拒绝
- _retry_throttle_check 跨设备隔离 (A 用满不影响 B)
- _compute_retry_depth 系统 retry_count + 手动 _retry_of 链长合并
"""
from __future__ import annotations

import time

import pytest

from src.host.routers import tasks as tasks_module


@pytest.fixture(autouse=True)
def _reset_throttle():
    """每个测试隔离：清空全局 throttle 状态."""
    with tasks_module._RETRY_THROTTLE_LOCK:
        tasks_module._RETRY_THROTTLE.clear()
    yield
    with tasks_module._RETRY_THROTTLE_LOCK:
        tasks_module._RETRY_THROTTLE.clear()


def test_throttle_allows_first_three_then_blocks_fourth():
    did = "DEVICE_A"
    for i in range(tasks_module._RETRY_THROTTLE_MAX):
        allowed, count = tasks_module._retry_throttle_check(did)
        assert allowed, f"call {i+1} should be allowed"
        assert count == i + 1
    allowed, count = tasks_module._retry_throttle_check(did)
    assert not allowed, "4th call should be blocked"
    assert count == tasks_module._RETRY_THROTTLE_MAX


def test_throttle_isolated_per_device():
    """设备 A 用满不影响设备 B（防一台手机连点导致全集群 retry 拒绝）."""
    did_a = "DEVICE_A"
    did_b = "DEVICE_B"
    for _ in range(tasks_module._RETRY_THROTTLE_MAX):
        tasks_module._retry_throttle_check(did_a)
    blocked_a, _ = tasks_module._retry_throttle_check(did_a)
    allowed_b, count_b = tasks_module._retry_throttle_check(did_b)
    assert not blocked_a
    assert allowed_b
    assert count_b == 1


def test_throttle_window_expires():
    """5min 窗口外的 timestamp 应被清掉，新调用允许."""
    did = "DEVICE_A"
    # 注入 4 个"6 分钟前"的旧 timestamp，模拟窗口过期
    expired = time.time() - tasks_module._RETRY_THROTTLE_WINDOW_S - 60
    with tasks_module._RETRY_THROTTLE_LOCK:
        tasks_module._RETRY_THROTTLE[did] = [expired] * 5
    allowed, count = tasks_module._retry_throttle_check(did)
    assert allowed, "expired timestamps must be cleared, new call allowed"
    assert count == 1


def test_throttle_empty_device_id_passes():
    """device_id 为空时不限流（避免无设备绑定的任务被误锁）."""
    allowed, count = tasks_module._retry_throttle_check("")
    assert allowed
    assert count == 0


def test_compute_retry_depth_combines_system_and_manual():
    """retry_count（系统重试）+ _retry_of（手动 retry 链）合并."""
    # 仅系统自动重试 2 次
    t1 = {"retry_count": 2, "params": {}}
    assert tasks_module._compute_retry_depth(t1) == 2

    # 仅手动 retry 1 跳
    t2 = {"retry_count": 0, "params": {"_retry_of": "abc-123"}}
    assert tasks_module._compute_retry_depth(t2) == 1

    # 系统 + 手动 叠加（最危险的场景）
    t3 = {"retry_count": 3, "params": {"_retry_of": "abc-123"}}
    assert tasks_module._compute_retry_depth(t3) == 4


def test_retry_depth_max_constant_sane():
    """阈值不能太松（>10 失去防护意义）也不能太紧（<3 误伤合理重试）."""
    assert 3 <= tasks_module._RETRY_DEPTH_MAX <= 10
    assert 1 <= tasks_module._RETRY_THROTTLE_MAX <= 5
    assert 60 <= tasks_module._RETRY_THROTTLE_WINDOW_S <= 1800
