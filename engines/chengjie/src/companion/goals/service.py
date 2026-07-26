"""目标服务编排层——routes / skill_manager 注入 / 主动桥的**同一入口**。

口径唯一性是本模块存在的理由：右栏卡显示的里程碑、prompt 注入的里程碑、
主动桥判定的拍，全部经 ``refresh_goal``（settle-on-read + 当日拍幂等规划）
产出——三个消费面读同一份结算结果，杜绝「卡片说第2步、prompt 说第3步」的漂移。

所有函数绝不抛：目标层任何失败都不能拖垮聊天/草稿/主动触达主链路。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.companion.goals import store as goal_store_mod
from src.companion.goals.context_block import goal_view_block
from src.companion.goals.ledger import settle_goal
from src.companion.goals.planner import day_key, plan_beat
from src.companion.goals.signals import collect_signals
from src.companion.goals.stats import get_goal_stats
from src.companion.goals.store import GoalStore, get_goal_store
from src.companion.goals.templates import (
    get_template,
    milestone_label,
)

logger = logging.getLogger("GoalService")

_DAY = 86400.0

# 强负面情绪 hint 词表（user_context.user_emotion_hint / conversation last_emotion）
NEGATIVE_EMOTIONS = frozenset(
    ("sad", "angry", "anxious", "depressed", "negative", "fear", "tired",
     "frustrated", "grief"))

DEFAULT_DB_NAME = "marketing_goals.db"


def resolve_goals_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals`` 配置段（缺/异常 → {} = 默认关）。"""
    try:
        if not isinstance(cfg_root, dict):
            return {}
        return (cfg_root.get("companion") or {}).get("goals") or {}
    except Exception:
        return {}


def goals_enabled(cfg_root: Any) -> bool:
    return bool(resolve_goals_cfg(cfg_root).get("enabled", False))


def resolve_db_path(cfg_root: Any, config_path: Any = None) -> str:
    """目标库路径：``companion.goals.db_path`` 显式值 > ``<config 目录>/marketing_goals.db``。"""
    cfg = resolve_goals_cfg(cfg_root)
    explicit = str(cfg.get("db_path") or "").strip()
    if explicit == ":memory:":
        return ":memory:"
    if explicit:
        p = Path(explicit)
        if not p.is_absolute() and config_path:
            p = Path(config_path).parent / p
        return str(p)
    if config_path:
        return str(Path(config_path).parent / DEFAULT_DB_NAME)
    return str(Path("config") / DEFAULT_DB_NAME)


def get_configured_store(cfg_root: Any, config_path: Any = None) -> GoalStore:
    """进程单例 store（首次调用据配置定库路径）。"""
    return get_goal_store(resolve_db_path(cfg_root, config_path))


def is_negative_emotion(hint: Any) -> bool:
    return str(hint or "").strip().lower() in NEGATIVE_EMOTIONS


def refresh_goal(
    store: GoalStore,
    cfg_root: Any,
    goal: Dict[str, Any],
    *,
    inbox_store: Any = None,
    negative_emotion: bool = False,
    emotion_intensity: float = -1.0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """结算 + 当日拍规划的单一入口。返回
    ``{"goal": 最新行, "action": 今日拍|None, "hold": 原因|None}``。绝不抛。"""
    n = float(now if now is not None else time.time())
    out: Dict[str, Any] = {"goal": goal, "action": None, "hold": None}
    try:
        gid = str(goal.get("goal_id") or "")
        tid = str(goal.get("template") or "")
        template = get_template(tid) or {}
        signals = collect_signals(
            platform=str(goal.get("platform") or ""),
            account_id=str(goal.get("account_id") or ""),
            chat_key=str(goal.get("chat_key") or ""),
            conversation_id=str(goal.get("conversation_id") or ""),
            inbox_store=inbox_store,
            negative_emotion=negative_emotion,
            emotion_intensity=emotion_intensity,
            now=n,
        )
        stats = get_goal_stats()

        # 「已开价」证据：direct 拍进入过生成/发出（转化模板里程碑 3 的推进条件）
        direct_engaged = False
        try:
            for a in store.list_actions(gid, limit=60):
                if (str(a.get("push_level")) == "direct"
                        and str(a.get("status")) in ("consumed", "sent")):
                    direct_engaged = True
                    break
        except Exception:
            direct_engaged = False

        res = settle_goal(
            template_id=tid, template=template, goal=goal, signals=signals,
            direct_beat_engaged=direct_engaged, now=n)
        adv = max(0, int(res["milestone_idx"]) - int(goal.get("milestone_idx") or 0))
        stats.record_settle(milestones_advanced=adv)
        if res["changed"]:
            store.update_goal_fields(
                gid, status=res["status"], milestone_idx=res["milestone_idx"],
                progress=res["progress"], result=res["result"],
                done_at=(n if res["status"] in ("done", "failed", "expired")
                         and str(goal.get("status")) == "active" else
                         float(goal.get("done_at") or 0)),
            )
            for kind, detail in res["events"]:
                store.add_event(gid, kind, detail)
            if (res["status"] != str(goal.get("status"))
                    and res["status"] in ("done", "failed", "expired")):
                stats.record_terminal(res["status"])
            goal = store.get_goal(gid) or goal
        out["goal"] = goal

        if str(goal.get("status")) != "active":
            return out

        # 当日拍（幂等）：已有 → 直接用；没有 → 规划（hold 则不建）
        day = day_key(n)
        action = store.get_action(gid, day)
        if action is None:
            cfg = resolve_goals_cfg(cfg_root)
            pl = (cfg.get("planner") or {}) if isinstance(cfg, dict) else {}
            engaged = store.count_engaged_since(gid, signals.last_inbound_ts)
            # 坐席驳回回流（P2）：近窗 beat_rejected 事件 → planner 降档/退避
            try:
                lookback = float(pl.get("reject_lookback_days", 7) or 7)
            except (TypeError, ValueError):
                lookback = 7.0
            rejects = store.count_events_since(
                gid, "beat_rejected", n - max(1.0, lookback) * _DAY)
            beat = plan_beat(
                template=template, goal=goal, signals=signals, day=day,
                engaged_since_inbound=engaged,
                backoff_after=int(pl.get("backoff_after_unanswered", 2) or 0),
                halt_after=int(pl.get("halt_after_unanswered", 4) or 0),
                recent_rejects=rejects,
            )
            if beat is None or beat.get("hold"):
                reason = str((beat or {}).get("hold") or "no_intent")
                stats.record_hold(reason)
                out["hold"] = reason
                return out
            action = store.upsert_action(
                gid, day, intent=str(beat.get("intent") or ""),
                push_level=str(beat.get("push_level") or "soft"), now=n)
            if action is not None:
                stats.record_beat_planned()
        out["action"] = action
    except Exception:
        logger.debug("refresh_goal failed", exc_info=True)
    return out


def goal_view(
    goal: Dict[str, Any],
    action: Optional[Dict[str, Any]] = None,
    hold: Optional[str] = None,
    *,
    lang: str = "zh",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """API/UI/注入共用的视图形状。"""
    n = float(now if now is not None else time.time())
    tid = str(goal.get("template") or "")
    template = get_template(tid) or {}
    start = float(goal.get("start_ts") or 0)
    deadline = float(goal.get("deadline_ts") or 0)
    total_days = int(round((deadline - start) / _DAY)) if (
        start > 0 and deadline > start) else int(template.get("default_days") or 0)
    day_index = int((n - start) // _DAY) + 1 if start > 0 else 1
    title = str(goal.get("title") or "").strip()
    if not title:
        key = "name_en" if str(lang).lower().startswith("en") else "name_zh"
        title = str(template.get(key) or tid)
        params = goal.get("params") or {}
        label = str(params.get("item_label") or "").strip()
        if label:
            title = f"{title}：{label}"
    mi = int(goal.get("milestone_idx") or 0)
    view: Dict[str, Any] = {
        "goal_id": str(goal.get("goal_id") or ""),
        "conversation_id": str(goal.get("conversation_id") or ""),
        "platform": str(goal.get("platform") or ""),
        "account_id": str(goal.get("account_id") or ""),
        "chat_key": str(goal.get("chat_key") or ""),
        "template": tid,
        "template_name": str(template.get(
            "name_en" if str(lang).lower().startswith("en") else "name_zh") or tid),
        "title": title,
        "params": goal.get("params") or {},
        "status": str(goal.get("status") or "active"),
        "autonomy": str(goal.get("autonomy") or "suggest"),
        "priority": int(goal.get("priority") or 1),
        "milestone_idx": mi,
        "milestone_label": milestone_label(template, mi, lang),
        "milestones": [dict(m) for m in (template.get("milestones") or [])],
        "progress": float(goal.get("progress") or 0.0),
        "day_index": day_index,
        "total_days": total_days,
        "start_ts": start,
        "deadline_ts": deadline,
        "result": str(goal.get("result") or ""),
        "created_at": float(goal.get("created_at") or 0),
        "updated_at": float(goal.get("updated_at") or 0),
        "hold": hold,
        "today": None,
    }
    if action is not None:
        view["today"] = {
            "action_id": str(action.get("action_id") or ""),
            "day": str(action.get("day") or ""),
            "intent": str(action.get("intent") or ""),
            "push_level": str(action.get("push_level") or "soft"),
            "status": str(action.get("status") or "planned"),
            # detail 透传：坐席「采纳」标记（"adopted"）/驳回原因（"rejected:*"）
            # → 右栏卡按此渲染反馈态
            "detail": str(action.get("detail") or ""),
        }
    return view


def build_block_for_chat(
    config_obj: Any,
    *,
    platform: str,
    chat_key: str,
    account_id: str = "",
    conversation_id: str = "",
    user_context: Optional[Dict[str, Any]] = None,
    chain: str = "reply",
    inbox_store: Any = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """skill_manager 注入口（单调用闭环）：找活跃目标 → 结算+当日拍 → 组块 →
    标记拍已进入生成。未启用/无目标/hold → None。绝不抛。"""
    try:
        cfg_root = getattr(config_obj, "config", None)
        if not isinstance(cfg_root, dict):
            cfg_root = config_obj if isinstance(config_obj, dict) else {}
        cfg = resolve_goals_cfg(cfg_root)
        if not cfg.get("enabled", False):
            return None
        inject_cfg = cfg.get("inject") or {}
        if not inject_cfg.get("enabled", True):
            return None
        store = get_configured_store(
            cfg_root, getattr(config_obj, "config_path", None))
        goal = store.find_active_goal(
            conversation_id=conversation_id, platform=platform,
            chat_key=str(chat_key or ""), account_id=account_id)
        if goal is None:
            return None
        # observe 档：目标只作看板观测，完全不进 prompt
        if str(goal.get("autonomy") or "suggest") == "observe":
            return None
        uc = user_context or {}
        neg = is_negative_emotion(uc.get("user_emotion_hint"))
        res = refresh_goal(
            store, cfg_root, goal, inbox_store=inbox_store,
            negative_emotion=neg, now=now)
        if res.get("hold") or str((res.get("goal") or {}).get("status")) != "active":
            return None
        action = res.get("action")
        # 坐席驳回今日拍（P2）→ 今天彻底不注入（明日 planner 按驳回回流降档重排）
        if action is not None and str(action.get("status")) in ("skipped", "blocked"):
            return None
        view = goal_view(res["goal"], res.get("action"), now=now)
        block = goal_view_block(
            view,
            suppress_push=bool(str(uc.get("_bazi_block") or "").strip()),
            max_chars=int(inject_cfg.get("max_chars", 360) or 360),
        )
        if not block:
            return None
        if action is not None and str(action.get("status")) == "planned":
            store.mark_action(
                str(action.get("action_id")), "consumed", detail=chain)
        get_goal_stats().record_injected(chain)
        return block
    except Exception:
        logger.debug("build_block_for_chat failed", exc_info=True)
        return None


__all__ = [
    "DEFAULT_DB_NAME",
    "NEGATIVE_EMOTIONS",
    "build_block_for_chat",
    "get_configured_store",
    "goal_view",
    "goals_enabled",
    "is_negative_emotion",
    "refresh_goal",
    "resolve_db_path",
    "resolve_goals_cfg",
]
