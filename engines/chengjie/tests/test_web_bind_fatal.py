"""幽灵实例纵深防御门禁（2026-07-22 18:53 事故根治的进程侧一半）。

事故机制：uvicorn 绑定失败在 startup() 里 sys.exit(1)——SystemExit 是
BaseException，旧代码 except OSError/Exception 全接不住；非主线程里
SystemExit 只杀 web 线程，进程带着 Telegram 客户端继续裸奔。

本门禁钉住：
- classify_web_serve_outcome 的定性语义（唯一合法退出 = should_exit 优雅停机）
- handle_web_fatal 默认自杀（exit 78）、exit_on_bind_fail=false 退回旧行为
- start_web_server_thread 端到端接线（SystemExit 真的会触发致命处置）
"""
import threading
import time
from types import SimpleNamespace

import pytest

from src.bootstrap.web_app import (
    classify_web_serve_outcome,
    handle_web_fatal,
    start_web_server_thread,
)


# ── classify：定性纯函数 ────────────────────────────────────────────


def test_graceful_shutdown_is_legal():
    # lifecycle 优雅停机：should_exit=True，无论有无异常都放行
    assert classify_web_serve_outcome(None, True, True, 18799) is None
    assert classify_web_serve_outcome(RuntimeError("x"), True, True, 18799) is None
    assert classify_web_serve_outcome(SystemExit(1), False, True, 18799) is None


def test_systemexit_bind_failure_is_fatal():
    # 事故真实路径：uvicorn startup 内 sys.exit(1)，线程死进程活
    reason = classify_web_serve_outcome(SystemExit(1), False, False, 18799)
    assert reason is not None
    assert "18799" in reason
    assert "幽灵" in reason


def test_oserror_bind_in_use_is_fatal():
    exc = OSError(98, "address already in use")
    reason = classify_web_serve_outcome(exc, False, False, 18899)
    assert reason is not None and "18899" in reason


def test_swallowed_return_without_start_is_fatal():
    # serve() 无异常返回但从未 started 且未被要求退出 = 启动失败被内部吞掉
    reason = classify_web_serve_outcome(None, False, False, 18799)
    assert reason is not None and "从未完成启动" in reason


def test_started_clean_return_not_fatal():
    # 已启动 + 非异常返回：保守放行（竞态防误杀）
    assert classify_web_serve_outcome(None, True, False, 18799) is None


def test_post_start_crash_is_fatal():
    # 启动成功后运行期崩溃（无 should_exit）：无 web 的实例同样不可见，按致命处理
    reason = classify_web_serve_outcome(RuntimeError("boom"), True, False, 18799)
    assert reason is not None and "boom" in reason


# ── handle_web_fatal：处置动作 ──────────────────────────────────────


class _FakeLogger:
    def __init__(self):
        self.critical_calls = []
        self.warning_calls = []

    def critical(self, msg, *args, **kw):
        self.critical_calls.append(msg % args if args else msg)

    def warning(self, msg, *args, **kw):
        self.warning_calls.append(msg % args if args else msg)


def _fake_assistant(web_admin_cfg=None):
    return SimpleNamespace(
        logger=_FakeLogger(),
        config=SimpleNamespace(config={"web_admin": web_admin_cfg or {}}),
    )


def test_handle_web_fatal_exits_by_default():
    a = _fake_assistant()
    exits = []
    handle_web_fatal(a, "端口 18799 已被占用", 18799, _exit=exits.append)
    assert exits == [78]
    assert a.logger.critical_calls, "必须留 CRITICAL 第一现场"


def test_handle_web_fatal_optout_keeps_old_behavior():
    a = _fake_assistant({"exit_on_bind_fail": False})
    exits = []
    handle_web_fatal(a, "端口 18799 已被占用", 18799, _exit=exits.append)
    assert exits == []
    assert a.logger.warning_calls and not a.logger.critical_calls


def test_handle_web_fatal_survives_broken_config():
    # config 结构异常也不能让处置本身崩：默认仍自杀
    a = SimpleNamespace(logger=_FakeLogger(), config=None)
    exits = []
    handle_web_fatal(a, "x", 1, _exit=exits.append)
    assert exits == [78]


# ── start_web_server_thread：端到端接线 ─────────────────────────────


class _FakeServer:
    """serve() 按脚本抛异常/返回，模拟 uvicorn 行为。"""

    def __init__(self, outcome, started=False, should_exit=False):
        self._outcome = outcome
        self.started = started
        self.should_exit = should_exit

    async def serve(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _wire(monkeypatch, server):
    """跑线程并捕获 handle_web_fatal 调用（不真退进程）。"""
    import src.bootstrap.web_app as mod

    calls = []
    monkeypatch.setattr(mod, "handle_web_fatal", lambda a, r, p: calls.append((r, p)))
    assistant = _fake_assistant()
    t = start_web_server_thread(assistant, server, "127.0.0.1", 18799)
    t.join(timeout=5)
    assert not t.is_alive()
    return calls


def test_thread_systemexit_triggers_fatal(monkeypatch):
    calls = _wire(monkeypatch, _FakeServer(SystemExit(1)))
    assert len(calls) == 1 and "18799" in calls[0][0]


def test_thread_graceful_shutdown_no_fatal(monkeypatch):
    calls = _wire(monkeypatch, _FakeServer(None, started=True, should_exit=True))
    assert calls == []


def test_thread_optout_flag_really_no_exit():
    # 不打桩、真跑 handle_web_fatal，用 exit_on_bind_fail=false 验证全链路不退进程
    assistant = _fake_assistant({"exit_on_bind_fail": False})
    t = start_web_server_thread(assistant, _FakeServer(SystemExit(1)), "127.0.0.1", 18799)
    t.join(timeout=5)
    assert not t.is_alive()
    assert assistant.logger.warning_calls, "退回旧行为应留 warning"
