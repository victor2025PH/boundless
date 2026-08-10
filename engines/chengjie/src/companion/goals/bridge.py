"""P1 主动触达桥——auto 档目标搭 ``proactive_topic`` 的**顺风车**（默认关）。

刻意**不做**独立派发器/发送队列：主动触达的全部护栏（沉默阈值、双轴 pacing、
冷却、安静时段、情绪护栏、care 让路、跨 loop marshalling、语音/生活照分支、
坏 peer 拉黑）都在既有 ``CompanionProactiveLoop`` 里——目标桥只在**本来就要发**
的主动开场上，把「今日拍」的意图并进 directive；发送成功由 ``on_sent`` 回执
标记拍已发出。目标绝不自己发起发送。

两个挂点（都随 ``bridge.enabled`` 同开同关）：
- ``plan_priority``（排序增益）：候选排序时目标会话优先占每 tick 名额——只改
  顺序不改准入，护栏照旧。
- ``augment_plan_with_goal``（意图增广）：真发前把「今日拍」并进 directive。

准入（三闸全过才增广）：
1. ``companion.goals.enabled`` + ``companion.goals.bridge.enabled``（默认关）
2. 目标 ``autonomy == "auto"``（suggest/observe 不上桥）
3. 会话档位 == ``auto_ai``（人审会话绝不自动带营销意图出门）

全部 best-effort：桥任何异常都不影响主动开场本身。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("GoalBridge")


def bridge_enabled(cfg_root: Any) -> bool:
    try:
        from src.companion.goals.service import resolve_goals_cfg
        cfg = resolve_goals_cfg(cfg_root)
        if not cfg.get("enabled", False):
            return False
        return bool((cfg.get("bridge") or {}).get("enabled", False))
    except Exception:
        return False


def plan_priority(
    cfg_root: Any,
    config_path: Any,
    plan: Dict[str, Any],
) -> float:
    """proactive 候选**排序增益**（``plan_proactive_sends(priority_fn=)`` 消费）：
    会话有 auto 档活跃目标 → ``1 + goal.priority``（多目标会话间还能按目标优先级
    分高下）；其余 0。

    刻意轻量：只查 store 一次，不结算不写库——每 tick 对幸存候选各付一次进程内
    SQLite 读。真正带不带「今日拍」意图仍由发送时 ``augment_plan_with_goal`` 决定
    （那里才有 hold/沉默熔断/会话档位闸）；这里只回答「谁先用掉本 tick 名额」。
    任何异常按 0（绝不影响主动开场本身的排序兜底）。"""
    try:
        if not bridge_enabled(cfg_root):
            return 0.0
        conversation_id = str(plan.get("conversation_id") or "")
        platform = str(plan.get("platform") or "")
        account_id = str(plan.get("account_id") or "")
        chat_key = str(plan.get("chat_key") or "")
        if not conversation_id and not (platform and chat_key):
            return 0.0
        from src.companion.goals.service import get_configured_store
        store = get_configured_store(cfg_root, config_path)
        goal = store.find_active_goal(
            conversation_id=conversation_id, platform=platform,
            chat_key=chat_key, account_id=account_id)
        if goal is None or str(goal.get("autonomy") or "") != "auto":
            return 0.0
        # 今日拍已发/被驳回 → 今天带不了目标意图，别浪费名额增益
        from src.companion.goals.planner import day_key
        action = store.get_action(str(goal.get("goal_id") or ""), day_key())
        if action is not None and str(action.get("status")) in (
                "sent", "skipped", "blocked"):
            return 0.0
        try:
            pr = int(goal.get("priority") or 1)
        except (TypeError, ValueError):
            pr = 1
        return 1.0 + float(max(0, min(pr, 9)))
    except Exception:
        logger.debug("plan_priority failed", exc_info=True)
        return 0.0


def augment_plan_with_goal(
    cfg_root: Any,
    config_path: Any,
    plan: Dict[str, Any],
    *,
    inbox_store: Any = None,
    now: Optional[float] = None,
) -> None:
    """真发前调用：若该会话有 auto 档活跃目标 + 今日拍可推进 →
    把目标意图并进 ``plan["directive"]`` 并在 plan 上留 ``_goal_action_id`` 回执钩。
    就地修改 plan；任何情况不抛。"""
    try:
        if not bridge_enabled(cfg_root):
            return
        conversation_id = str(plan.get("conversation_id") or "")
        platform = str(plan.get("platform") or "")
        account_id = str(plan.get("account_id") or "")
        chat_key = str(plan.get("chat_key") or "")
        if not conversation_id and not (platform and chat_key):
            return
        from src.companion.goals.service import (
            get_configured_store,
            refresh_goal,
        )
        store = get_configured_store(cfg_root, config_path)
        goal = store.find_active_goal(
            conversation_id=conversation_id, platform=platform,
            chat_key=chat_key, account_id=account_id)
        if goal is None or str(goal.get("autonomy") or "") != "auto":
            return
        # 会话档位闸：仅全自动会话（人审会话的营销推进必须过人）
        if inbox_store is not None and conversation_id:
            try:
                mode = str(inbox_store.get_automation_mode(conversation_id) or "")
                if mode != "auto_ai":
                    return
            except Exception:
                return
        res = refresh_goal(store, cfg_root, goal, inbox_store=inbox_store, now=now)
        if res.get("hold") or str((res.get("goal") or {}).get("status")) != "active":
            return
        action = res.get("action")
        if action is None:
            return
        if str(action.get("status")) in ("sent", "skipped", "blocked"):
            return  # 已发出不重复带；坐席驳回/受阻 → 今天不带
        intent = str(action.get("intent") or "").strip()
        if not intent:
            return
        push = str(action.get("push_level") or "soft")
        # P27：摸底目标的当前缺口并进意图（与注入链同一 merged_beat_intent
        # ——桥直读 DB 拍意图，缺口原本到不了主动开场）。none 力度日不合。
        if push != "none":
            try:
                from src.companion.goals.service import (
                    discovery_gap_for_goal,
                    merged_beat_intent,
                )
                from src.companion.goals.templates import get_template
                _tpl = get_template(str(goal.get("template") or "")) or {}
                _gap = discovery_gap_for_goal(store, _tpl, goal)
                if _gap:
                    intent = merged_beat_intent(intent, _gap)
            except Exception:
                logger.debug("bridge gap merge skipped", exc_info=True)
        if push == "none":
            hint = "（今天只陪伴，营销内容只字不提）"
        elif push == "direct":
            hint = "（对方兴致好可以直说，但不硬销）"
        else:
            hint = "（只在话题自然贴近时轻轻带到）"
        plan["directive"] = (
            str(plan.get("directive") or "").rstrip()
            + f"\n【工作目标衔接】{intent}{hint}"
        ).strip()
        plan["_goal_action_id"] = str(action.get("action_id") or "")
        plan["_goal_id"] = str(goal.get("goal_id") or "")
        aid = str(action.get("action_id") or "")
        if aid and str(action.get("status")) == "planned":
            store.mark_action(aid, "consumed", detail="proactive")
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_injected("proactive")
        except Exception:
            pass
    except Exception:
        logger.debug("augment_plan_with_goal failed", exc_info=True)


def on_proactive_sent(plan: Dict[str, Any]) -> None:
    """发送成功回执：标记今日拍已发出（观测 + 防同日重复带）。绝不抛。"""
    try:
        aid = str((plan or {}).get("_goal_action_id") or "")
        gid = str((plan or {}).get("_goal_id") or "")
        if not aid:
            return
        from src.companion.goals.store import peek_goal_store
        store = peek_goal_store()
        if store is None:
            return
        store.mark_action(aid, "sent", detail="proactive")
        if gid:
            store.add_event(gid, "beat_sent", "proactive")
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_beat_sent_proactive()
        except Exception:
            pass
    except Exception:
        logger.debug("on_proactive_sent failed", exc_info=True)


__all__ = [
    "augment_plan_with_goal",
    "bridge_enabled",
    "on_proactive_sent",
    "plan_priority",
]
