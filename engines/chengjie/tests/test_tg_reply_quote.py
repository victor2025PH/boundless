"""Telegram 原生引用回复（``reply_to`` → ``reply_to_message_id``）门禁。

2026-07-31 背景：编排器 ``send()`` 早有逐级降级探测、WhatsApp/LINE 两个 worker 也已接原生
引用，**独漏 Telegram**——而生产实跑的正是 companion 档、且 TG 是最大流量平台。本组钉住：

1. 两个 TG worker（协议薄壳 / A 线 companion）都把 ``reply_to.id`` 透传成
   pyrogram ``reply_to_message_id``；
2. 引用是气泡装饰，其缺失/失败**绝不阻断投递**——非数字 id、被引用消息太旧/已删、
   A 线壳尚未接该参数（patch 未同步）等，一律回落普通发送且整条消息仍送达；
3. A 线发送栈（``send_message_return_id`` → ``_send_text_guarded`` → client）透传不丢参。

纯 mock、无 IO，构造 worker 只做纯赋值。
"""
from __future__ import annotations

import logging

import pytest

from src.client.sender import TelegramSenderMixin
from src.integrations.account_orchestrator import TelegramProtocolWorker
from src.integrations.telegram_companion_worker import TelegramCompanionWorker


class _FakeMsg:
    id = 4242


class _RecordingPyroClient:
    """记录 send_message 收到的 reply_to_message_id；可选对「带引用」的调用抛错。"""

    def __init__(self, *, fail_on_reply: bool = False) -> None:
        self.calls: list = []
        self._fail_on_reply = fail_on_reply

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        self.calls.append({"chat_id": chat_id, "text": text,
                           "reply_to_message_id": reply_to_message_id})
        if self._fail_on_reply and reply_to_message_id is not None:
            raise RuntimeError("message to reply not found (too old / deleted)")
        return _FakeMsg()


def _protocol_worker(client) -> TelegramProtocolWorker:
    w = TelegramProtocolWorker({"account_id": "t1", "meta": {}}, {})
    w.client = client
    return w


# ── 协议薄壳 worker（self.client = pyrogram，直接透传）───────────────────────

@pytest.mark.asyncio
async def test_protocol_worker_forwards_reply_id():
    c = _RecordingPyroClient()
    res = await _protocol_worker(c).send("123", "hi", reply_to={"id": "999"})
    assert res["delivered"] is True
    assert c.calls[-1]["reply_to_message_id"] == 999


@pytest.mark.asyncio
async def test_protocol_worker_no_reply_is_plain():
    c = _RecordingPyroClient()
    await _protocol_worker(c).send("123", "hi")
    assert c.calls[-1]["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_protocol_worker_nonnumeric_ref_is_plain():
    # 非数字 id 解析不出 → 直接普通发送（只调一次，不试引用）。
    c = _RecordingPyroClient()
    await _protocol_worker(c).send("123", "hi", reply_to={"id": "not-a-number"})
    assert len(c.calls) == 1
    assert c.calls[0]["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_protocol_worker_reply_failure_falls_back_plain():
    # 引用发送抛错（被引用消息太旧/已删）→ 回落普通发送，整条消息仍送达。
    c = _RecordingPyroClient(fail_on_reply=True)
    res = await _protocol_worker(c).send("123", "hi", reply_to={"id": "999"})
    assert res["delivered"] is True
    assert c.calls[0]["reply_to_message_id"] == 999   # 第一次带引用抛错
    assert c.calls[1]["reply_to_message_id"] is None  # 回落普通发送


# ── companion worker（A 线，经 send_message_return_id 包装）───────────────────

class _RecordingALine:
    """A 线 TelegramClient 桩：记录 reply_to_message_id；可模拟旧壳（不接该 kwarg）。"""

    def __init__(self, *, accept_reply: bool = True) -> None:
        self.calls: list = []
        self._accept_reply = accept_reply

    async def send_message_return_id(self, chat_id, text, **kw):
        if "reply_to_message_id" in kw and not self._accept_reply:
            raise TypeError("old A-line shell has no reply_to_message_id kwarg")
        self.calls.append({"chat_id": chat_id, "text": text,
                           "reply_to_message_id": kw.get("reply_to_message_id")})
        return True, "555"


def _companion_worker(client) -> TelegramCompanionWorker:
    w = TelegramCompanionWorker({"account_id": "t2", "meta": {}}, {})
    w.client = client
    return w


@pytest.mark.asyncio
async def test_companion_forwards_reply_id():
    c = _RecordingALine()
    res = await _companion_worker(c).send("123", "hi", reply_to={"id": "888"})
    assert res["delivered"] is True
    assert res["message_id"] == "555"
    assert c.calls[-1]["reply_to_message_id"] == 888


@pytest.mark.asyncio
async def test_companion_no_reply_is_plain():
    c = _RecordingALine()
    await _companion_worker(c).send("123", "hi")
    assert c.calls[-1]["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_companion_old_shell_typeerror_falls_back():
    # A 线壳未接 reply_to_message_id（本 patch 未同步到 sender.py）→ TypeError 回落无引用，不崩。
    c = _RecordingALine(accept_reply=False)
    res = await _companion_worker(c).send("123", "hi", reply_to={"id": "888"})
    assert res["delivered"] is True
    assert c.calls[-1]["reply_to_message_id"] is None


# ── A 线发送栈透传（send_message_return_id → _send_text_guarded → client）──────

class _StubSender(TelegramSenderMixin):
    """最小 mixin 宿主：把 _send_text_guarded 依赖的护栏全 stub 成 no-op，验证真实透传链。"""

    def __init__(self, client) -> None:
        self.client = client
        self.logger = logging.getLogger("test_tg_reply_quote")

    def _dead_peer_guard(self):
        return (False, None)

    def _presend_blocked(self):
        return False

    async def _presend_pace(self):
        return None

    def _postsend_record_count(self):
        return None

    def _is_peer_invalid_error(self, e):
        return False

    def _handle_send_exc(self, e):
        return None


@pytest.mark.asyncio
async def test_sender_stack_forwards_reply_id():
    c = _RecordingPyroClient()
    ok, mid = await _StubSender(c).send_message_return_id(
        1, "hi", reply_to_message_id=777)
    assert ok is True
    assert mid == "4242"
    assert c.calls[-1]["reply_to_message_id"] == 777


@pytest.mark.asyncio
async def test_sender_stack_default_no_reply():
    c = _RecordingPyroClient()
    ok = await _StubSender(c).send_message(1, "hi")
    assert ok is True
    assert c.calls[-1]["reply_to_message_id"] is None
