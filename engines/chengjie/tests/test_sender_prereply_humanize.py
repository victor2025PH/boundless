"""原生 A 线文本回复前拟人序列（sender.run_prereply_humanize）单测。

锁定：
  - 默认 thinking_delay=0 → 只已读、不挂打字（保持近即时手感）
  - 配了 min/max → 先已读、再挂「正在输入」、停顿后返回（顺序正确）
  - 读回执/打字异常不抛（best-effort）
  - thinking_delay 未配置 → **跟随** inbox.l2_autosend.deliver_delay
    （设置页「回复节奏」滑杆的单一节奏源收口，2026-08-04）；
    显式配置的 thinking_delay 永远优先。
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
    def __init__(self, thinking_delay, deliver_delay=None):
        self.config = {"telegram": {"reply_humanize": {
            "thinking_delay": thinking_delay}}}
        if deliver_delay is not None:
            self.config["inbox"] = {"l2_autosend": {
                "deliver_delay": deliver_delay}}

    def get(self, k, d=None):
        return self.config.get(k, d if d is not None else {})


def _sender(thinking_delay, events, *, deliver_delay=None, **client_kw):
    class _S(TelegramSenderMixin):
        def __init__(self):
            self.config = _Cfg(thinking_delay, deliver_delay)
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


def test_missing_thinking_delay_follows_deliver_delay(monkeypatch):
    # thinking_delay 未配置 → 跟随设置页写的 deliver_delay（min=max=5 消随机）
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender(None, events, deliver_delay={"min_sec": 5, "max_sec": 5})
    _run(s.run_prereply_humanize(321, text="一句正常长度的回复内容"))
    assert events[0] == ("read", 321)
    assert any(e[0] == "sleep" for e in events[1:])       # 真等了
    assert sum(s for k, s in events if k == "sleep") == pytest.approx(5.0)


def test_zero_thinking_delay_block_still_follows_deliver_delay(monkeypatch):
    # 出厂基准就是显式 0/0 块 → 必须照样跟随设置页（按「块存在」判定=回落死路，
    # 所有标准部署 A 线永远秒回——2026-08-04 首版实现踩过，此测试钉死语义）
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender({"min_sec": 0, "max_sec": 0, "adaptive": False}, events,
                deliver_delay={"min_sec": 30, "max_sec": 30})
    _run(s.run_prereply_humanize(11, text="回复内容"))
    assert sum(s for k, s in events if k == "sleep") == pytest.approx(30.0)


def test_follow_false_opts_out_of_deliver_delay():
    # 显式 follow:false=「B 线有延迟、A 线保持秒回」的旧行为出口 → 只已读
    events = []
    s = _sender({"min_sec": 0, "max_sec": 0, "follow": False}, events,
                deliver_delay={"min_sec": 30, "max_sec": 30})
    _run(s.run_prereply_humanize(11))
    assert events == [("read", 11)]


def test_nonzero_thinking_delay_wins_over_deliver_delay(monkeypatch):
    # 非零 thinking_delay=A 线独立覆写 → 优先于 deliver_delay
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender({"min_sec": 6, "max_sec": 6}, events,
                deliver_delay={"min_sec": 30, "max_sec": 30})
    _run(s.run_prereply_humanize(11, text="回复内容"))
    assert sum(s for k, s in events if k == "sleep") == pytest.approx(6.0)


def test_empty_thinking_delay_block_follows_deliver_delay(monkeypatch):
    # thinking_delay: {}（空块=未配置）→ 同样回落 deliver_delay
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender({}, events, deliver_delay={"min_sec": 4, "max_sec": 4})
    _run(s.run_prereply_humanize(22, text="回复"))
    assert sum(s for k, s in events if k == "sleep") == pytest.approx(4.0)


def test_deliver_delay_platform_override_applies_to_telegram(monkeypatch):
    # deliver_delay.platform_overrides.telegram 对 A 线生效（platform 透传）
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender(None, events, deliver_delay={
        "min_sec": 2, "max_sec": 2,
        "platform_overrides": {"telegram": {"min_sec": 7, "max_sec": 7}}})
    _run(s.run_prereply_humanize(33, text="回复文本"))
    assert sum(s for k, s in events if k == "sleep") == pytest.approx(7.0)


# ── P2b A 线连发间隔地板（min_gap_sec，2026-08-12 实测 4.1s 双发后补）─────────


def test_gap_floor_pads_when_recent_send(monkeypatch):
    """账本里 6s 前刚发过一条 → 本条延迟被垫到 ≥ 10×0.85−6 = 2.5s（基础只有 1s）。"""
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender(None, events, deliver_delay={
        "min_sec": 1, "max_sec": 1, "min_gap_sec": 10})
    import time as _t
    s._conv_last_sent = {"55": _t.time() - 6.0}
    _run(s.run_prereply_humanize(55, text="回复文本"))
    total = sum(x for k, x in events if k == "sleep")
    assert 2.5 <= total <= 5.5      # 10×[0.85,1.15] − 6


def test_gap_floor_no_prev_send_not_padded(monkeypatch):
    """账本无本会话记录（进程内首条出站）→ 不垫，只有基础延迟。"""
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender(None, events, deliver_delay={
        "min_sec": 1, "max_sec": 1, "min_gap_sec": 10})
    _run(s.run_prereply_humanize(56, text="回复文本"))
    assert sum(x for k, x in events if k == "sleep") == pytest.approx(1.0)


def test_gap_floor_disabled_by_default(monkeypatch):
    """未配 min_gap_sec（默认 0）→ 旧行为（刚发过也不垫）。"""
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender(None, events, deliver_delay={"min_sec": 1, "max_sec": 1})
    import time as _t
    s._conv_last_sent = {"57": _t.time() - 1.0}
    _run(s.run_prereply_humanize(57, text="回复文本"))
    assert sum(x for k, x in events if k == "sleep") == pytest.approx(1.0)


def test_gap_floor_postsleep_recheck_pads_concurrent_send(monkeypatch):
    """并发窗对账（02:40 实测 gap=4.1s 双发的直接解）：本条睡前账本为空 →
    sleep 期间同会话另一条在途回复落地（账本被刷新）→ 睡醒后必须补垫到
    间隔，且垫付期续挂「正在输入」。"""
    events = []
    import time as _t
    s = _sender(None, events, deliver_delay={
        "min_sec": 1, "max_sec": 1, "min_gap_sec": 10})

    async def _fast_sleep(x):
        events.append(("sleep", x))
        # 首次 sleep（基础延迟）期间模拟并发回复落地：刷新账本＝4s 前发出
        if not getattr(s, "_conv_last_sent", None):
            s._conv_last_sent = {"58": _t.time() - 4.0}

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    _run(s.run_prereply_humanize(58, text="回复文本"))
    total = sum(x for k, x in events if k == "sleep")
    # 基础 1s + 垫付 (10×[0.85,1.15]−4 ∈ [4.5,7.5]) → [5.5, 8.5]
    assert 5.5 <= total <= 8.5
    # 垫付段有续挂打字气泡（在基础延迟的 typing 之外至少再挂一次）
    assert sum(1 for e in events if e[0] == "typing") >= 2


def test_gap_floor_postsleep_no_change_no_double_pad(monkeypatch):
    """账本在 sleep 期间**没变**（就是睡前那条旧记录）→ 不重掷抖动、不双垫。"""
    events = []

    async def _fast_sleep(_s):
        events.append(("sleep", _s))

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    s = _sender(None, events, deliver_delay={
        "min_sec": 1, "max_sec": 1, "min_gap_sec": 10})
    import time as _t
    s._conv_last_sent = {"59": _t.time() - 6.0}
    _run(s.run_prereply_humanize(59, text="回复文本"))
    total = sum(x for k, x in events if k == "sleep")
    # 只有睡前那一次垫付（fake sleep 不走真实时钟，若双垫总额会到 [5,11]）
    assert 2.5 <= total <= 5.5


def test_postsend_mirror_records_conv_ledger(monkeypatch):
    """发送记账点写账本：_postsend_mirror_and_record 后账本有该会话时间戳。"""
    events = []
    s = _sender(None, events, deliver_delay={"min_sec": 0, "max_sec": 0})
    s._mirror_out_row = lambda *a, **k: None
    s._record_contact_out = lambda *a, **k: None
    import time as _t
    t0 = _t.time()
    s._postsend_mirror_and_record(60, "hello")
    assert s._conv_last_sent["60"] >= t0
