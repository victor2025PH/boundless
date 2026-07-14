"""Telegram 原生语音通话 —— 纯函数核心（无 IO / 无框架依赖，可离线单测）。

为什么单独成模块：原生来电链路 = pytgcalls 传输(WebRTC) + 实时语音大脑(MiniCPM-o /
半级联)。与 ``realtime_voice``（浏览器 WS 通话）共用大脑，但多出三块**电话场景专属**逻辑，
全部收敛在这里做成纯函数（可离线单测、零框架耦合）：

  - ``CallsConfig``          —— ``config.yaml::telegram_calls`` 段的强类型视图（默认关）
  - ``decide_incoming_call`` —— 接听决策护栏（接 / 拒接+补偿 / 静默拒接）
  - ``transition``           —— 通话状态机 IDLE→RINGING→CONNECTING→LIVE→WRAPPING→ENDED
  - 帧数学                   —— 10ms 帧节奏（ntgcalls sink 实测 10ms/帧，发 20ms 会
                                jitter underrun）、PCM 分帧、``FramePacer`` 按墙钟补发不漂移
  - 拟人化调度               —— ``ring_delay_sec`` 接听前响几声、``ThinkingFiller`` 思考填充音

设计原则（与 ``realtime_voice`` / ``voice_emotion`` 同源）：
  - **纯函数、无副作用**：所有决策/数学都可注入时钟/随机源做确定性单测。
  - **防御式**：脏输入安全退化，绝不抛异常进实时通话主链（卡死通话比降级更糟）。
  - **安全优先**：接听决策把「反风控 / 反打扰」放在最前（静默拒接陌生人/群聊/冻结账号），
    「够格但此刻不便」才走「拒接+补偿消息」——**永远不无声空响**（ring-out 是陪护产品大忌）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

# MiniCPM-o / CosyVoice 实时侧稳定支持的语种；其余来电礼貌拒接（克隆声念不了外语）。
_SUPPORTED_LANGS = ("zh", "en")


# ── 通话状态机 ────────────────────────────────────────────────────────────────
class CallState(str, Enum):
    """单通电话的生命周期状态。"""
    IDLE = "idle"              # 无通话
    RINGING = "ringing"        # 收到来电、尚未接听（拟人：响几声再接）
    CONNECTING = "connecting"  # 已决定接听，正在建 WebRTC + 载大脑
    LIVE = "live"              # 通话中（双向音频流）
    WRAPPING = "wrapping"      # 正在收尾（说结束语 / 落库转写）
    ENDED = "ended"            # 已结束


class CallEvent(str, Enum):
    """驱动状态机的事件。"""
    RING = "ring"                # 来电到达
    ACCEPT = "accept"            # 决定接听
    DECLINE = "decline"          # 决定拒接
    CONNECTED = "connected"      # WebRTC + 大脑就绪
    WRAP = "wrap"                # 开始收尾（到时长/空闲/对方要挂）
    WRAP_DONE = "wrap_done"      # 收尾完成
    PEER_HANGUP = "peer_hangup"  # 对方挂断
    ERROR = "error"              # 任意致命错误


# 合法转移表：(当前态, 事件) -> 新态。表外一律非法（``transition`` 返回 None）。
_TRANSITIONS: Dict[Tuple[CallState, CallEvent], CallState] = {
    (CallState.IDLE, CallEvent.RING): CallState.RINGING,
    (CallState.RINGING, CallEvent.ACCEPT): CallState.CONNECTING,
    (CallState.RINGING, CallEvent.DECLINE): CallState.ENDED,
    (CallState.RINGING, CallEvent.PEER_HANGUP): CallState.ENDED,
    (CallState.RINGING, CallEvent.ERROR): CallState.ENDED,
    (CallState.CONNECTING, CallEvent.CONNECTED): CallState.LIVE,
    (CallState.CONNECTING, CallEvent.PEER_HANGUP): CallState.ENDED,
    (CallState.CONNECTING, CallEvent.ERROR): CallState.ENDED,
    (CallState.LIVE, CallEvent.WRAP): CallState.WRAPPING,
    (CallState.LIVE, CallEvent.PEER_HANGUP): CallState.ENDED,
    (CallState.LIVE, CallEvent.ERROR): CallState.ENDED,
    (CallState.WRAPPING, CallEvent.WRAP_DONE): CallState.ENDED,
    (CallState.WRAPPING, CallEvent.PEER_HANGUP): CallState.ENDED,
    (CallState.WRAPPING, CallEvent.ERROR): CallState.ENDED,
}


def transition(state: CallState, event: CallEvent) -> Optional[CallState]:
    """状态机单步：合法则返回新态，非法返回 ``None``（调用方据此忽略/告警，绝不崩）。"""
    return _TRANSITIONS.get((state, event))


def is_terminal(state: CallState) -> bool:
    """是否终态（ENDED）。"""
    return state == CallState.ENDED


# ── 接听决策 ─────────────────────────────────────────────────────────────────
class CallAction(str, Enum):
    """来电处置动作。"""
    ACCEPT = "accept"                      # 接听
    DECLINE_COMPENSATE = "decline_compensate"  # 拒接 + 发补偿消息（够格但此刻不便）
    DECLINE_SILENT = "decline_silent"      # 拒接、不补偿（不够格/反风控/反打扰）


@dataclass(frozen=True)
class CallDecision:
    """接听决策结果。``compensate`` 为 True 时上层应发一条兜底消息/语音，绝不无声空响。"""
    action: CallAction
    reason: str
    compensate: bool = False


@dataclass(frozen=True)
class CallBudgetVerdict:
    """账号级通话预算/健康裁决（防「自动接电话把号打进风控红线」）。"""
    allowed: bool
    reason: str = "ok"


def evaluate_call_budget(
    cfg: "CallsConfig",
    *,
    calls_today: int = 0,
    minutes_today: float = 0.0,
    account_light: str = "green",
) -> CallBudgetVerdict:
    """单账号「今天还能不能自动接电话」裁决（纯函数）。

    与文本 send_gate 正交、更保守——**一通 AI 语音通话是极强的 userbot 特征**（比发条消息
    风险高一档），故给通话独立的日次数/日分钟预算 + 风控红灯硬停：
      - ``account_light == "red"``（send_gate/account_health 判红）+ ``block_on_red`` → 停
        （风控红线上再自动接=催封号）；
      - 日自动接听数 ≥ ``daily_calls_cap``（>0 时）→ 停（当天配额用尽）；
      - 日通话分钟 ≥ ``daily_minutes_cap``（>0 时）→ 停（当天时长用尽）。
    cap=0 表示不限该维度。allowed=False 的处置由上层映射为「拒接+补偿」（对方是熟人、只是
    今天聊太多/号要歇歇），绝不静默空响。
    """
    if cfg.budget_block_on_red and str(account_light or "").lower() == "red":
        return CallBudgetVerdict(False, "account_unhealthy")
    if cfg.daily_calls_cap > 0 and int(calls_today or 0) >= cfg.daily_calls_cap:
        return CallBudgetVerdict(False, "daily_calls_exhausted")
    if cfg.daily_minutes_cap > 0 and float(minutes_today or 0.0) >= cfg.daily_minutes_cap:
        return CallBudgetVerdict(False, "daily_minutes_exhausted")
    return CallBudgetVerdict(True, "ok")


def decide_incoming_call(
    cfg: "CallsConfig",
    *,
    chat_type: str = "private",
    has_conversation: bool = False,
    peer_known: bool = True,
    automation_mode: str = "auto_ai",
    intimacy: float = 0.0,
    conversation_language: str = "zh",
    host_warm: bool = True,
    concurrent_active: int = 0,
    kill_switch_frozen: bool = False,
    quiet_hours: bool = False,
) -> CallDecision:
    """来电接听决策（纯函数）。**判定顺序＝安全/反风控优先，其次反打扰，最后容量**。

    - **静默拒接**（``DECLINE_SILENT``，无补偿，反风控/反打扰）：子系统关、群聊/非私聊、
      账号被 kill-switch 冻结、非自动模式、陌生人（无会话或 peer 不可解析）。
      给陌生人/群聊自动发消息本身是风控特征，故静默。
    - **拒接+补偿**（``DECLINE_COMPENSATE``，够格的熟人但此刻不便）：语言不支持、亲密度不足、
      安静时段、主机冷、忙线。上层据 ``reason`` 发对味的兜底消息/语音（"在忙等下回你"）。
    - **接听**（``ACCEPT``）：全部通过。

    刻意不接受 ``now``/时钟——安静时段与主机热度由调用方预先算好传入，保持本函数纯粹可测。
    """
    if not cfg.enabled:
        return CallDecision(CallAction.DECLINE_SILENT, "disabled")
    if str(chat_type or "").lower() != "private":
        return CallDecision(CallAction.DECLINE_SILENT, "not_private")
    if kill_switch_frozen:
        return CallDecision(CallAction.DECLINE_SILENT, "kill_switch")
    if cfg.require_auto_ai and str(automation_mode or "").lower() != "auto_ai":
        return CallDecision(CallAction.DECLINE_SILENT, "not_auto_ai")
    if not has_conversation or not peer_known:
        return CallDecision(CallAction.DECLINE_SILENT, "stranger")
    # ↓ 以下都是「够格的熟人」，任何拒接都要补偿，绝不冷场
    lang = str(conversation_language or "").lower().split("-")[0]
    if lang not in cfg.languages:
        return CallDecision(CallAction.DECLINE_COMPENSATE, "language_unsupported", True)
    if float(intimacy or 0.0) < cfg.min_intimacy:
        return CallDecision(CallAction.DECLINE_COMPENSATE, "low_intimacy", True)
    if quiet_hours:
        return CallDecision(CallAction.DECLINE_COMPENSATE, "quiet_hours", True)
    if not host_warm:
        return CallDecision(CallAction.DECLINE_COMPENSATE, "host_cold", True)
    if int(concurrent_active or 0) >= cfg.max_concurrent:
        return CallDecision(CallAction.DECLINE_COMPENSATE, "busy", True)
    return CallDecision(CallAction.ACCEPT, "ok")


# ── 帧数学（10ms 节奏）───────────────────────────────────────────────────────
def frame_bytes(sample_rate: int, frame_ms: int, channels: int = 1,
                sample_width: int = 2) -> int:
    """一帧 PCM 的字节数。默认 48kHz/10ms/mono/PCM16 → 960 字节（480 采样）。

    ⚠ ntgcalls 的 ``AudioSink.frameTime()`` 实测 **10ms**（非常见误解的 20ms）；
    发 20ms 帧会让对端 jitter buffer underrun → 卡顿/爆音。故 frame_ms 默认 10。
    """
    sr = max(1, int(sample_rate))
    fm = max(1, int(frame_ms))
    ch = max(1, int(channels))
    sw = max(1, int(sample_width))
    return (sr * fm // 1000) * ch * sw


def split_pcm_frames(pcm: bytes, frame_size: int, *, pad_last: bool = True) -> List[bytes]:
    """把整段 PCM 切成定长帧。尾帧不足一帧时用静音(0x00)补齐（``pad_last``），
    保证每帧长度一致——ntgcalls 送短帧同样会 underrun。空输入返回 []。"""
    if not pcm or frame_size <= 0:
        return []
    frames: List[bytes] = []
    n = len(pcm)
    off = 0
    while off < n:
        chunk = pcm[off:off + frame_size]
        if len(chunk) < frame_size:
            if pad_last:
                chunk = chunk + b"\x00" * (frame_size - len(chunk))
            else:
                break
        frames.append(chunk)
        off += frame_size
    return frames


def silence_frame(frame_size: int) -> bytes:
    """一帧静音（用于填充音间隙 / 保活）。"""
    return b"\x00" * max(0, int(frame_size))


class FramePacer:
    """按墙钟节奏发帧的调度器：``due(i)`` 给出第 i 帧的应发时刻（相对起点），
    ``sleep_for(i, now)`` 给出还需等待的秒数（<0 说明已落后→立即发，绝不累积漂移）。

    纯计算 + 可注入时钟，便于单测「1000 帧后累计不漂」。10ms 帧下每帧 due = i*0.01s。
    """

    def __init__(self, frame_ms: int) -> None:
        self._dt = max(1, int(frame_ms)) / 1000.0
        self._t0: Optional[float] = None

    def start(self, now: float) -> None:
        self._t0 = float(now)

    def due(self, index: int) -> float:
        """第 index 帧相对起点的应发时刻（秒）。"""
        return (self._t0 or 0.0) + index * self._dt

    def sleep_for(self, index: int, now: float) -> float:
        """距第 index 帧应发还需等待的秒数；已落后返回 0.0（立即发，不追赶式狂发）。"""
        if self._t0 is None:
            self._t0 = float(now)
        return max(0.0, self.due(index) - float(now))


# ── 拟人化调度 ───────────────────────────────────────────────────────────────
def ring_delay_sec(cfg: "CallsConfig", rand: Optional[Callable[[], float]] = None) -> float:
    """接听前先响几声再接（秒接=机器人）。在 [min,max] 间取值，rand 可注入做确定性测试。"""
    lo = max(0.0, float(cfg.ring_delay_min_sec))
    hi = max(lo, float(cfg.ring_delay_max_sec))
    r = rand() if rand is not None else 0.5
    r = min(1.0, max(0.0, float(r)))
    return lo + (hi - lo) * r


def pick_supported_language(conversation_language: str, default: str = "zh") -> str:
    """把会话语言归一到通话支持的 zh/en；不支持则回落 default。"""
    lang = str(conversation_language or "").lower().split("-")[0]
    if lang in _SUPPORTED_LANGS:
        return lang
    return default if default in _SUPPORTED_LANGS else "zh"


class ThinkingFiller:
    """思考填充音调度：LLM 迟迟不出话时插「嗯…」「让我想想」等预渲染短句，
    把 1-1.5s 空窗变成「她在想」。避免刚接通就填、避免连珠炮式填。

    半级联大脑的首音延迟（ASR→LLM→TTS）是真人感最大杀手，这层把等待「表演化」。
    纯状态 + 可注入时钟；``should_fill(now, reply_started)`` 是唯一决策入口。
    """

    def __init__(self, cfg: "CallsConfig") -> None:
        self._after = max(0.0, float(cfg.filler_after_ms)) / 1000.0
        self._gap = max(0.0, float(cfg.filler_min_gap_ms)) / 1000.0
        self._turn_t0: Optional[float] = None   # 用户本轮说完的时刻
        self._last_fill: float = -1e9           # 上次填充时刻

    def on_user_turn_end(self, now: float) -> None:
        """用户说完一轮 → 开始计等待。"""
        self._turn_t0 = float(now)

    def on_reply_audio(self) -> None:
        """助手回复音频已开始 → 本轮不再需要填充。"""
        self._turn_t0 = None

    def should_fill(self, now: float, reply_started: bool = False) -> bool:
        """当前是否该插一段填充音：等待已超阈值 + 距上次填充够久 + 回复还没开始。"""
        if reply_started or self._turn_t0 is None:
            return False
        waited = float(now) - self._turn_t0
        if waited < self._after:
            return False
        if (float(now) - self._last_fill) < self._gap:
            return False
        self._last_fill = float(now)
        return True

    def reset(self) -> None:
        self._turn_t0 = None
        self._last_fill = -1e9


class BackchannelDecider:
    """倾听反馈（backchannel）调度：对方**持续说**一段时间时插一声「嗯」「对」「然后呢」，
    让对方感到「她在认真听」——这是电话真人感的最大单项加分（沉默倾听像挂了/走神）。

    与 ``ThinkingFiller`` 对称：Filler 管「我方该开口却还没」，Backchannel 管「对方在说、
    我方给最小回应」。**仅半级联(cascade)模式需要**——S2S 全双工模型原生会 backchannel，
    调用方据 ``cfg.brain`` 决定是否启用本器（本函数不判 brain，保持纯粹）。

    护栏：说够 ``after_sec`` 才首插、两次间隔 ≥ ``gap_sec``、本轮封顶 ``max_per_turn``
    （插太频=打断/敷衍，恐怖谷）。纯状态 + 可注入时钟。
    """

    def __init__(self, cfg: "CallsConfig") -> None:
        self._enabled = bool(cfg.backchannel)
        self._after = max(0.0, float(cfg.backchannel_after_sec))
        self._gap = max(0.0, float(cfg.backchannel_gap_sec))
        self._max = max(0, int(cfg.backchannel_max_per_turn))
        self._speak_t0: Optional[float] = None
        self._last: float = -1e9
        self._count = 0

    def on_user_speech_start(self, now: float) -> None:
        """对方开始说话（VAD 上升沿）→ 起算本轮倾听。"""
        self._speak_t0 = float(now)
        self._count = 0

    def on_user_turn_end(self) -> None:
        """对方说完（话轮结束）→ 本轮 backchannel 归零。"""
        self._speak_t0 = None
        self._count = 0

    def should_backchannel(self, now: float) -> bool:
        """当前是否该插一声倾听反馈。"""
        if not self._enabled or self._speak_t0 is None:
            return False
        if self._count >= self._max:
            return False
        if (float(now) - self._speak_t0) < self._after:
            return False
        if (float(now) - self._last) < self._gap:
            return False
        self._last = float(now)
        self._count += 1
        return True

    def reset(self) -> None:
        self._speak_t0 = None
        self._last = -1e9
        self._count = 0


# ── 流式 PCM 帧协议（对齐 7852 /v1/tts/stream：4字节小端长度前缀 + PCM16）──────────
def iter_length_prefixed_pcm(buffer: bytes) -> Tuple[List[bytes], bytes]:
    """从累积字节流中切出所有完整的「<I 长度前缀 + PCM」块，返回 (完整块列表, 剩余尾巴)。

    7852 流式 TTS 用此协议；桥接侧边收边解，把 TTS 块再切成 10ms 通话帧下发。
    0 长度块＝服务端异常结束标记，作为空块 b"" 返回（上层据此停止本轮）。
    """
    blocks: List[bytes] = []
    off = 0
    n = len(buffer)
    while n - off >= 4:
        (ln,) = struct.unpack_from("<I", buffer, off)
        if ln == 0:                       # 异常结束标记
            blocks.append(b"")
            off += 4
            continue
        if n - off - 4 < ln:              # 半个块，等更多数据
            break
        start = off + 4
        blocks.append(bytes(buffer[start:start + ln]))
        off = start + ln
    return blocks, bytes(buffer[off:])


# ── 配置视图 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CallsConfig:
    """``config.yaml::telegram_calls`` 段的强类型视图。默认 **关**（新子系统约定）。"""
    enabled: bool = False
    transport: str = "ntgcalls"     # ntgcalls | tg2sip
    # 2026-07-14 实测：本机 CosyVoice 克隆流式 TTFB 48-63s→cascade 当前硬件达不到实时；
    # 5090 MiniCPM-o S2S 本就流式 ~1.3s。故现实默认 s2s，cascade 待专用流式克隆嘴 GPU。
    brain: str = "s2s"              # s2s（MiniCPM-o 全双工，现实默认）| cascade（音色主权，需专用 GPU）
    # 传输层已验证闸（默认 False）：ntgcalls #44 进向音频/tg2sip 网关是运行时无法自证的最大未知，
    # 只有跑过 tools/tg_call_poc.py 三闸门确认能收发音频后，运营才手动置 true。就绪度体检据此判定：
    # 未验证 → blocker（哪怕主机在线也不算「就绪」，防误判绿灯后真机收不到来电）。
    transport_verified: bool = False
    # 接听护栏
    min_intimacy: float = 30.0
    languages: Tuple[str, ...] = _SUPPORTED_LANGS
    require_auto_ai: bool = True
    max_concurrent: int = 1
    ring_delay_min_sec: float = 2.0
    ring_delay_max_sec: float = 4.0
    # 帧
    sample_rate: int = 48000
    frame_ms: int = 10
    channels: int = 1
    # 拟人
    filler_after_ms: float = 700.0
    filler_min_gap_ms: float = 2500.0
    backchannel: bool = True
    backchannel_after_sec: float = 3.5
    backchannel_gap_sec: float = 4.0
    backchannel_max_per_turn: int = 3
    # 账号级通话预算/健康（防自动接听把号打进风控红线；与文本 send_gate 正交、更保守）
    daily_calls_cap: int = 20
    daily_minutes_cap: float = 60.0
    budget_block_on_red: bool = True
    # 会话
    max_session_sec: float = 1800.0
    idle_timeout_sec: float = 45.0
    # 安静时段
    quiet_enabled: bool = True
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8

    @classmethod
    def from_config(cls, full_config: Optional[Dict[str, Any]]) -> "CallsConfig":
        cfg: Dict[str, Any] = {}
        if isinstance(full_config, dict):
            tc = full_config.get("telegram_calls")
            if isinstance(tc, dict):
                cfg = tc
        answer = cfg.get("answer") if isinstance(cfg.get("answer"), dict) else {}
        frame = cfg.get("frame") if isinstance(cfg.get("frame"), dict) else {}
        human = cfg.get("humanize") if isinstance(cfg.get("humanize"), dict) else {}
        sess = cfg.get("session") if isinstance(cfg.get("session"), dict) else {}
        quiet = cfg.get("quiet_hours") if isinstance(cfg.get("quiet_hours"), dict) else {}
        budget = cfg.get("budget") if isinstance(cfg.get("budget"), dict) else {}

        def _f(d: Dict[str, Any], k: str, dv: float) -> float:
            try:
                return float(d.get(k))
            except (TypeError, ValueError):
                return dv

        def _i(d: Dict[str, Any], k: str, dv: int) -> int:
            try:
                return int(d.get(k))
            except (TypeError, ValueError):
                return dv

        def _s(d: Dict[str, Any], k: str, dv: str) -> str:
            v = d.get(k)
            return str(v).strip().lower() if v not in (None, "") else dv

        langs_raw = answer.get("languages")
        if isinstance(langs_raw, (list, tuple)) and langs_raw:
            langs = tuple(str(x).strip().lower().split("-")[0] for x in langs_raw
                          if str(x).strip())
        else:
            langs = _SUPPORTED_LANGS
        transport = _s(cfg, "transport", "ntgcalls")
        if transport not in ("ntgcalls", "tg2sip"):
            transport = "ntgcalls"
        brain = _s(cfg, "brain", "s2s")
        if brain not in ("cascade", "s2s"):
            brain = "s2s"
        transport_verified = bool(cfg.get("transport_verified", False))
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            transport=transport,
            brain=brain,
            transport_verified=transport_verified,
            min_intimacy=_f(answer, "min_intimacy", 30.0),
            languages=langs,
            require_auto_ai=bool(answer.get("require_auto_ai", True)),
            max_concurrent=max(1, _i(answer, "max_concurrent", 1)),
            ring_delay_min_sec=_f(answer, "ring_delay_min_sec", 2.0),
            ring_delay_max_sec=_f(answer, "ring_delay_max_sec", 4.0),
            sample_rate=_i(frame, "sample_rate", 48000),
            frame_ms=_i(frame, "frame_ms", 10),
            channels=_i(frame, "channels", 1),
            filler_after_ms=_f(human, "filler_after_ms", 700.0),
            filler_min_gap_ms=_f(human, "filler_min_gap_ms", 2500.0),
            backchannel=bool(human.get("backchannel", True)),
            backchannel_after_sec=_f(human, "backchannel_after_sec", 3.5),
            backchannel_gap_sec=_f(human, "backchannel_gap_sec", 4.0),
            backchannel_max_per_turn=_i(human, "backchannel_max_per_turn", 3),
            daily_calls_cap=max(0, _i(budget, "daily_calls_cap", 20)),
            daily_minutes_cap=max(0.0, _f(budget, "daily_minutes_cap", 60.0)),
            budget_block_on_red=bool(budget.get("block_on_red", True)),
            max_session_sec=_f(sess, "max_sec", 1800.0),
            idle_timeout_sec=_f(sess, "idle_timeout_sec", 45.0),
            quiet_enabled=bool(quiet.get("enabled", True)),
            quiet_start_hour=_i(quiet, "start_hour", 23),
            quiet_end_hour=_i(quiet, "end_hour", 8),
        )


def is_quiet_hour(hour: int, cfg: CallsConfig) -> bool:
    """给定当前小时(0-23)是否落在安静时段。支持跨午夜（如 23→8）。关则恒 False。"""
    if not cfg.quiet_enabled:
        return False
    h = int(hour) % 24
    start = int(cfg.quiet_start_hour) % 24
    end = int(cfg.quiet_end_hour) % 24
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end       # 跨午夜
