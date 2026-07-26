"""目标结算器（纯函数，零 IO）——「目标到哪一步了」的单一判定口径。

**settle-on-read**：不订阅事件，读到目标时按当下信号快照结算一次
（幂等、确定性）。判定内容：

- 完成（done）：模板各自的硬信号——付费解锁到账 / 会员档位到位 / 漏斗阶段达标 /
  亲密度达标 / 沉默用户开口。
- 过期（expired/failed）：过 deadline 未完成。
- 里程碑推进（milestone_idx）与进度（progress）：**单调不回退**（信号抖动不
  让 UI 上的进度倒着走；完成恒 1.0）。

调用方（service）负责把 ``changed`` 的字段落库 + 记 events + 计 stats。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.companion.goals.signals import (
    GoalSignals,
    entitlement_tier,
    entitlement_unlocked,
)
from src.companion.goals.templates import STAGE_ORDER

_DAY = 86400.0


def _stage_index(stage: str) -> int:
    s = str(stage or "").strip().lower()
    try:
        return STAGE_ORDER.index(s)
    except ValueError:
        return -1


def _elapsed_days(goal: Dict[str, Any], now: float) -> float:
    try:
        start = float(goal.get("start_ts") or 0)
    except (TypeError, ValueError):
        start = 0.0
    if start <= 0:
        return 0.0
    return max(0.0, (now - start) / _DAY)


def _total_days(goal: Dict[str, Any], default_days: float) -> float:
    try:
        start = float(goal.get("start_ts") or 0)
        dl = float(goal.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        return default_days
    if start > 0 and dl > start:
        return (dl - start) / _DAY
    return default_days


def settle_goal(
    *,
    template_id: str,
    template: Dict[str, Any],
    goal: Dict[str, Any],
    signals: GoalSignals,
    direct_beat_engaged: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """结算一次。返回::

        {"status": str, "milestone_idx": int, "progress": float,
         "result": str, "changed": bool, "events": [(kind, detail), ...]}

    Args:
        direct_beat_engaged: 是否已有 ``push_level=direct`` 的拍进入过生成/发出
            （转化模板 milestone 3「跟进收口」的推进证据，由 store 侧统计传入）。
    """
    n = float(now if now is not None else signals.now or 0.0)
    old_status = str(goal.get("status") or "active")
    old_mi = int(goal.get("milestone_idx") or 0)
    try:
        old_progress = float(goal.get("progress") or 0.0)
    except (TypeError, ValueError):
        old_progress = 0.0

    status = old_status
    mi = old_mi
    progress = old_progress
    result = str(goal.get("result") or "")
    events: List[tuple] = []
    params = goal.get("params") or {}
    tid = str(template_id or "")
    start_ts = float(goal.get("start_ts") or 0.0)
    deadline_ts = float(goal.get("deadline_ts") or 0.0)
    intimacy = float(signals.intimacy)
    inbound_after_start = (
        signals.last_inbound_ts > 0 and start_ts > 0
        and signals.last_inbound_ts > start_ts)

    # ── 完成判定（先于过期：deadline 当天完成算完成）────────────────────────
    done = False
    if tid == "conversion_unlock":
        item = str(params.get("item_id") or "").strip()
        if item and entitlement_unlocked(signals.entitlement, item):
            done, result = True, f"unlocked:{item}"
        # 里程碑推进（单调）：开口/回暖 → 价值 → 开价证据 → 收口
        if inbound_after_start or intimacy >= 25:
            mi = max(mi, 1)
        if intimacy >= 40 or _stage_index(signals.funnel_stage) >= _stage_index("engaged"):
            mi = max(mi, 2)
        if direct_beat_engaged:
            mi = max(mi, 3)
        progress = max(progress, (mi / 4.0) * 0.9)
    elif tid == "conversion_subscribe":
        tier = str(params.get("tier") or "").strip().lower()
        if tier and entitlement_tier(signals.entitlement) == tier:
            done, result = True, f"subscribed:{tier}"
        if inbound_after_start or intimacy >= 25:
            mi = max(mi, 1)
        if intimacy >= 40:
            mi = max(mi, 2)
        if direct_beat_engaged:
            mi = max(mi, 3)
        progress = max(progress, (mi / 4.0) * 0.9)
    elif tid == "relationship_stage":
        target = str(params.get("target_stage") or "").strip().lower()
        cur_i, tgt_i = _stage_index(signals.funnel_stage), _stage_index(target)
        if target and signals.funnel_stage == target:
            done, result = True, f"stage:{target}"
        elif tgt_i >= 0 and cur_i >= tgt_i >= 0:
            done, result = True, f"stage:{signals.funnel_stage}"
        elif tgt_i > 0 and cur_i >= 0:
            frac = max(0.0, min(1.0, cur_i / float(tgt_i)))
            mi = max(mi, min(3, int(frac * 4)))
            progress = max(progress, round(frac, 3))
    elif tid == "relationship_intimacy":
        try:
            target = float(params.get("target_score") or 55)
        except (TypeError, ValueError):
            target = 55.0
        target = max(1.0, target)
        if intimacy >= 0:
            frac = max(0.0, min(1.0, intimacy / target))
            if intimacy >= target:
                done, result = True, f"intimacy:{intimacy:.0f}"
            else:
                if frac >= 0.4:
                    mi = max(mi, 1)
                if frac >= 0.7:
                    mi = max(mi, 2)
                if frac >= 0.9:
                    mi = max(mi, 3)
                progress = max(progress, round(frac, 3))
    elif tid == "engagement_reactivate":
        if inbound_after_start:
            done, result = True, "replied"
        else:
            total = max(1.0, _total_days(goal, template.get("default_days", 10)))
            frac = max(0.0, min(1.0, _elapsed_days(goal, n) / total))
            mi = max(mi, min(3, int(frac * 4)))
            progress = max(progress, round(frac * 0.8, 3))
    else:  # custom / 未知模板：只按时间显示推进，绝不自动完成
        total = max(1.0, _total_days(goal, template.get("default_days", 14)))
        frac = max(0.0, min(1.0, _elapsed_days(goal, n) / total))
        mi = max(mi, min(3, int(frac * 4)))
        progress = max(progress, round(min(0.95, frac), 3))

    if done and status == "active":
        status = "done"
        progress = 1.0
        mi = max(mi, 3)
        events.append(("status", f"done:{result}"))
    elif status == "active" and deadline_ts > 0 and n > deadline_ts:
        # 过期：唤回类=failed（对方没回来），其余=expired（到期未达成）
        status = "failed" if tid == "engagement_reactivate" else "expired"
        result = result or "deadline"
        events.append(("status", f"{status}:deadline"))

    if mi != old_mi and status in ("active", "done"):
        events.append(("milestone", f"{old_mi}->{mi}"))

    changed = (
        status != old_status or mi != old_mi
        or round(progress, 3) != round(old_progress, 3))
    return {
        "status": status,
        "milestone_idx": mi,
        "progress": round(float(progress), 3),
        "result": result,
        "changed": changed,
        "events": events,
    }


__all__ = ["settle_goal"]
