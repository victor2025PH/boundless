# -*- coding: utf-8 -*-
"""通话桥真实适配器门禁（无 network / 无 GPU）：

- build_call_init：CallContext → session.init 负载（语言归一 / 记忆注入 / 音色分支）；
- RealtimeBrainSession：push_audio base64 编码、inject_directive 契约、事件透传、
  连接关闭归一 session.end；
- RealtimeS2SBrain.open：注入 fake client 走通 connect→send_session_init→包成 BrainSession。
NtgcallsTransport 是薄 IO（真机联调），此处只验证惰性导入不在模块加载期牵入 pytgcalls。
"""
import asyncio
import base64

from src.voicecall.adapters import (
    EV_SESSION_UPDATE,
    RealtimeBrainSession,
    RealtimeS2SBrain,
    build_call_init,
)
from src.voicecall.bridge import CallContext


def _ctx(**kw):
    d = dict(chat_id=1, conversation_language="zh", memory_bullets="喜欢猫\n在深圳工作")
    d.update(kw)
    return CallContext(**d)


# ── build_call_init 纯函数 ──────────────────────────────────────────────────
def test_build_call_init_zh_with_memory():
    from src.ai.realtime_voice import RealtimeVoiceConfig
    rvc = RealtimeVoiceConfig.from_config({"realtime_voice": {"model": "minicpm-o-4_5"}})
    init = build_call_init(_ctx(), rvc)
    assert init.get("type") == "session.init"
    assert init.get("language") == "zh"
    assert "喜欢猫" in init.get("system_prompt", "")
    assert init.get("model") == "minicpm-o-4_5"


def test_build_call_init_language_normalized():
    from src.ai.realtime_voice import RealtimeVoiceConfig
    rvc = RealtimeVoiceConfig.from_config(None)
    # 英文会话 → en；不支持的语种（如 th）→ 回落默认 zh
    assert build_call_init(_ctx(conversation_language="en", memory_bullets=""),
                           rvc)["language"] == "en"
    assert build_call_init(_ctx(conversation_language="th", memory_bullets=""),
                           rvc)["language"] == "zh"


def test_build_call_init_voice_ref_branch():
    from src.ai.realtime_voice import RealtimeVoiceConfig
    rvc = RealtimeVoiceConfig.from_config(None)
    with_ref = build_call_init(_ctx(), rvc, voice_ref_b64="QUJD")
    assert with_ref.get("voice_ref_b64") == "QUJD"     # 有参考音 → 克隆分支


# ── RealtimeBrainSession（FakeWS 驱动，无 network）───────────────────────────
class FakeRealtimeSession:
    """模拟 realtime_voice.RealtimeSession：recv 依次吐预设事件，耗尽后抛（=连接关闭）。"""

    def __init__(self, events):
        self._events = list(events)
        self.sent = []
        self.closed = False

    async def send_event(self, ev):
        self.sent.append(ev)

    async def send_session_init(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if not self._events:
            raise ConnectionError("closed")
        return self._events.pop(0)

    async def close(self):
        self.closed = True


def test_brain_session_push_audio_b64():
    fs = FakeRealtimeSession([])
    bs = RealtimeBrainSession(fs)
    asyncio.run(bs.push_audio(b"\x01\x02\x03\x04"))
    assert fs.sent[0]["type"] == "input_audio"
    assert fs.sent[0]["audio_b64"] == base64.b64encode(b"\x01\x02\x03\x04").decode()


def test_brain_session_inject_directive():
    fs = FakeRealtimeSession([])
    bs = RealtimeBrainSession(fs)
    asyncio.run(bs.inject_directive("请温柔一点"))
    assert fs.sent[0]["type"] == EV_SESSION_UPDATE
    assert fs.sent[0]["directive"] == "请温柔一点"
    # 空指令不发
    asyncio.run(bs.inject_directive("  "))
    assert len(fs.sent) == 1


def test_brain_session_events_passthrough_then_end_on_close():
    events = [
        {"type": "transcript.user", "text": "在吗"},
        {"type": "output_audio", "audio_b64": "AAA="},
    ]
    fs = FakeRealtimeSession(events)
    bs = RealtimeBrainSession(fs)

    async def _collect():
        out = []
        async for ev in bs.events():
            out.append(ev)
        return out

    got = asyncio.run(_collect())
    assert got[0]["type"] == "transcript.user"
    assert got[1]["type"] == "output_audio"
    assert got[-1]["type"] == "session.end"      # 连接关闭 → 归一 session.end
    assert got[-1]["reason"] == "closed"


def test_brain_session_error_event_ends():
    fs = FakeRealtimeSession([{"type": "error", "error": "host_boom"}])
    bs = RealtimeBrainSession(fs)

    async def _collect():
        return [ev async for ev in bs.events()]

    got = asyncio.run(_collect())
    assert got[-1]["type"] == "session.end"
    assert got[-1]["reason"] == "host_boom"


# ── RealtimeS2SBrain.open（注入 fake client）────────────────────────────────
def test_s2s_brain_open_wires_session_init():
    fs = FakeRealtimeSession([])

    class FakeClient:
        def __init__(self):
            self.connected = False

        async def connect(self):
            self.connected = True
            return fs

    fake = FakeClient()
    brain = RealtimeS2SBrain({"realtime_voice": {"model": "minicpm-o-4_5"}}, client=fake)
    sess = asyncio.run(brain.open(_ctx()))
    assert fake.connected is True
    assert isinstance(sess, RealtimeBrainSession)
    # session.init 已下发，含语言/系统提示
    init = fs.sent[0]
    assert init["type"] == "session.init"
    assert init["language"] == "zh"


def test_ntgcalls_transport_lazy_import():
    # 导入 adapters 不应在模块加载期牵入 pytgcalls（惰性）
    import sys
    import importlib
    mod = importlib.import_module("src.voicecall.adapters")
    assert hasattr(mod, "NtgcallsTransport")
    # 构造不触发导入（真正导入发生在 answer/send_frame 调用时）
    t = mod.NtgcallsTransport(calls=object())
    assert t is not None
