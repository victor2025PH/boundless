"""通话桥的真实 IO 适配器 —— 把抽象协议接到具体实现。

  - ``RealtimeS2SBrain``   —— 把既有 ``realtime_voice``（MiniCPM-o 全双工 @5090）适配成
    bridge 的 ``CallBrain``/``BrainSession``（P1 现实默认大脑，~1.3s、零新增显存）。
    复用 ``realtime_voice`` 的纯 builder（``build_call_system_prompt``/``build_session_init``）
    与 ``RealtimeVoiceClient``（健康探测/连接），事件名两侧本就对齐（output_audio /
    transcript.user / transcript.assistant）——bridge 无需翻译。
  - ``NtgcallsTransport``  —— 把 py-tgcalls 3.0 适配成 bridge 的 ``CallTransport``
    （send_frame 出向、来电/进向帧回调由外部 wiring 注入）。**惰性导入**，未装不影响其余。

可测部分（不需 network/GPU）抽成纯函数：
  - ``build_call_init``     —— CallContext → session.init 负载（注入测试可断言）
  - ``RealtimeBrainSession.events`` 的**连接关闭→session.end** 归一（FakeWS 可测）
真实连接/发帧是薄 IO，靠 mock host / mock binding 做契约测试或真机联调。
"""
from __future__ import annotations

import base64
import logging
from typing import Any, AsyncIterator, Callable, Dict, Optional

from src.ai.realtime_voice import (
    EV_ERROR,
    RealtimeVoiceConfig,
    build_call_system_prompt,
    build_session_init,
    dumps_event,
    input_audio_event,
    parse_host_event,
    pick_language,
)
from src.voicecall.bridge import CallContext
from src.voicecall.core import CallsConfig

logger = logging.getLogger(__name__)

# 主机契约扩展：mid-call 安全指令注入（host 应把 directive 并进后续轮次系统提示）。
EV_SESSION_UPDATE = "session.update"


def build_call_init(ctx: CallContext, rvc: RealtimeVoiceConfig,
                    *, voice_ref_b64: str = "") -> Dict[str, Any]:
    """CallContext → ``session.init`` 负载（纯函数）。

    - 语言：跟会话客户语言（``pick_language`` 归一到 zh/en，通话大脑仅稳定支持这两种）；
    - 系统提示：人设画像（此处最小化，仅记忆 + 语言 + 共情守则；生产可注入完整人设 base_prompt）
      + 记忆 bullets（来自 ctx.memory_bullets，与草稿/浏览器通话同口径）；
    - 音色：有参考音 b64 → 克隆该音色，否则用主机内置 voice。
    """
    language = pick_language(None, default=(ctx.conversation_language or rvc.default_language))
    bullets = [b for b in str(ctx.memory_bullets or "").splitlines() if b.strip()]
    system_prompt = build_call_system_prompt(
        persona=None, memory_bullets=bullets, language=language,
        extra_guidance=rvc.guidance)
    voice = "" if voice_ref_b64 else (rvc.default_voice or "")
    return build_session_init(
        system_prompt=system_prompt, language=language,
        voice_ref_b64=voice_ref_b64 or None, voice=voice or None,
        sample_rate=rvc.sample_rate, model=rvc.model)


class RealtimeBrainSession:
    """把 ``realtime_voice.RealtimeSession`` 适配成 bridge 的 ``BrainSession``。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def push_audio(self, pcm16_mono_16k: bytes) -> None:
        b64 = base64.b64encode(pcm16_mono_16k).decode("ascii")
        await self._session.send_event(input_audio_event(b64))

    async def inject_directive(self, text: str) -> None:
        """mid-call 安全指令注入（host 契约扩展 session.update）。host 不认则忽略、无害。"""
        if not str(text or "").strip():
            return
        await self._session.send_event({"type": EV_SESSION_UPDATE, "directive": str(text)})

    async def events(self) -> AsyncIterator[Dict[str, Any]]:
        """逐条产出主机事件；连接关闭/异常 → 归一为 ``session.end`` 后收尾（bridge 据此结束）。"""
        while True:
            try:
                ev = await self._session.recv()
            except Exception:
                yield {"type": "session.end", "reason": "closed"}
                return
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == EV_ERROR:
                yield {"type": "session.end", "reason": str(ev.get("error") or "error")}
                return
            yield ev

    async def close(self) -> None:
        try:
            await self._session.close()
        except Exception:
            pass


class RealtimeS2SBrain:
    """把 ``realtime_voice`` 适配成 bridge 的 ``CallBrain``（S2S 大脑，MiniCPM-o 全双工）。"""

    def __init__(self, full_config: Optional[Dict[str, Any]] = None, *,
                 client: Any = None,
                 init_builder: Optional[Callable[[CallContext, RealtimeVoiceConfig], Dict[str, Any]]] = None,
                 voice_ref_resolver: Optional[Callable[[CallContext], str]] = None) -> None:
        self.rvc = RealtimeVoiceConfig.from_config(full_config)
        self._client = client                    # 注入便于测试；缺省惰性建 RealtimeVoiceClient
        self._init_builder = init_builder or (lambda c, r: build_call_init(
            c, r, voice_ref_b64=(voice_ref_resolver(c) if voice_ref_resolver else "")))

    def _get_client(self) -> Any:
        if self._client is None:
            from src.ai.realtime_voice_client import RealtimeVoiceClient
            self._client = RealtimeVoiceClient(self.rvc)
        return self._client

    async def open(self, ctx: CallContext) -> RealtimeBrainSession:
        client = self._get_client()
        session = await client.connect()
        init_payload = self._init_builder(ctx, self.rvc)
        await session.send_session_init(init_payload)
        return RealtimeBrainSession(session)


class NtgcallsTransport:
    """py-tgcalls 3.0 传输适配（``CallTransport``）。**惰性导入**，未装/未启不影响其余子系统。

    出向：``send_frame(chat_id, Device.MICROPHONE, pcm48)``（10ms 帧，由 bridge 切好）。
    进向/来电：由外部 wiring 用 ``@calls.on_update(filters.stream_frame(INCOMING))`` /
    ``filters.chat_update(INCOMING_CALL)`` 回调驱动 bridge（见 tools/tg_call_poc.py 的样式），
    本类只封装「接听/拒接/发帧/挂断」四个出站动作，保持职责单一。
    """

    def __init__(self, calls: Any) -> None:
        self._calls = calls        # 已 start 的 PyTgCalls 实例

    async def answer(self, chat_id: int) -> None:
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream
        await self._calls.play(chat_id, MediaStream(ExternalMedia.AUDIO),
                               config=CallConfig(timeout=30))
        await self._calls.record(chat_id, MediaStream(ExternalMedia.AUDIO))

    async def decline(self, chat_id: int) -> None:
        try:
            await self._calls.leave_call(chat_id)
        except Exception:
            logger.debug("[voicecall] decline/leave 失败 chat=%s", chat_id, exc_info=True)

    async def send_frame(self, chat_id: int, pcm_frame_48k: bytes) -> None:
        from pytgcalls.types import Device
        await self._calls.send_frame(chat_id, Device.MICROPHONE, pcm_frame_48k)

    async def hangup(self, chat_id: int) -> None:
        try:
            await self._calls.leave_call(chat_id)
        except Exception:
            logger.debug("[voicecall] hangup 失败 chat=%s", chat_id, exc_info=True)


__all__ = [
    "build_call_init",
    "RealtimeBrainSession",
    "RealtimeS2SBrain",
    "NtgcallsTransport",
    "EV_SESSION_UPDATE",
]
