# -*- coding: utf-8 -*-
"""LLM Router 单测 —— mock 两 backend, 不真调外部 LLM。

覆盖:
    1. yaml 加载 + 默认 fallback (无文件)
    2. yaml 包裹形式 ("llm:" 顶层 vs 扁平)
    3. per_task_override 模型路由
    4. circuit breaker 状态机:
        a) 连续失败 N 次 → 切 OPEN
        b) OPEN 状态下直接走 fallback
        c) recovery 探测成功 → 切回 CLOSED
        d) recovery 探测失败 → 保持 OPEN
    5. primary 成功 / fallback 不被调用
    6. primary + fallback 全失败 → 返回 error
    7. fallback 关闭时 primary 失败 → 直接 error
    8. images 参数传递
    9. thread-safety: 并发调用计数一致
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.host import llm_router as lr
from src.host.llm_router import (
    LLMRouter,
    OllamaBackend,
    AnthropicBackend,
    BackendError,
    _CircuitBreaker,
    load_config,
    llm_complete,
    get_router,
    reset_router,
)


# ════════════════════════════════════════════════════════════════════
# Mock backend
# ════════════════════════════════════════════════════════════════════
class _MockBackend:
    def __init__(self, name: str, *, fail: bool = False, ping_ok: bool = True,
                 reply: str = "ok-reply"):
        self.name = name
        self.fail = fail
        self.ping_ok = ping_ok
        self.reply = reply
        self.call_count = 0
        self.ping_count = 0
        self.last_kwargs: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def complete(self, prompt, *, model, system=None, max_tokens=512,
                 temperature=0.7, images=None, timeout=30.0):
        with self._lock:
            self.call_count += 1
            self.last_kwargs = {
                "prompt": prompt, "model": model, "system": system,
                "max_tokens": max_tokens, "temperature": temperature,
                "images": images, "timeout": timeout,
            }
        if self.fail:
            raise BackendError(f"{self.name}_simulated_fail")
        return f"{self.reply}:{model}"

    def ping(self, timeout=3.0):
        self.ping_count += 1
        return self.ping_ok


def _mk_router(*, primary_fail=False, ping_ok=True, fb_fail=False,
               failure_threshold=3, recovery_check_interval=0.0,
               per_task=None, fallback_enabled=True):
    cfg = {
        "primary": {"backend": "ollama_central", "base_url": "http://x",
                    "model": "qwen2.5:7b", "timeout": 30},
        "fallback": {"enabled": fallback_enabled, "backend": "cloud",
                     "provider": "anthropic",
                     "model": "claude-haiku-4-5-20251001", "timeout": 30},
        "routing_policy": {
            "failure_threshold": failure_threshold,
            "recovery_check_interval": recovery_check_interval,
        },
        "per_task_override": per_task or {},
    }
    primary = _MockBackend("ollama_central", fail=primary_fail, ping_ok=ping_ok)
    fallback = _MockBackend("cloud", fail=fb_fail) if fallback_enabled else None
    router = LLMRouter(config=cfg, primary_backend=primary,
                       fallback_backend=fallback)
    return router, primary, fallback


# ════════════════════════════════════════════════════════════════════
# 1) yaml 加载 + 默认 fallback
# ════════════════════════════════════════════════════════════════════
class TestLoadConfig:
    def test_default_when_missing(self, tmp_path: Path):
        cfg = load_config(tmp_path / "no_such.yaml")
        assert cfg["primary"]["backend"] == "ollama_central"
        assert cfg["primary"]["model"] == "qwen2.5:7b"
        assert cfg["fallback"]["enabled"] is True
        assert cfg["fallback"]["provider"] == "anthropic"
        assert cfg["routing_policy"]["failure_threshold"] == 3
        assert cfg["per_task_override"] == {}

    def test_load_with_llm_wrapper(self, tmp_path: Path):
        p = tmp_path / "cfg.yaml"
        p.write_text(
            "llm:\n"
            "  primary:\n"
            "    base_url: http://10.0.0.1:11434\n"
            "    model: qwen2.5:14b\n"
            "  routing_policy:\n"
            "    failure_threshold: 5\n"
            "  per_task_override:\n"
            "    ai_greeting:\n"
            "      primary_model: qwen2.5:7b\n",
            encoding="utf-8",
        )
        cfg = load_config(p)
        assert cfg["primary"]["base_url"] == "http://10.0.0.1:11434"
        assert cfg["primary"]["model"] == "qwen2.5:14b"
        # 未指定的字段 fallback 到默认
        assert cfg["primary"]["backend"] == "ollama_central"
        assert cfg["routing_policy"]["failure_threshold"] == 5
        assert cfg["routing_policy"]["recovery_check_interval"] == 120
        assert cfg["per_task_override"]["ai_greeting"]["primary_model"] == "qwen2.5:7b"

    def test_load_flat(self, tmp_path: Path):
        p = tmp_path / "cfg.yaml"
        p.write_text(
            "primary:\n  model: m1\n"
            "fallback:\n  enabled: false\n",
            encoding="utf-8",
        )
        cfg = load_config(p)
        assert cfg["primary"]["model"] == "m1"
        assert cfg["fallback"]["enabled"] is False

    def test_real_repo_yaml_loads(self):
        """实际 config/llm_routing.yaml 文件应能正常加载。"""
        cfg = load_config()  # 默认路径
        assert "primary" in cfg
        assert "fallback" in cfg
        assert "routing_policy" in cfg


# ════════════════════════════════════════════════════════════════════
# 2) per_task_override 路由
# ════════════════════════════════════════════════════════════════════
class TestPerTaskOverride:
    def test_no_task_uses_primary_default(self):
        router, primary, _ = _mk_router()
        res = router.complete("hi")
        assert res["ok"] is True
        assert res["model"] == "qwen2.5:7b"
        assert primary.last_kwargs["model"] == "qwen2.5:7b"

    def test_task_override_primary(self):
        router, primary, _ = _mk_router(per_task={
            "handoff_summary": {"primary_model": "qwen2.5:14b",
                                "fallback_model": "claude-sonnet-4-6"},
        })
        res = router.complete("hi", task="handoff_summary")
        assert res["model"] == "qwen2.5:14b"
        assert primary.last_kwargs["model"] == "qwen2.5:14b"

    def test_task_override_fallback_used_when_primary_open(self):
        """primary 熔断后, fallback 应该用 task 指定的 fallback_model。"""
        router, _, fb = _mk_router(
            primary_fail=True, failure_threshold=1,
            per_task={"persona_classify": {
                "primary_model": "qwen2.5vl:7b",
                "fallback_model": "claude-sonnet-4-6",
            }},
        )
        res = router.complete("classify this", task="persona_classify")
        assert res["ok"] is True
        assert res["backend"] == "cloud"
        assert res["model"] == "claude-sonnet-4-6"
        assert fb.last_kwargs["model"] == "claude-sonnet-4-6"

    def test_unknown_task_falls_back_to_default(self):
        router, primary, _ = _mk_router(per_task={
            "ai_greeting": {"primary_model": "qwen2.5:other"},
        })
        res = router.complete("hi", task="not_in_override")
        assert res["model"] == "qwen2.5:7b"


# ════════════════════════════════════════════════════════════════════
# 3) Circuit Breaker 状态机
# ════════════════════════════════════════════════════════════════════
class TestCircuitBreaker:
    def test_n_failures_open_breaker(self):
        router, primary, fb = _mk_router(
            primary_fail=True, failure_threshold=3,
        )
        # 前 3 次 primary 失败, 但每次都 fallback 成功
        for i in range(3):
            res = router.complete("x")
            assert res["ok"] is True
            assert res["backend"] == "cloud", f"iter {i}"
        # 第 3 次失败时已经触发 OPEN
        assert router.breaker.state == _CircuitBreaker.OPEN
        # primary 总共被调 3 次 (达到阈值后停止尝试)
        assert primary.call_count == 3
        assert fb.call_count == 3

    def test_open_state_skips_primary(self):
        router, primary, fb = _mk_router(
            primary_fail=True, failure_threshold=1,
            recovery_check_interval=999,  # 不让恢复触发
        )
        router.complete("a")  # 一次失败就 OPEN
        assert router.breaker.state == _CircuitBreaker.OPEN
        primary.call_count = 0  # 重置
        # 后续 5 次直接走 fallback, 不再调 primary
        for _ in range(5):
            res = router.complete("y")
            assert res["backend"] == "cloud"
        assert primary.call_count == 0
        assert fb.call_count >= 5

    def test_recovery_ping_success_closes_breaker(self):
        router, primary, fb = _mk_router(
            primary_fail=True, failure_threshold=1,
            recovery_check_interval=0.0,  # 立即可探测
        )
        router.complete("a")
        assert router.breaker.state == _CircuitBreaker.OPEN
        # 修复 primary
        primary.fail = False
        primary.ping_ok = True
        primary.call_count = 0

        res = router.complete("b")
        # 应当探活成功 → 切回 CLOSED → 走 primary
        assert router.breaker.state == _CircuitBreaker.CLOSED
        assert res["backend"] == "ollama_central"
        assert primary.call_count == 1

    def test_recovery_ping_fail_keeps_open(self):
        router, primary, fb = _mk_router(
            primary_fail=True, failure_threshold=1,
            recovery_check_interval=0.0,
        )
        router.complete("a")
        primary.ping_ok = False  # ping 失败
        primary.call_count = 0
        res = router.complete("b")
        assert router.breaker.state == _CircuitBreaker.OPEN
        assert res["backend"] == "cloud"
        assert primary.call_count == 0  # 不试 primary
        assert primary.ping_count >= 1

    def test_success_resets_failure_count(self):
        """CLOSED 状态下, 成功一次就清空失败计数。"""
        router, primary, _ = _mk_router(failure_threshold=3)
        router.complete("x")  # 成功
        snap = router.breaker.snapshot()
        assert snap["consecutive_failures"] == 0
        # 模拟 2 次失败 (不到阈值)
        primary.fail = True
        router.complete("a")
        router.complete("b")
        snap = router.breaker.snapshot()
        assert snap["consecutive_failures"] == 2
        assert snap["state"] == _CircuitBreaker.CLOSED
        # 成功一次, 重置
        primary.fail = False
        router.complete("c")
        snap = router.breaker.snapshot()
        assert snap["consecutive_failures"] == 0


# ════════════════════════════════════════════════════════════════════
# 4) 全失败 / fallback 关闭
# ════════════════════════════════════════════════════════════════════
class TestFailures:
    def test_both_fail_returns_error(self):
        router, _, _ = _mk_router(primary_fail=True, fb_fail=True,
                                  failure_threshold=1)
        res = router.complete("x")
        assert res["ok"] is False
        assert res["text"] == ""
        assert res["backend"] == ""
        assert "fallback" in res["error"]

    def test_fallback_disabled_primary_fail_returns_error(self):
        router, primary, fb = _mk_router(
            primary_fail=True, failure_threshold=2,
            fallback_enabled=False,
        )
        assert fb is None
        res = router.complete("x")
        assert res["ok"] is False
        assert res["text"] == ""
        assert primary.call_count == 1


# ════════════════════════════════════════════════════════════════════
# 5) primary 成功路径 / images 透传
# ════════════════════════════════════════════════════════════════════
class TestSuccessPath:
    def test_primary_success_no_fallback_call(self):
        router, primary, fb = _mk_router()
        res = router.complete("hello", system="sys", max_tokens=42,
                              temperature=0.3)
        assert res["ok"] is True
        assert res["backend"] == "ollama_central"
        assert res["text"].startswith("ok-reply")
        assert primary.last_kwargs["system"] == "sys"
        assert primary.last_kwargs["max_tokens"] == 42
        assert primary.last_kwargs["temperature"] == 0.3
        assert fb.call_count == 0

    def test_images_passthrough(self):
        router, primary, _ = _mk_router()
        imgs = ["base64-img-1", "base64-img-2"]
        router.complete("describe", images=imgs)
        assert primary.last_kwargs["images"] == imgs


# ════════════════════════════════════════════════════════════════════
# 6) Thread safety
# ════════════════════════════════════════════════════════════════════
class TestThreadSafety:
    def test_concurrent_calls_counts_match(self):
        router, primary, _ = _mk_router()
        N = 50

        def worker():
            for _ in range(4):
                router.complete("x")

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert primary.call_count == N * 4

    def test_concurrent_failures_only_open_once(self):
        """N 个线程同时打到 primary_fail, 熔断器只开一次。"""
        router, _, _ = _mk_router(primary_fail=True, failure_threshold=3,
                                  recovery_check_interval=999)
        threads = [threading.Thread(target=lambda: router.complete("y"))
                   for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = router.breaker.snapshot()
        # 应该只 OPEN 一次, 不会因为竞态多次累加
        assert snap["state"] == _CircuitBreaker.OPEN
        assert snap["open_count"] == 1


# ════════════════════════════════════════════════════════════════════
# 7) 模块级单例
# ════════════════════════════════════════════════════════════════════
class TestSingleton:
    def test_get_router_idempotent(self):
        reset_router()
        a = get_router()
        b = get_router()
        assert a is b
        reset_router()
        c = get_router()
        assert c is not a

    def test_llm_complete_smoke_no_api_key(self, monkeypatch):
        """无 API key + ollama 不可达, llm_complete 不应抛, 而是返回 ok=False。"""
        reset_router()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # 强制 primary 指向不可达地址 (config/llm_routing.yaml 是 192.168.0.118)
        # 这里只是 smoke 检查不抛异常 —— 我们不真等网络超时。
        # 用 monkeypatch 替换 OllamaBackend.complete 直接抛 BackendError
        monkeypatch.setattr(
            lr.OllamaBackend, "complete",
            lambda self, prompt, **kw: (_ for _ in ()).throw(
                BackendError("net_unreach")),
        )
        # 同时 fallback 抛 (无 api key)
        res = llm_complete("hi", task="ai_greeting", max_tokens=8)
        assert isinstance(res, dict)
        assert res["ok"] is False
        assert "error" in res
        reset_router()


# ════════════════════════════════════════════════════════════════════
# 8) AnthropicBackend 无 api_key 直接抛
# ════════════════════════════════════════════════════════════════════
class TestAnthropicBackend:
    def test_no_api_key_raises(self):
        be = AnthropicBackend(api_key="", base_url="http://x")
        with pytest.raises(BackendError) as e:
            be.complete("hi", model="claude-haiku-4-5-20251001")
        assert "no_api_key" in str(e.value)

    def test_ping_returns_true(self):
        # 设计取舍: 云端 ping 不真请求, 默认返回 True
        be = AnthropicBackend(api_key="k", base_url="http://x")
        assert be.ping() is True
