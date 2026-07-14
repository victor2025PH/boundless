"""通话安全监测 —— 基于实时转写的危机拦截（纯函数，复用 wellbeing_guard 单一事实源）。

**为什么单独一层**：S2S（MiniCPM-o 全双工）直出音频，失去了文本链「出口前拦截」的能力
（`_apply_crisis_safety_net`）。补偿方案＝**转写并行监测**：主机每出一条用户转写
（``transcript.user``），本层跑同一个 ``detect_crisis`` 单一事实源判级，产出通话侧处置动作：

  - ``severe`` （自伤/轻生）→ ``ESCALATE``：注入最高优先安全指令 + 触发人工介入告警
    （语音场景无法整段覆盖已出的音频，但可在「下一轮」硬性接管口吻 + 立即拉人）；
  - ``elevated``（深度绝望）→ ``SOFTEN``：注入「格外温柔接住、放慢、先共情」指令；
  - ``none`` → ``CONTINUE``。

刻意**不**在这里发音频/改会话——只产出「决策 + 待注入指令 + 是否拉人」，由 bridge 执行
（保持纯函数可测）。severe 决策带 ``notify_human=True``，bridge 据此走既有人工升级通道。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SafetyAction(str, Enum):
    CONTINUE = "continue"     # 无危机信号，正常继续
    SOFTEN = "soften"         # elevated：温柔接住、放慢
    ESCALATE = "escalate"     # severe：安全接管 + 拉人


@dataclass(frozen=True)
class SafetyVerdict:
    action: SafetyAction
    level: str                 # none | elevated | severe（透传 detect_crisis）
    directive: str = ""        # 待注入下一轮的安全指令（供 bridge 塞进大脑）
    notify_human: bool = False  # 是否触发人工介入告警


# 语音通话专属安全指令（比文本更强调「口吻立即变化」——语音的接管靠语气不靠整段覆盖）。
_VOICE_SEVERE_DIRECTIVE = (
    "【最高优先·安全】对方刚刚透露了可能自伤或轻生的念头。从现在起，用最温柔、最稳的语气，"
    "放慢语速，先稳稳接住 TA 的情绪，绝不评判、绝不说教、绝不轻描淡写。让 TA 知道你在、"
    "你听见了、TA 不是一个人。如果 TA 愿意，温柔地提一句可以找人聊聊。此刻只做陪伴与倾听。"
)
_VOICE_ELEVATED_DIRECTIVE = (
    "【安全】对方情绪处于很深的低谷。放慢语速、压低音量，先共情再回应，多留白多倾听，"
    "别急着给建议或转移话题，让 TA 感到被稳稳接住。"
)


def assess_call_transcript(user_text: str, *, enabled: bool = True) -> SafetyVerdict:
    """对一条用户转写做危机判级，产出通话侧安全处置（纯函数）。

    复用 ``wellbeing_guard.detect_crisis`` 作单一事实源（与文本链、评测门禁同口径），
    绝不在通话侧另立一套危机词表（防口径漂移）。``detect_crisis`` 缺失时安全退化 CONTINUE。
    """
    if not enabled or not str(user_text or "").strip():
        return SafetyVerdict(SafetyAction.CONTINUE, "none")
    try:
        from src.utils.wellbeing_guard import detect_crisis
        signal = detect_crisis(user_text)
        level = str(signal.get("level") or "none")
    except Exception:
        return SafetyVerdict(SafetyAction.CONTINUE, "none")
    if level == "severe":
        return SafetyVerdict(SafetyAction.ESCALATE, "severe",
                             directive=_VOICE_SEVERE_DIRECTIVE, notify_human=True)
    if level == "elevated":
        return SafetyVerdict(SafetyAction.SOFTEN, "elevated",
                             directive=_VOICE_ELEVATED_DIRECTIVE)
    return SafetyVerdict(SafetyAction.CONTINUE, "none")


__all__ = ["SafetyAction", "SafetyVerdict", "assess_call_transcript"]
