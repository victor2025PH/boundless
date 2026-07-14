"""通话桥 —— 把「传输层(来电/音频帧) ↔ 实时语音大脑」编排起来（薄 IO + 复用纯函数核心）。

分层（可 mock 契约测试的关键）：
  - ``CallTransport`` / ``CallBrain`` / ``BrainSession`` 是**协议**（鸭子类型），bridge 只依赖它们；
    真实实现（pytgcalls 传输 / realtime_voice 大脑）在别处，测试注入 fake 即可全链验证。
  - 决策 / 状态机 / 帧数学 / 拟人调度 / 安全判级全走 ``core`` + ``safety`` 纯函数（已各自单测）。

职责：
  1. ``handle_incoming``：来电 → ``decide_incoming_call`` → 接听 / 拒接+补偿 / 静默拒接；
  2. 出向音频管线：大脑吐 PCM(16k/24k) → 重采样 48k → 切 10ms 帧 → ``transport.send_frame``；
  3. 进向音频管线：传输层帧(48k) → 折叠单声道 + 重采样 16k → ``brain.push_audio``；
  4. 安全并行监测：``transcript.user`` → ``assess_call_transcript`` → 注入指令 / 拉人（S2S 无
     「出口前拦截」的补偿）；
  5. 收尾：转写累积交回调（落库/记忆/挂断后 follow-up，具体落库在 bridge 外做）。

一切失败安全退化，绝不把异常抛进实时音频热路。默认整体关（``CallsConfig.enabled``）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple

from src.voicecall.audio import downmix_to_mono, resample_pcm16
from src.voicecall.core import (
    CallAction,
    CallDecision,
    CallEvent,
    CallState,
    CallsConfig,
    decide_incoming_call,
    evaluate_call_budget,
    frame_bytes,
    is_quiet_hour,
    split_pcm_frames,
    transition,
)
from src.voicecall.humanize import Humanizer
from src.voicecall.safety import SafetyAction, assess_call_transcript

logger = logging.getLogger(__name__)

# 拟人音频供给：async ``() -> (pcm_bytes, sample_rate)``；返回 None/空=本次不出声。
PcmProvider = Callable[[], Awaitable[Optional[Tuple[bytes, int]]]]


# ── 协议（真实现在别处；测试注入 fake）──────────────────────────────────────────
class BrainSession(Protocol):
    """一次通话的大脑会话（S2S=MiniCPM-o WS / cascade=ASR+LLM+TTS 编排）。"""
    async def push_audio(self, pcm16_mono_16k: bytes) -> None: ...
    async def inject_directive(self, text: str) -> None: ...
    def events(self) -> AsyncIterator[Dict[str, Any]]: ...
    async def close(self) -> None: ...


class CallBrain(Protocol):
    async def open(self, ctx: "CallContext") -> BrainSession: ...


class CallTransport(Protocol):
    """传输层（pytgcalls / tg2sip SIP UA）。帧为 48kHz PCM16 mono。"""
    async def answer(self, chat_id: int) -> None: ...
    async def decline(self, chat_id: int) -> None: ...
    async def send_frame(self, chat_id: int, pcm_frame_48k: bytes) -> None: ...
    async def hangup(self, chat_id: int) -> None: ...


@dataclass
class CallContext:
    """一通来电的解析上下文（由上层从 inbox/contacts/registry 组装后传入）。"""
    chat_id: int
    account_id: str = ""
    persona_id: str = ""
    conversation_language: str = "zh"
    intimacy: float = 0.0
    automation_mode: str = "auto_ai"
    has_conversation: bool = False
    peer_known: bool = True
    kill_switch_frozen: bool = False
    hour: int = 12                       # 当前小时（安静时段判定，由上层按时区算好）
    host_warm: bool = True
    concurrent_active: int = 0
    memory_bullets: str = ""
    # 账号级通话预算/健康信号（由上层从 call_stats/account_health 组装；防自动接听催封号）
    calls_today: int = 0
    minutes_today: float = 0.0
    account_light: str = "green"         # green|amber|red（send_gate/account_health 判定）


@dataclass
class CallHooks:
    """通话生命周期回调（落库/记忆/告警等副作用挂这里，保持 bridge 主体纯净）。"""
    compensate: Optional[Callable[[CallContext, str], Awaitable[None]]] = None   # 拒接补偿消息
    on_human_escalation: Optional[Callable[[CallContext, str], Awaitable[None]]] = None  # 危机拉人
    on_wrapup: Optional[Callable[[CallContext, "CallResult"], Awaitable[None]]] = None    # 收尾


@dataclass
class CallResult:
    """一通电话的结账（供 wrapup 落库/记忆/观测）。"""
    chat_id: int
    accepted: bool = False
    end_reason: str = ""
    user_transcript: List[str] = field(default_factory=list)
    assistant_transcript: List[str] = field(default_factory=list)
    out_frames: int = 0
    max_safety_level: str = "none"
    human_escalated: bool = False
    filler_count: int = 0
    backchannel_count: int = 0
    duration_sec: float = 0.0


class TelegramCallBridge:
    """编排一通电话：决策 → 建会话 → 双向中继 + 安全监测 → 收尾。"""

    def __init__(self, cfg: CallsConfig, transport: CallTransport, brain: CallBrain,
                 *, hooks: Optional[CallHooks] = None, stats: Any = None,
                 opener_provider: Optional[PcmProvider] = None,
                 filler_provider: Optional[PcmProvider] = None,
                 backchannel_provider: Optional[PcmProvider] = None,
                 tick_interval_sec: float = 0.2,
                 now_fn: Optional[Callable[[], float]] = None) -> None:
        self.cfg = cfg
        self.transport = transport
        self.brain = brain
        self.hooks = hooks or CallHooks()
        self._out_frame_bytes = frame_bytes(cfg.sample_rate, cfg.frame_ms, cfg.channels)
        # 活跃会话登记表：进向音频回调（传输层 on_update(stream_frame INCOMING)）按 chat_id
        # 路由到对应大脑会话。pytgcalls 进向是**独立注册回调**（非拉取循环）→ 必须有此映射。
        self._sessions: Dict[int, BrainSession] = {}
        # 活跃 Humanizer 登记表：传输层 VAD「对方开口」信号（on_user_speech_start）按 chat_id
        # 路由到对应编排器驱动 backchannel 计时——否则倾听反馈无入口、永不触发。
        self._humanizers: Dict[int, Humanizer] = {}
        # 拟人音频供给（预渲染克隆声 PCM）；缺省 None=对应能力静默关（无预渲染资产时正确降级）。
        # opener＝接通即播的克隆声开场白（"喂？""在呢~"），消接通后尴尬沉默——用**预渲染**音频
        # （零 GPU 零延迟）解 realtime_voice.opener 因 20s 一次性合成而关掉的老问题。
        self._opener_provider = opener_provider
        self._filler_provider = filler_provider
        self._backchannel_provider = backchannel_provider
        self._tick_interval = max(0.05, float(tick_interval_sec))
        self._now = now_fn or time.monotonic
        # 观测（进程级单例，可注入 fake 测试）。
        if stats is None:
            try:
                from src.voicecall.call_stats import get_call_stats
                stats = get_call_stats()
            except Exception:
                stats = None
        self.stats = stats

    def active_calls(self) -> int:
        """当前活跃通话数（供 max_concurrent 闸门 / 观测）。"""
        return len(self._sessions)

    def _stat(self, method: str, *args: Any, **kw: Any) -> None:
        """best-effort 观测：stats 缺失/异常一律吞掉，绝不阻塞通话。"""
        if self.stats is None:
            return
        try:
            getattr(self.stats, method)(*args, **kw)
        except Exception:
            logger.debug("[voicecall] stats.%s 失败", method, exc_info=True)

    # ── 来电决策 ───────────────────────────────────────────────────────────
    def decide(self, ctx: CallContext) -> CallDecision:
        """纯决策（不产生副作用）：把 ctx 喂给核心护栏。安静时段按 ctx.hour 现算。

        并发数以**桥自身登记表为权威**（取 ctx 传入值与实时 active_calls 的较大者）——
        调用方可能不知道/算错在途通话，桥才是单一事实源，据此堵住「max_concurrent=1 却
        因 caller 传 0 而放进第二通」的竞态。
        """
        quiet = is_quiet_hour(ctx.hour, self.cfg)
        concurrent = max(int(ctx.concurrent_active or 0), self.active_calls())
        decision = decide_incoming_call(
            self.cfg,
            chat_type="private",
            has_conversation=ctx.has_conversation,
            peer_known=ctx.peer_known,
            automation_mode=ctx.automation_mode,
            intimacy=ctx.intimacy,
            conversation_language=ctx.conversation_language,
            host_warm=ctx.host_warm,
            concurrent_active=concurrent,
            kill_switch_frozen=ctx.kill_switch_frozen,
            quiet_hours=quiet,
        )
        # 通过基础护栏后，再过账号级通话预算/健康闸（防自动接听把号打进风控红线）。
        # 熟人只是「今天聊太多/号要歇歇」→ 拒接+补偿（绝不空响），非静默。
        if decision.action == CallAction.ACCEPT:
            budget = evaluate_call_budget(
                self.cfg, calls_today=ctx.calls_today,
                minutes_today=ctx.minutes_today, account_light=ctx.account_light)
            if not budget.allowed:
                return CallDecision(CallAction.DECLINE_COMPENSATE, budget.reason, True)
        return decision

    async def handle_incoming(self, ctx: CallContext) -> CallDecision:
        """来电入口：决策 → 接听/拒接+补偿/静默拒接。返回决策供上层观测。

        - ACCEPT → transport.answer + 起中继（``run_session``，调用方通常 fire-and-forget）；
        - DECLINE_COMPENSATE → transport.decline + 触发补偿消息 hook（绝不冷场）；
        - DECLINE_SILENT → transport.decline，无补偿。
        """
        self._stat("incoming")
        decision = self.decide(ctx)
        if decision.action == CallAction.ACCEPT:
            self._stat("decided", "accept", decision.reason)
            return decision
        # 两类拒接都要先挂断来电
        try:
            await self.transport.decline(ctx.chat_id)
        except Exception:
            logger.debug("[voicecall] decline 失败 chat=%s", ctx.chat_id, exc_info=True)
        compensated = False
        if decision.action == CallAction.DECLINE_COMPENSATE and self.hooks.compensate:
            try:
                await self.hooks.compensate(ctx, decision.reason)
                compensated = True
            except Exception:
                logger.debug("[voicecall] 补偿消息失败", exc_info=True)
        self._stat("decided", decision.action.value, decision.reason,
                   compensated=compensated)
        return decision

    # ── 出向音频管线（大脑 PCM → 48k → 10ms 帧 → 传输层）──────────────────────
    async def emit_pcm(self, chat_id: int, pcm: bytes, src_rate: int,
                       result: Optional["CallResult"] = None) -> int:
        """把大脑吐出的一段 PCM(src_rate, mono) 重采样到 48k、切 10ms 帧逐帧下发。

        返回发出的帧数。任何异常安全退化（记 debug，不抛进音频热路）。
        """
        try:
            pcm48 = resample_pcm16(pcm, src_rate, self.cfg.sample_rate)
            frames = split_pcm_frames(pcm48, self._out_frame_bytes)
            sent = 0
            for fr in frames:
                await self.transport.send_frame(chat_id, fr)
                sent += 1
            if result is not None:
                result.out_frames += sent
            return sent
        except Exception:
            logger.debug("[voicecall] emit_pcm 失败 chat=%s", chat_id, exc_info=True)
            return 0

    # ── 进向音频管线（传输层帧 48k → 单声道 16k → 大脑）───────────────────────
    async def ingest_frame(self, session: BrainSession, pcm_frame_48k: bytes,
                           *, channels: int = 1) -> None:
        """把传输层进向帧折叠单声道 + 重采样到大脑上行采样率(16k)喂进大脑。"""
        try:
            mono = downmix_to_mono(pcm_frame_48k, channels)
            pcm16 = resample_pcm16(mono, self.cfg.sample_rate, 16000)
            await session.push_audio(pcm16)
        except Exception:
            logger.debug("[voicecall] ingest_frame 失败", exc_info=True)

    async def on_inbound_frame(self, chat_id: int, pcm_frame_48k: bytes,
                               *, channels: int = 1) -> None:
        """传输层进向音频回调入口（pytgcalls stream_frame(INCOMING) → 此处按 chat_id 路由）。

        pytgcalls 进向是**注册式回调**（非拉取循环）：回调只带 chat_id，故靠会话登记表
        找到对应大脑会话再喂音频。找不到（通话已结束/未接）→ 静默丢弃，绝不抛。
        """
        session = self._sessions.get(int(chat_id))
        if session is None:
            return
        await self.ingest_frame(session, pcm_frame_48k, channels=channels)

    def on_user_speech_start(self, chat_id: int, now: Optional[float] = None) -> None:
        """传输层 VAD 检测到对方开口（长段倾诉起点）→ 驱动该通话的 backchannel 计时。

        pytgcalls 无内建 VAD，此信号由传输层适配器基于进向帧能量/静音判定后调用（P1-next），
        或 cascade 侧的 smart-turn 提供。找不到活跃编排器（未接/已结束）→ 静默忽略。
        """
        hum = self._humanizers.get(int(chat_id))
        if hum is not None:
            hum.on_user_speech_start(self._now() if now is None else float(now))

    # ── 大脑事件处理（含安全并行监测）────────────────────────────────────────
    async def _handle_brain_event(self, ctx: CallContext, session: BrainSession,
                                  ev: Dict[str, Any], result: CallResult) -> None:
        et = str(ev.get("type") or "")
        if et == "output_audio":
            b64 = ev.get("audio_b64") or ev.get("audio") or ""
            if b64:
                import base64
                try:
                    pcm = base64.b64decode(b64)
                except Exception:
                    pcm = b""
                sr = int(ev.get("sample_rate") or 24000)
                await self.emit_pcm(ctx.chat_id, pcm, sr, result)
        elif et == "transcript.user":
            text = str(ev.get("text") or "")
            if text.strip():
                result.user_transcript.append(text)
                await self._safety_check(ctx, session, text, result)
        elif et == "transcript.assistant":
            text = str(ev.get("text") or "")
            if ev.get("final") and text.strip():
                result.assistant_transcript.append(text)

    async def _safety_check(self, ctx: CallContext, session: BrainSession,
                            user_text: str, result: CallResult) -> None:
        """转写并行危机监测：severe→注入安全指令+拉人；elevated→温柔指令。"""
        verdict = assess_call_transcript(user_text, enabled=True)
        _rank = {"none": 0, "elevated": 1, "severe": 2}
        if _rank.get(verdict.level, 0) > _rank.get(result.max_safety_level, 0):
            result.max_safety_level = verdict.level
        if verdict.action == SafetyAction.CONTINUE:
            return
        if verdict.directive:
            try:
                await session.inject_directive(verdict.directive)
            except Exception:
                logger.debug("[voicecall] 安全指令注入失败", exc_info=True)
        if verdict.action == SafetyAction.SOFTEN:
            self._stat("safety_escalation", "elevated")
        if verdict.notify_human and not result.human_escalated:
            result.human_escalated = True
            self._stat("safety_escalation", "severe")
            if self.hooks.on_human_escalation:
                try:
                    await self.hooks.on_human_escalation(ctx, verdict.level)
                except Exception:
                    logger.debug("[voicecall] 人工升级回调失败", exc_info=True)

    # ── 一通电话的完整会话 ───────────────────────────────────────────────────
    async def run_session(self, ctx: CallContext) -> CallResult:
        """接听后的完整会话（假定已决策 ACCEPT）。返回 CallResult 供收尾。"""
        result = CallResult(chat_id=ctx.chat_id, accepted=True)
        state = CallState.RINGING
        state = transition(state, CallEvent.ACCEPT) or state   # → CONNECTING
        try:
            await self.transport.answer(ctx.chat_id)
        except Exception:
            logger.warning("[voicecall] answer 失败 chat=%s", ctx.chat_id)
            result.end_reason = "answer_failed"
            return await self._finish(ctx, result, state)
        try:
            session = await self.brain.open(ctx)
        except Exception:
            logger.warning("[voicecall] 大脑会话建立失败 chat=%s", ctx.chat_id)
            result.end_reason = "brain_failed"
            await self._safe_hangup(ctx.chat_id)
            return await self._finish(ctx, result, state)

        state = transition(state, CallEvent.CONNECTED) or state  # → LIVE
        self._sessions[int(ctx.chat_id)] = session   # 登记：进向音频回调据此路由
        self._stat("connected")
        t_connected = self._now()
        # 接通即播克隆声开场白（预渲染，零延迟）——消除接通瞬间的尴尬沉默。失败静默跳过。
        if self._opener_provider is not None:
            try:
                got = await self._opener_provider()
                if got:
                    await self.emit_pcm(ctx.chat_id, got[0], int(got[1]), result)
            except Exception:
                logger.debug("[voicecall] opener 出声失败", exc_info=True)
        humanizer = self._build_humanizer(ctx.chat_id, result)
        self._humanizers[int(ctx.chat_id)] = humanizer   # 登记：VAD「对方开口」信号据此路由
        hum_task = asyncio.ensure_future(self._humanizer_loop(humanizer))
        try:
            async for ev in session.events():
                humanizer.on_event(ev, self._now())      # 拟人计时随事件推进
                await self._handle_brain_event(ctx, session, ev, result)
                if str(ev.get("type") or "") == "session.end":
                    break
            result.end_reason = result.end_reason or "normal"
        except Exception:
            result.end_reason = "relay_error"
            logger.debug("[voicecall] 中继异常 chat=%s", ctx.chat_id, exc_info=True)
        finally:
            hum_task.cancel()
            try:
                await hum_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sessions.pop(int(ctx.chat_id), None)   # 注销：结束后进向帧静默丢弃
            self._humanizers.pop(int(ctx.chat_id), None)
            result.filler_count = humanizer.filler_count
            result.backchannel_count = humanizer.backchannel_count
            result.duration_sec = max(0.0, self._now() - t_connected)
            self._stat("humanize", filler=humanizer.filler_count,
                       backchannel=humanizer.backchannel_count)
            self._stat("ended", result.end_reason, was_connected=True,
                       duration_sec=result.duration_sec)
            try:
                await session.close()
            except Exception:
                pass
            await self._safe_hangup(ctx.chat_id)
        return await self._finish(ctx, result, state)

    # ── 拟人并行调度（思考填充 + 倾听反馈）────────────────────────────────────
    def _build_humanizer(self, chat_id: int, result: CallResult) -> Humanizer:
        """建 Humanizer，把「命中→取预渲染 PCM→出向下发」的 emit 闭包接上传输层。

        无对应 provider 时 emit=None → 该能力静默关（无预渲染资产时正确降级，不出静音噪声）。
        """
        emit_filler = self._make_emit(chat_id, self._filler_provider, result) \
            if self._filler_provider else None
        emit_bc = self._make_emit(chat_id, self._backchannel_provider, result) \
            if self._backchannel_provider else None
        return Humanizer(self.cfg, emit_filler=emit_filler, emit_backchannel=emit_bc)

    def _make_emit(self, chat_id: int, provider: PcmProvider,
                   result: CallResult) -> Callable[[], Awaitable[bool]]:
        async def _emit() -> bool:
            got = await provider()
            if not got:
                return False
            pcm, sr = got
            sent = await self.emit_pcm(chat_id, pcm, int(sr), result)
            return sent > 0
        return _emit

    async def _humanizer_loop(self, humanizer: Humanizer) -> None:
        """周期 tick：命中则出声。随会话结束被 cancel。异常不外泄（不拖垮通话）。"""
        try:
            while True:
                await asyncio.sleep(self._tick_interval)
                await humanizer.tick(self._now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[voicecall] humanizer loop 异常", exc_info=True)

    async def _finish(self, ctx: CallContext, result: CallResult,
                      state: CallState) -> CallResult:
        if self.hooks.on_wrapup:
            try:
                await self.hooks.on_wrapup(ctx, result)
            except Exception:
                logger.debug("[voicecall] wrapup 回调失败", exc_info=True)
        return result

    async def _safe_hangup(self, chat_id: int) -> None:
        try:
            await self.transport.hangup(chat_id)
        except Exception:
            logger.debug("[voicecall] hangup 失败 chat=%s", chat_id, exc_info=True)


__all__ = [
    "BrainSession", "CallBrain", "CallTransport",
    "CallContext", "CallHooks", "CallResult", "TelegramCallBridge",
]
