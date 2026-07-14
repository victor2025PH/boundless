"""通话拟人编排 —— 思考填充音 + 倾听反馈的并行调度（事件驱动 + 时钟可注入，可离线测）。

电话真人感最大的两个杀手在**沉默的处理方式**上：
  1. 我方 LLM 还在想（半级联首音 1-1.5s / S2S 冷启）→ 死寂 = 「挂了吗？」→ 插「嗯…」「让我想想」；
  2. 对方长段倾诉、我方全程无声 = 「走神了/没在听」→ 插「嗯嗯」「然后呢」（backchannel）。

本模块把 ``core.ThinkingFiller`` / ``core.BackchannelDecider``（纯决策）包成一个**事件驱动 +
周期 tick** 的编排器 ``Humanizer``：
  - ``on_event(ev, now)``  —— 消费大脑事件（transcript.user / output_audio…）更新内部计时；
  - ``on_user_speech_start(now)`` —— 传输层 VAD 上升沿（对方开口）驱动 backchannel 计时；
  - ``tick(now)``         —— 周期检查两个决策，命中则调注入的 async ``emit_*`` 并返回命中动作。

真实音频注入（emit_filler/emit_backchannel）由 bridge 提供（喂预渲染克隆声 PCM）；本模块只管
**何时该出声**，不碰音频字节 → 决策可确定性单测（注入 now，不依赖真实 sleep）。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.voicecall.core import BackchannelDecider, CallsConfig, ThinkingFiller

logger = logging.getLogger(__name__)

# 大脑事件类型（与 bridge/realtime_voice 对齐）
_EV_USER = "transcript.user"
_EV_ASSISTANT = "transcript.assistant"
_EV_OUTPUT_AUDIO = "output_audio"

# 注入型 async 回调：返回 True=成功出声（供计数），False/异常=未出声（不阻塞）。
EmitFn = Callable[[], Awaitable[bool]]


class Humanizer:
    """思考填充 + 倾听反馈的并行编排器（一通电话一个实例）。

    ``emit_filler``/``emit_backchannel``：async，注入预渲染克隆声 PCM（bridge 提供）。
    缺省（None）时对应能力静默关闭（如 backchannel 仅 cascade 需要、S2S 原生自带→可不注入）。
    """

    def __init__(self, cfg: CallsConfig, *,
                 emit_filler: Optional[EmitFn] = None,
                 emit_backchannel: Optional[EmitFn] = None) -> None:
        self.cfg = cfg
        self._filler = ThinkingFiller(cfg)
        self._backchannel = BackchannelDecider(cfg)
        self._emit_filler = emit_filler
        self._emit_backchannel = emit_backchannel
        self._reply_active = False       # 助手本轮是否已在出声（出声期不填充/不插话）
        self.filler_count = 0
        self.backchannel_count = 0

    # ── 事件消费（更新计时状态）─────────────────────────────────────────────
    def on_event(self, ev: Dict[str, Any], now: float) -> None:
        et = str(ev.get("type") or "")
        if et == _EV_USER:
            # 用户说完一轮 → 开始等我方回复（填充计时起点）；同时结束倾听（backchannel 归零）
            self._filler.on_user_turn_end(now)
            self._backchannel.on_user_turn_end()
            self._reply_active = False
        elif et == _EV_OUTPUT_AUDIO:
            # 我方开始出声 → 本轮不再填充
            self._filler.on_reply_audio()
            self._reply_active = True
        elif et == _EV_ASSISTANT and ev.get("final"):
            self._reply_active = False

    def on_user_speech_start(self, now: float) -> None:
        """传输层 VAD 检测到对方开口（长段倾诉的起点）→ 驱动 backchannel 计时。"""
        self._backchannel.on_user_speech_start(now)

    # ── 周期 tick（命中则出声）───────────────────────────────────────────────
    async def tick(self, now: float) -> List[str]:
        """周期检查：命中填充/倾听则调 emit。返回本次命中的动作列表（观测用）。"""
        fired: List[str] = []
        # 思考填充：等待我方回复超阈值且尚未出声
        if self._emit_filler is not None and self._filler.should_fill(
                now, reply_started=self._reply_active):
            if await self._safe_emit(self._emit_filler):
                self.filler_count += 1
                fired.append("filler")
        # 倾听反馈：对方持续说且我方未在出声（出声期插话=打断，抑制）
        if (self._emit_backchannel is not None and not self._reply_active
                and self._backchannel.should_backchannel(now)):
            if await self._safe_emit(self._emit_backchannel):
                self.backchannel_count += 1
                fired.append("backchannel")
        return fired

    @staticmethod
    async def _safe_emit(fn: EmitFn) -> bool:
        try:
            return bool(await fn())
        except Exception:
            logger.debug("[voicecall] humanizer emit 失败", exc_info=True)
            return False

    def reset(self) -> None:
        self._filler.reset()
        self._backchannel.reset()
        self._reply_active = False


__all__ = ["Humanizer", "EmitFn"]
