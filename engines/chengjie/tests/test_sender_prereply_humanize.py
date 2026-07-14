"""原生 A 线文本回复前拟人序列（sender.run_prereply_humanize）单测。

锁定：
  - 默认 thinking_delay=0 → 只已读、不挂打字（保持近即时手感）
  - 配了 min/max → 先已读、再挂「正在输入」、停顿后返回（顺序正确）
  - 读回执/打字异常不抛（best-effort）
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from src.client.sender import TelegramSenderMixin


class _FakeClient:
    def __init__(self, events, *, read_boom=False, action_boom=False):
        self._events = events
        self._read_boom = read_boom
        self._action_boom = action_boom

    async def read_chat_history(self, chat_id):
        if self._read_boom:
            raise RuntimeError("read boom")
        self._events.append(("read", chat_id))

    async def send_chat_action(self, chat_id, action):
        if self._action_boom:
            raise RuntimeError("action boom")
        self._events.append(("typing", str(action)))


class _Cfg:
    def __init__(self, thinking_delay):
        self.config = {"telegram": {"reply_humanize": {
            "thinking_delay": thinking_delay}}}

    def get(self, k, d=None):
        return self.config.get(k, d if d is not None else {})


def _sender(thinking_delay, events, **client_kw):
    class _S(TelegramSenderMixin):
        def __init__(self):
            self.config = _Cfg(thinking_delay)
            self.client = _FakeClient(events, **client_kw)
            self.logger = logging.getLogger("prereply")

    return _S()


def _run(coro):
    return asyncio.run(coro)


def test_default_zero_delay_only_marks_read():
    events = []
    s = _sender({"min_sec": 0, "max_sec": 0}, events)
    _run(s.run_prereply_humanize(12345))
    assert events == [("read", 12345)]     # 只已读，无打字（delay=0）


def test_with_delay_reads_then_types(monkeypatch):
    events = []
    # 固定随机延迟为 4s；用假 sleep 免真等（协作器 sleep=asyncio.sleep）
    monkeypatch.setattr("src.client.sender.random.uniform", lambda a, b: 4.0)

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender({"min_sec": 3, "max_sec": 6}, events)
    _run(s.run_prereply_humanize(999))
    # 顺序：先已读，再打字，再 sleep
    assert events[0] == ("read", 999)
    assert ("typing", "ChatAction.TYPING") in events
    assert events.index(("read", 999)) < next(
        i for i, e in enumerate(events) if e[0] == "typing")


def test_read_exception_does_not_raise():
    events = []
    s = _sender({"min_sec": 0, "max_sec": 0}, events, read_boom=True)
    # 不抛
    _run(s.run_prereply_humanize(1))
    assert events == []


def test_none_chat_id_noop():
    events = []
    s = _sender({"min_sec": 5, "max_sec": 5}, events)
    _run(s.run_prereply_humanize(None))
    assert events == []


def test_adaptive_deducts_elapsed_no_typing_when_already_waited(monkeypatch):
    # adaptive=true + elapsed 远超目标 → delay=0 → 只已读、不挂打字
    events = []
    s = _sender({"min_sec": 0, "max_sec": 30, "adaptive": True,
                 "per_char_sec": 0.1, "jitter": 0}, events)
    _run(s.run_prereply_humanize(7, text="短短", elapsed_sec=100.0))
    assert events == [("read", 7)]     # delay 被扣到 0，无 typing/sleep
