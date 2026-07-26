"""每日「拍」规划器（纯函数，零 IO）。

职责：给定目标 + 信号 + 历史消耗计数，决定**今天**这个目标该怎么推进：

- ``hold``（不注入推进块）：强负面情绪 / 连发多拍对方一直没回（沉默熔断）。
- ``push_level=none``（退避陪伴日）：连发未回达退避阈值 → 今日意图换成纯陪伴。
- 正常拍：从模板意图池确定性取「今日意图」+ 里程碑力度曲线。

信号自适应对齐 AI SDR 「adaptive cadence」思想，但所有判定确定性可单测；
情绪红线沿用既有 ``proactive_emotion_gate`` 的保守精神（宁静默不冒犯）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from src.companion.goals.signals import GoalSignals
from src.companion.goals.templates import (
    pick_care_intent,
    pick_intent,
    push_for_milestone,
)

# 强负面情绪判定线（与 proactive_emotion_gate 的 min_negative_intensity 同刻度）
NEGATIVE_INTENSITY_HOLD = 0.5


def day_key(now: Optional[float] = None) -> str:
    """本地日键 YYYY-MM-DD（拍的幂等键；与运营口径一致用本地时区）。"""
    t = time.localtime(now if now is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def plan_beat(
    *,
    template: Dict[str, Any],
    goal: Dict[str, Any],
    signals: GoalSignals,
    day: str,
    engaged_since_inbound: int = 0,
    backoff_after: int = 2,
    halt_after: int = 4,
    recent_rejects: int = 0,
) -> Optional[Dict[str, Any]]:
    """规划今天的拍。返回 ``{intent, push_level}``；hold 时返回
    ``{"hold": reason}``（调用方不建拍、不注入推进块）。

    Args:
        engaged_since_inbound: 对方最近一次开口之后，我们已带目标进入生成/发出的拍数
            （store.count_engaged_since 提供）。对方一直不回还连着推 = 骚扰，熔断。
        backoff_after: 达到该数 → 退避陪伴日（push none，只陪伴不推进）。
        halt_after: 达到该数 → 完全 hold（连陪伴式的目标块也不注入，彻底让路）。
        recent_rejects: 坐席近窗驳回今日拍的次数（store.count_events_since
            ``beat_rejected`` 提供）。人审说「推得不对」是最强的负反馈信号：
            1 次 → 力度封顶 soft（direct 降档）；≥2 次 → 退避陪伴日。
            回流只降不升——坐席采纳不加码，防正反馈螺旋。
    """
    # 1) 情绪红线：强负面 → 彻底放下目标（危机场景另有 crisis safety net 兜底）
    if signals.negative_emotion and (
        signals.emotion_intensity < 0
        or signals.emotion_intensity >= NEGATIVE_INTENSITY_HOLD
    ):
        return {"hold": "emotion"}

    # 2) 沉默熔断：对方一直没回还连着带目标说话 → 停
    engaged = max(0, int(engaged_since_inbound or 0))
    if halt_after > 0 and engaged >= int(halt_after):
        return {"hold": "silent"}

    gid = str(goal.get("goal_id") or "")
    mi = int(goal.get("milestone_idx") or 0)
    rejects = max(0, int(recent_rejects or 0))

    # 3) 退避陪伴日：连发未回 或 坐席连续驳回 → 推进降为纯陪伴
    if (backoff_after > 0 and engaged >= int(backoff_after)) or rejects >= 2:
        return {"intent": pick_care_intent(gid, day), "push_level": "none"}

    # 4) 正常拍：模板意图池确定性轮换 + 里程碑力度
    intent = pick_intent(template, mi, gid, day, params=goal.get("params") or {})
    if not intent:
        return {"hold": "no_intent"}
    push = push_for_milestone(template, mi)
    if rejects >= 1 and push == "direct":
        push = "soft"          # 坐席驳回过 → 力度封顶 soft
    return {"intent": intent, "push_level": push}


__all__ = ["NEGATIVE_INTENSITY_HOLD", "day_key", "plan_beat"]
