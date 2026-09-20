# -*- coding: utf-8 -*-
"""boot 启动探针后台化契约（P3-1 2026-08-12 可靠性复盘）。

钉住的语义（改 ai_client.initialize / _deferred_boot_probe 前先读）：
- ``initialize(defer_probe=True)``（仅 main.py 冷启动传）：不 await 探针即返回
  True——assistant.initialize() 本就不消费返回值，阻塞探针在 boot 纯花时间
  （实测 4.3s = init 段最大单项）；探针在后台任务照跑，日志/告警语义保留。
- ``initialize()`` 缺省阻塞：探针失败必须返回 False——reload_ai_runtime
  （桌面「模式切换是否生效」UX）消费这个布尔值，语义不许漂。
- local/local_only 分支在后台探针里同构保留（先探本地，local 失败再探云端）。

全程假探针注入，零真网络。
"""
from __future__ import annotations

import asyncio

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {}, "ai": {}}

    def __init__(self, ai: dict):
        self._ai = ai

    def get_ai_config(self):
        return self._ai


def _ai_cfg(**over) -> dict:
    cfg = {
        "provider": "openai_compatible",
        "api_key": "sk-test-not-a-placeholder",
        # 黑洞地址：探针若真的发网络请求，测试桩没生效会立刻暴露
        "base_url": "http://127.0.0.1:9/v1",
        "model": "deepseek-chat",
        "boot_probe_timeout_sec": 6.0,
    }
    cfg.update(over)
    return cfg


async def test_defer_probe_returns_before_probe_and_runs_in_background():
    c = AIClient(_Cfg(_ai_cfg()))
    started = asyncio.Event()
    release = asyncio.Event()
    ran = {"n": 0}

    async def fake_probe():
        started.set()
        await release.wait()          # 探针被卡住——阻塞语义下 initialize 也会被卡
        ran["n"] += 1
        return True

    c._test_openai_connection = fake_probe
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    ok = await c.initialize(defer_probe=True)
    dt = loop.time() - t0
    assert ok is True
    # 结构性证明在于能返回本身：探针卡在 release 上（我们此刻还没放行），若
    # initialize 阻塞等探针必死锁。时间断言只做宽松健全性检查（其余 ~1s 是
    # AsyncOpenAI/嵌入客户端构造等真实开销，忙机上有抖动，别掐太紧）。
    assert dt < 5.0, f"defer 路径耗时异常（{dt:.2f}s），像是在等什么"
    # 后台任务真的在跑：放行后走完
    await asyncio.wait_for(started.wait(), 2)
    release.set()
    await asyncio.wait_for(c._boot_probe_task, 2)
    assert ran["n"] == 1


async def test_blocking_initialize_still_consumes_probe_failure():
    c = AIClient(_Cfg(_ai_cfg()))

    async def fail_probe():
        return False

    c._test_openai_connection = fail_probe
    ok = await c.initialize()
    assert ok is False, "缺省阻塞路径必须继续消费探针结果（reload UX 契约）"


async def test_blocking_initialize_success_unchanged():
    c = AIClient(_Cfg(_ai_cfg()))

    async def ok_probe():
        return True

    c._test_openai_connection = ok_probe
    assert await c.initialize() is True


async def test_defer_probe_local_mode_probes_local_first():
    ai = _ai_cfg(
        primary="local",
        fallback={"enabled": True, "base_url": "http://127.0.0.1:9", "model": "m"},
    )
    c = AIClient(_Cfg(ai))
    calls = []

    async def fake_local():
        calls.append("local")
        return True

    async def fake_cloud():
        calls.append("cloud")
        return True

    c._test_local_connection = fake_local
    c._test_openai_connection = fake_cloud
    assert await c.initialize(defer_probe=True) is True
    await asyncio.wait_for(c._boot_probe_task, 2)
    assert calls == ["local"], "local 模式后台探针应先探本地、本地就绪即止"


async def test_deferred_probe_failure_never_raises():
    c = AIClient(_Cfg(_ai_cfg()))

    async def boom():
        raise RuntimeError("probe exploded")

    c._test_openai_connection = boom
    assert await c.initialize(defer_probe=True) is True
    # 后台任务吞异常收尾，不得向外抛
    await asyncio.wait_for(c._boot_probe_task, 2)
    assert c._boot_probe_task.exception() is None
