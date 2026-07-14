# -*- coding: utf-8 -*-
"""通话桥契约门禁：用 fake 传输 + fake 大脑全链验证（无 pytgcalls / 无 live host）。

覆盖：来电决策三分支（接/拒补偿/静默）、出向音频 48k 切 10ms 帧、进向折叠+重采样喂大脑、
转写并行安全监测（severe→拉人+注入 / elevated→温柔指令）、收尾回调、状态机驱动。
外加 audio.py（重采样长度/时长/折叠）与 safety.py（危机判级）纯函数。
"""
import asyncio
import base64
import struct

import pytest

from src.voicecall.audio import (
    downmix_to_mono,
    pcm16_duration_ms,
    resample_pcm16,
    resampled_len,
)
from src.voicecall.bridge import (
    CallContext,
    CallHooks,
    CallResult,
    TelegramCallBridge,
)
from src.voicecall.core import CallAction, CallsConfig, frame_bytes
from src.voicecall.safety import SafetyAction, assess_call_transcript


def _cfg(**answer):
    base = {"telegram_calls": {"enabled": True, "answer": {
        "min_intimacy": 30, "languages": ["zh", "en"], "max_concurrent": 1}}}
    base["telegram_calls"]["answer"].update(answer)
    return CallsConfig.from_config(base)


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeTransport:
    def __init__(self):
        self.answered = []
        self.declined = []
        self.frames = []
        self.hungup = []

    async def answer(self, chat_id):
        self.answered.append(chat_id)

    async def decline(self, chat_id):
        self.declined.append(chat_id)

    async def send_frame(self, chat_id, pcm_frame_48k):
        self.frames.append(pcm_frame_48k)

    async def hangup(self, chat_id):
        self.hungup.append(chat_id)


class FakeBrainSession:
    def __init__(self, events):
        self._events = events
        self.pushed = []
        self.directives = []
        self.closed = False

    async def push_audio(self, pcm):
        self.pushed.append(pcm)

    async def inject_directive(self, text):
        self.directives.append(text)

    async def events(self):
        for ev in self._events:
            yield ev

    async def close(self):
        self.closed = True


class FakeBrain:
    def __init__(self, events):
        self._events = events
        self.opened = []
        self.last_session = None

    async def open(self, ctx):
        self.opened.append(ctx)
        self.last_session = FakeBrainSession(self._events)
        return self.last_session


def _ctx(**kw):
    d = dict(chat_id=555, has_conversation=True, peer_known=True,
             automation_mode="auto_ai", intimacy=70, conversation_language="zh",
             host_warm=True, concurrent_active=0, hour=15)
    d.update(kw)
    return CallContext(**d)


class FakeStats:
    """记录 bridge 打点，验证观测接线。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _rec(*a, **kw):
            self.calls.append((name, a, kw))
        return _rec

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


# ── 来电决策三分支 ───────────────────────────────────────────────────────────
def test_handle_incoming_accept():
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br)
    d = asyncio.run(bridge.handle_incoming(_ctx()))
    assert d.action == CallAction.ACCEPT
    assert tp.declined == []          # 接听不挂断


def test_handle_incoming_compensate_calls_hook():
    tp, br = FakeTransport(), FakeBrain([])
    seen = {}

    async def _comp(ctx, reason):
        seen["reason"] = reason

    bridge = TelegramCallBridge(_cfg(min_intimacy=90), tp, br,
                                hooks=CallHooks(compensate=_comp))
    d = asyncio.run(bridge.handle_incoming(_ctx(intimacy=10)))
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert tp.declined == [555]       # 拒接先挂断
    assert seen["reason"] == "low_intimacy"   # 补偿消息 hook 被调用（绝不冷场）


def test_handle_incoming_silent_no_compensation():
    tp, br = FakeTransport(), FakeBrain([])
    called = {"n": 0}

    async def _comp(ctx, reason):
        called["n"] += 1

    bridge = TelegramCallBridge(_cfg(), tp, br, hooks=CallHooks(compensate=_comp))
    # 陌生人（无会话）→ 静默拒接，绝不给陌生人自动发消息
    d = asyncio.run(bridge.handle_incoming(_ctx(has_conversation=False)))
    assert d.action == CallAction.DECLINE_SILENT
    assert tp.declined == [555]
    assert called["n"] == 0


def test_concurrency_uses_bridge_registry_as_source_of_truth():
    # max_concurrent=1，桥已有 1 通在途 → 即使 caller 传 concurrent_active=0，也判忙线
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(max_concurrent=1), tp, br)
    bridge._sessions[111] = FakeBrainSession([])   # 模拟已有一通活跃
    d = asyncio.run(bridge.handle_incoming(_ctx(chat_id=222, concurrent_active=0)))
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "busy"


def test_quiet_hours_compensate():
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br)
    d = asyncio.run(bridge.handle_incoming(_ctx(hour=3)))   # 凌晨 3 点
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "quiet_hours"


def test_budget_red_light_compensates():
    # 账号风控红灯 → 拒接+补偿（熟人只是号要歇歇，绝不空响）
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br)
    d = asyncio.run(bridge.handle_incoming(_ctx(account_light="red")))
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "account_unhealthy"
    assert d.compensate is True


def test_budget_daily_cap_compensates():
    tp, br = FakeTransport(), FakeBrain([])
    cfg = CallsConfig.from_config({"telegram_calls": {"enabled": True,
        "answer": {"min_intimacy": 30, "languages": ["zh", "en"]},
        "budget": {"daily_calls_cap": 5}}})
    bridge = TelegramCallBridge(cfg, tp, br)
    d = asyncio.run(bridge.handle_incoming(_ctx(calls_today=5)))
    assert d.action == CallAction.DECLINE_COMPENSATE
    assert d.reason == "daily_calls_exhausted"


def test_budget_ok_still_accepts():
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br)
    d = asyncio.run(bridge.handle_incoming(_ctx(calls_today=1, minutes_today=5.0,
                                                account_light="green")))
    assert d.action == CallAction.ACCEPT


# ── 出向音频：大脑 PCM → 48k → 10ms 帧 ───────────────────────────────────────
def test_emit_pcm_frames_10ms():
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br)
    # 大脑吐 24kHz，100ms 音频 → 2400 采样 → 重采样 48k 后应 = 10 帧×10ms
    src_rate = 24000
    n_samples = src_rate * 100 // 1000     # 2400
    pcm = b"\x10\x00" * n_samples
    result = CallResult(chat_id=555)
    sent = asyncio.run(bridge.emit_pcm(555, pcm, src_rate, result))
    fb = frame_bytes(48000, 10)            # 960
    assert all(len(f) == fb for f in tp.frames)
    assert sent == 10                      # 100ms / 10ms
    assert result.out_frames == 10


# ── 进向音频：折叠单声道 + 重采样喂大脑 ───────────────────────────────────────
def test_ingest_frame_to_brain():
    tp = FakeTransport()
    sess = FakeBrainSession([])
    bridge = TelegramCallBridge(_cfg(), tp, FakeBrain([]))
    frame48 = b"\x20\x00" * 480            # 一帧 48k/10ms mono
    asyncio.run(bridge.ingest_frame(sess, frame48, channels=1))
    assert len(sess.pushed) == 1
    # 48k→16k：480 采样 → 160 采样 → 320 字节
    assert len(sess.pushed[0]) == 320


# ── 全链会话：出向音频 + 转写累积 + 收尾 ─────────────────────────────────────
def test_run_session_full_flow():
    pcm = b"\x10\x00" * 2400               # 24k 100ms
    events = [
        {"type": "output_audio", "audio_b64": base64.b64encode(pcm).decode(),
         "sample_rate": 24000},
        {"type": "transcript.user", "text": "今天好累啊"},
        {"type": "transcript.assistant", "text": "辛苦啦，我在呢", "final": True},
        {"type": "session.end"},
    ]
    tp, br = FakeTransport(), FakeBrain(events)
    wrapped = {}

    async def _wrap(ctx, result):
        wrapped["result"] = result

    bridge = TelegramCallBridge(_cfg(), tp, br, hooks=CallHooks(on_wrapup=_wrap))
    result = asyncio.run(bridge.run_session(_ctx()))
    assert result.accepted is True
    assert tp.answered == [555]
    assert tp.hungup == [555]              # 会话结束必挂断
    assert result.out_frames == 10         # 音频下发
    assert result.user_transcript == ["今天好累啊"]
    assert result.assistant_transcript == ["辛苦啦，我在呢"]
    assert br.last_session.closed is True  # 大脑会话释放
    assert wrapped["result"] is result     # 收尾回调
    assert result.end_reason == "normal"


# ── 安全并行监测：severe → 拉人 + 注入；elevated → 温柔指令 ──────────────────
def test_safety_severe_escalates_and_injects():
    events = [
        {"type": "transcript.user", "text": "我不想活了，想结束这一切"},
        {"type": "session.end"},
    ]
    tp, br = FakeTransport(), FakeBrain(events)
    escalations = []

    async def _esc(ctx, level):
        escalations.append(level)

    bridge = TelegramCallBridge(_cfg(), tp, br,
                                hooks=CallHooks(on_human_escalation=_esc))
    result = asyncio.run(bridge.run_session(_ctx()))
    assert result.max_safety_level == "severe"
    assert result.human_escalated is True
    assert escalations == ["severe"]                 # 触发人工介入
    assert len(br.last_session.directives) == 1      # 安全指令已注入下一轮
    assert "安全" in br.last_session.directives[0]


def test_safety_elevated_softens_no_escalation():
    events = [
        {"type": "transcript.user", "text": "我觉得好绝望，什么都没意思了"},
        {"type": "session.end"},
    ]
    tp, br = FakeTransport(), FakeBrain(events)
    escalations = []

    async def _esc(ctx, level):
        escalations.append(level)

    bridge = TelegramCallBridge(_cfg(), tp, br,
                                hooks=CallHooks(on_human_escalation=_esc))
    result = asyncio.run(bridge.run_session(_ctx()))
    assert result.max_safety_level == "elevated"
    assert result.human_escalated is False
    assert escalations == []                         # 不拉人
    assert len(br.last_session.directives) == 1      # 但注入温柔指令


def test_inbound_frame_routes_by_chat_id():
    # on_inbound_frame 按 chat_id 路由到活跃会话；未登记的 chat_id 静默丢弃
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br)
    sess = FakeBrainSession([])
    bridge._sessions[555] = sess
    frame48 = b"\x20\x00" * 480
    asyncio.run(bridge.on_inbound_frame(555, frame48))
    assert len(sess.pushed) == 1           # 已登记 → 喂进大脑
    asyncio.run(bridge.on_inbound_frame(999, frame48))   # 未登记 chat → 丢弃不抛
    assert len(sess.pushed) == 1


def test_active_calls_tracked_during_session():
    # 会话期间 active_calls==1，结束后归零（供并发闸门/观测）
    seen = {}
    events = [{"type": "transcript.user", "text": "hi"}, {"type": "session.end"}]

    class SpyBrain(FakeBrain):
        async def open(self, ctx):
            s = await super().open(ctx)
            seen["during"] = None   # 占位
            return s

    tp = FakeTransport()
    br = SpyBrain(events)
    bridge = TelegramCallBridge(_cfg(), tp, br)

    async def _hook_wrap(ctx, result):
        seen["after"] = bridge.active_calls()

    bridge.hooks = CallHooks(on_wrapup=_hook_wrap)
    assert bridge.active_calls() == 0
    asyncio.run(bridge.run_session(_ctx()))
    assert seen["after"] == 0               # 结束后注销


def test_run_session_brain_open_failure_hangs_up():
    class BadBrain:
        async def open(self, ctx):
            raise RuntimeError("host down")

    tp = FakeTransport()
    bridge = TelegramCallBridge(_cfg(), tp, BadBrain(), stats=FakeStats())
    result = asyncio.run(bridge.run_session(_ctx()))
    assert result.end_reason == "brain_failed"
    assert tp.hungup == [555]              # 大脑挂了也要挂断来电，不留死通话


# ── 观测接线 ─────────────────────────────────────────────────────────────────
def test_stats_wired_on_incoming_decision():
    st = FakeStats()
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br, stats=st)
    asyncio.run(bridge.handle_incoming(_ctx()))
    assert st.count("incoming") == 1
    decided = [c for c in st.calls if c[0] == "decided"]
    assert decided and decided[0][1][0] == "accept"


def test_stats_wired_on_session_lifecycle():
    st = FakeStats()
    events = [{"type": "transcript.user", "text": "hi"}, {"type": "session.end"}]
    tp, br = FakeTransport(), FakeBrain(events)
    bridge = TelegramCallBridge(_cfg(), tp, br, stats=st)
    asyncio.run(bridge.run_session(_ctx()))
    assert st.count("connected") == 1
    assert st.count("ended") == 1
    assert st.count("humanize") == 1       # 收尾滚入拟人计数


def test_stats_safety_escalation_counted():
    st = FakeStats()
    events = [{"type": "transcript.user", "text": "我不想活了"},
              {"type": "session.end"}]
    tp, br = FakeTransport(), FakeBrain(events)
    bridge = TelegramCallBridge(_cfg(), tp, br, stats=st)
    asyncio.run(bridge.run_session(_ctx()))
    esc = [c for c in st.calls if c[0] == "safety_escalation"]
    assert ("safety_escalation", ("severe",), {}) in st.calls


# ── 拟人 emit 闭包：命中 → 取预渲染 PCM → 出向下发 ─────────────────────────────
def test_make_emit_sends_prerendered_frames():
    tp, br = FakeTransport(), FakeBrain([])
    pcm = b"\x10\x00" * 480                # 20ms @24k → 重采样 48k 后 4 帧
    provider_called = {"n": 0}

    async def _provider():
        provider_called["n"] += 1
        return (pcm, 24000)

    bridge = TelegramCallBridge(_cfg(), tp, br, stats=FakeStats())
    result = CallResult(chat_id=555)
    emit = bridge._make_emit(555, _provider, result)
    ok = asyncio.run(emit())
    assert ok is True
    assert provider_called["n"] == 1
    assert len(tp.frames) > 0              # 预渲染音频已下发
    assert all(len(f) == frame_bytes(48000, 10) for f in tp.frames)


def test_make_emit_empty_provider_returns_false():
    tp, br = FakeTransport(), FakeBrain([])

    async def _empty():
        return None

    bridge = TelegramCallBridge(_cfg(), tp, br, stats=FakeStats())
    emit = bridge._make_emit(555, _empty, CallResult(chat_id=555))
    assert asyncio.run(emit()) is False
    assert tp.frames == []


def test_opener_played_on_connect():
    # 接通即播预渲染克隆声开场白（消尴尬沉默）
    played = {"n": 0}

    async def _opener():
        played["n"] += 1
        return (b"\x10\x00" * 480, 24000)

    events = [{"type": "session.end"}]
    tp, br = FakeTransport(), FakeBrain(events)
    bridge = TelegramCallBridge(_cfg(), tp, br, stats=FakeStats(),
                                opener_provider=_opener)
    result = asyncio.run(bridge.run_session(_ctx()))
    assert played["n"] == 1
    assert result.out_frames > 0           # 开场白已下发


def test_on_user_speech_start_routes_to_humanizer():
    # VAD「对方开口」信号按 chat_id 路由到活跃编排器；未登记 chat 静默忽略（不抛）
    tp, br = FakeTransport(), FakeBrain([])
    bridge = TelegramCallBridge(_cfg(), tp, br, stats=FakeStats())
    from src.voicecall.humanize import Humanizer
    hum = Humanizer(_cfg())
    bridge._humanizers[555] = hum
    bridge.on_user_speech_start(555, now=1.0)      # 不抛
    bridge.on_user_speech_start(999, now=1.0)      # 未登记 → 静默忽略


def test_run_session_with_filler_provider_completes_cleanly():
    # 注入 filler_provider → run_session 起并行 humanizer loop；会话结束 loop 干净 cancel（不挂起）
    async def _provider():
        return (b"\x00\x10" * 240, 24000)

    events = [{"type": "transcript.user", "text": "hi"}, {"type": "session.end"}]
    tp, br = FakeTransport(), FakeBrain(events)
    bridge = TelegramCallBridge(_cfg(), tp, br, stats=FakeStats(),
                                filler_provider=_provider, tick_interval_sec=0.05)
    result = asyncio.run(bridge.run_session(_ctx()))
    assert result.end_reason == "normal"
    assert result.duration_sec >= 0.0      # 时长已记
    assert tp.hungup == [555]


# ── audio.py 纯函数 ─────────────────────────────────────────────────────────
def test_resampled_len():
    assert resampled_len(2400, 24000, 48000) == 4800
    assert resampled_len(480, 48000, 16000) == 160
    assert resampled_len(0, 24000, 48000) == 0
    assert resampled_len(100, 16000, 16000) == 100


def test_resample_pcm16_upsample_len():
    pcm = b"\x00\x10" * 160                # 160 采样 @16k
    out = resample_pcm16(pcm, 16000, 48000)
    assert len(out) // 2 == 480            # 上采样 3x


def test_resample_pcm16_identity():
    pcm = b"\x01\x02" * 100
    assert resample_pcm16(pcm, 16000, 16000) == pcm
    assert resample_pcm16(b"", 16000, 48000) == b""


def test_downmix_to_mono():
    # 立体声：L=100 R=300 → mono 200
    stereo = struct.pack("<hh", 100, 300) * 4
    mono = downmix_to_mono(stereo, 2)
    vals = struct.unpack("<4h", mono)
    assert all(v == 200 for v in vals)
    # 单声道原样
    assert downmix_to_mono(b"\x01\x02", 1) == b"\x01\x02"


def test_pcm16_duration_ms():
    assert pcm16_duration_ms(b"\x00\x00" * 16000, 16000) == 1000.0
    assert pcm16_duration_ms(b"", 16000) == 0.0


# ── safety.py 纯函数（复用 detect_crisis 单一事实源）────────────────────────
def test_assess_transcript_severe():
    v = assess_call_transcript("我真的不想活了")
    assert v.action == SafetyAction.ESCALATE
    assert v.level == "severe"
    assert v.notify_human is True
    assert v.directive


def test_assess_transcript_normal():
    v = assess_call_transcript("今天天气不错，我们聊聊天")
    assert v.action == SafetyAction.CONTINUE
    assert v.level == "none"
    assert v.notify_human is False


def test_assess_transcript_empty_or_disabled():
    assert assess_call_transcript("").action == SafetyAction.CONTINUE
    assert assess_call_transcript("我不想活了", enabled=False).action == SafetyAction.CONTINUE
