"""Phase 12：启动 AI 连接探针的短上限（_run_boot_probe）门禁。

不变量：
- 慢探针（云限流/抖动模拟）超过上限 → 返回 True（放行、不阻断启动、不误报坏 key）。
- 快探针 → 用真实结果（True/False 原样透传）。
- 上限 <=0 → 不设限，直接 await（回退旧行为）。
- 探针内部抛异常（真实错误，如 401）→ 由探针自身处理（此处不吞，wait_for 传播其返回）。
"""
import asyncio

import pytest

from src.ai.ai_client import AIClient


class _Cfg:
    def get_ai_config(self):
        return {}

    config = {}

    def __getattr__(self, _n):
        return {}


def _mk_client(boot_timeout):
    c = AIClient(_Cfg())
    c._boot_probe_timeout = boot_timeout
    return c


def test_slow_probe_times_out_and_passes():
    c = _mk_client(0.2)

    async def _slow():
        await asyncio.sleep(5.0)
        return True

    # 慢探针必须在 ~0.2s 被上限截断并放行 True（而非等满 5s）
    import time
    t0 = time.monotonic()
    out = asyncio.run(c._run_boot_probe(_slow()))
    dt = time.monotonic() - t0
    assert out is True
    assert dt < 2.0  # 远小于 5s，证明确实被上限截断


def test_fast_ok_passes_through():
    c = _mk_client(6.0)

    async def _ok():
        return True

    assert asyncio.run(c._run_boot_probe(_ok())) is True


def test_fast_fail_passes_through():
    c = _mk_client(6.0)

    async def _bad():
        return False

    # 快速失败（如 401 坏 key，探针内部已记日志/告警并返回 False）必须原样透传 False
    assert asyncio.run(c._run_boot_probe(_bad())) is False


def test_zero_timeout_is_unbounded():
    c = _mk_client(0.0)

    async def _ok():
        return True

    assert asyncio.run(c._run_boot_probe(_ok())) is True


def test_missing_attr_defaults_unbounded():
    # 直接调 _initialize_openai_compatible 的旧测试路径不设 _boot_probe_timeout：
    # getattr 默认 0 → 不设限，行为不变。
    c = AIClient(_Cfg())
    assert not hasattr(c, "_boot_probe_timeout")

    async def _ok():
        return True

    assert asyncio.run(c._run_boot_probe(_ok())) is True
