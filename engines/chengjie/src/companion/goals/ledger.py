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
from src.companion.goals.templates import (
    STAGE_ORDER,
    milestone_count,
    phase_cap,
    phase_floor,
    scaled_phase_days,
)

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
    elif tid == "acquire_and_convert":
        # 完成：官网成交发生在站外，无自动硬信号——运营手动标记 done；
        # 若 params.item_id 配了站内权益项则也认 entitlement（兼容口径）。
        item = str(params.get("item_id") or "").strip()
        if item and entitlement_unlocked(signals.entitlement, item):
            done, result = True, f"unlocked:{item}"
        # 信号推进（单调）：开口 → 画像过半 → 画像够+关系热 → 已开价
        # bant_fill/relation_fill 由 service 从 customer_profiles 现算注入 extras
        # （-1 = 画像未知 → 该判据不参与，退回纯关系信号）。
        bant = float(signals.extras.get("bant_fill", -1.0) or -1.0)
        if inbound_after_start:
            mi = max(mi, 1)
        if bant >= 0.5 or intimacy >= 20:
            mi = max(mi, 2)
        if (bant >= 0.75 and (intimacy >= 30 or _stage_index(
                signals.funnel_stage) >= _stage_index("engaged"))):
            mi = max(mi, 3)
        if direct_beat_engaged:
            mi = max(mi, 4)
        # progress 在下方相位块统一按封顶后的 mi 计（防「进度条超前于里程碑」）
    elif tid == "retention_expand":
        # 留存环（P5）：激活=购后开口；价值确认/深化按相位天窗走（下方通用块）；
        # 续费拍被接（direct_beat_engaged）→ 收口段。完成信号在站外——续费单经
        # settle_order_ref 外部结算或坐席标成交，ledger 自身绝不自动 done；
        # 到期没续走通用 expired（诚实流失记录，供 outcome_report 读流失率）。
        if inbound_after_start:
            mi = max(mi, 1)
        if direct_beat_engaged:
            mi = max(mi, 3)
    elif tid == "profile_discovery":
        # 摸底目标（P26）：勾选槽位填充率即进度——客户说了职业当轮推进、
        # 全说出来自动达成（settle-on-read，下轮读取/注入即结算）。
        # selected_fill 由 service 从 customer_profiles×params.slots 现算注入
        # extras；-1=勾选为空/画像不可读 → 只按开口+相位天窗推进，绝不自动完成。
        fill = float(signals.extras.get("selected_fill", -1.0) or -1.0)
        if fill >= 0.999:
            done, result = True, "slots_filled"
        if inbound_after_start:
            mi = max(mi, 1)
        if fill >= 0.5:
            mi = max(mi, 2)
        if fill >= 0.8:
            mi = max(mi, 3)
        if fill >= 0.0:
            progress = max(progress, round(fill * 0.95, 3))
    else:  # custom / 未知模板：只按时间显示推进，绝不自动完成
        total = max(1.0, _total_days(goal, template.get("default_days", 14)))
        frac = max(0.0, min(1.0, _elapsed_days(goal, n) / total))
        mi = max(mi, min(3, int(frac * 4)))
        progress = max(progress, round(min(0.95, frac), 3))
        # #65 C1（2026-09-04）：限时档（today/session）进度按「已出手的拍」爬。
        # 纯时间口径在 3 小时目标里＝聊了半小时还是 9%（用户原话），看着像没执行。
        # service 只在 sprint 时塞 extras.sprint_beats/sprint_cap（natural 不带
        # 这两个键 → 本段零感知，行为逐字不变）。单调不回退、仍绝不自动完成
        # （封顶 0.9：拍打满＝「该说的都说了」，成没成要看对方/人工确认）。
        beats_raw = signals.extras.get("sprint_beats")
        if beats_raw is not None:
            try:
                beats = max(0, int(beats_raw))
                cap = max(1, int(signals.extras.get("sprint_cap") or 3))
            except (TypeError, ValueError):
                beats, cap = 0, 3
            if beats > 0:
                mi = max(mi, min(3, beats))
                progress = max(progress, round(
                    min(0.9, beats / float(cap) * 0.9), 3))

    # ── 时间相位（模板声明 phase_days 才生效；存量模板零行为变更）──────────────
    # 兑底：天窗过了信号还没到 → 按时推进（「1-3 天摸底、7-10 天收口」的硬保证）；
    # 封顶：信号超热也只允许超前一段（第 1 天不开价——节奏是弧线不是开关）。
    # 两者都不回退已达成的里程碑（单调不变量优先于封顶）。
    if not done and status == "active":
        pdays = scaled_phase_days(
            template, _total_days(goal, template.get("default_days", 10)))
        if pdays:
            elapsed = _elapsed_days(goal, n)
            floor_i = phase_floor(pdays, elapsed)
            cap_i = max(phase_cap(pdays, elapsed), old_mi)
            mi = max(old_mi, min(max(mi, floor_i), cap_i))
            n_ms = max(1, milestone_count(template))
            progress = max(progress, round((mi / float(n_ms)) * 0.9, 3))

    if done and status == "active":
        status = "done"
        progress = 1.0
        mi = max(mi, milestone_count(template) - 1)
        events.append(("status", f"done:{result}"))
    elif status == "active" and deadline_ts > 0 and n > deadline_ts:
        # 过期：唤回类=failed（对方没回来），其余=expired（到期未达成）。
        # P2 2026-08-30：曾检出达成信号（对方给过联系方式）但没人确认 →
        # result 单列 expired_with_signal——报表把「疑似成了没人点」与
        # 「真没成」分开数，终局卡据此出「补确认」入口（不冒充人工确认）。
        status = "failed" if tid == "engagement_reactivate" else "expired"
        if not result:
            result = ("expired_with_signal"
                      if isinstance(params.get("outcome_signal"), dict)
                         and params.get("outcome_signal")
                      else "deadline")
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
