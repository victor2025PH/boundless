"""实施84：主动关怀 × 工作目标（marketing goals）联通层。

三个方向的数据流（此前两系统零引用，坐席设了计划、AI 主动开口却自说自话）：

- **P0-2 目标背景注入**（``care_goal_hint``，``proactive_care.goal_hint`` 默认开）：
  care 拟稿时把该会话活跃目标的标题/今日方向作背景块喂给 LLM——关怀顺势带
  方向，绝不硬销。只认 ``autonomy == "auto"`` 的目标（与 goals.bridge 同一准入：
  机器主动出口不替 suggest/observe 档带营销意图）。**只读**：不 refresh、不
  mark 拍——care 是背景引用，拍的生命周期仍归回复链/主动桥。
- **P1-1 捕获回流**（``record_capture_event``，``goal_link.enabled`` +
  ``capture_events``）：care 从聊天里捕获的约定写进目标事件时间线
  （``goal_events``），坐席在目标卡「AI 做了什么」直接看到——零新前端。
- **P1-2 到期排期**（``scan_goal_deadlines`` / ``CareGoalScanner``，
  ``goal_link.enabled``）：auto 档活跃目标 deadline 前 N 天写一条
  ``topic_norm=goal:<goal_id>`` 的关怀待办，走 care 全套派发护栏（预算/安静
  时段/人审/dry_run）。与 goals.bridge 分工：bridge=搭沉默回访的顺风车
  （有车才搭）；本层=目标自己的时刻表（没车也到点发）。

全部 best-effort：goals 子系统缺席/关闭/异常 → 空产出，绝不影响 care 主链。
配置（``companion.proactive_care``）：``goal_hint``（默认 true，随 care 总闸）、
``goal_link.{enabled(默认 false), days_before(3), capture_events(true)}``。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from src.contacts.care_schedule import GOAL_CARE_NORM_PREFIX

logger = logging.getLogger(__name__)

_DAY = 86400.0


def _cfg_root(config_obj: Any) -> dict:
    cfg = getattr(config_obj, "config", None)
    if isinstance(cfg, dict):
        return cfg
    return config_obj if isinstance(config_obj, dict) else {}


def _care_cfg(config_obj: Any) -> dict:
    try:
        return dict((_cfg_root(config_obj).get("companion") or {})
                    .get("proactive_care") or {})
    except Exception:
        return {}


def _goals_ready(config_obj: Any, goals_store: Any = None):
    """goals 子系统开着 → (store, cfg)；否则 (None, {})。lazy import 防硬依赖。

    ``goals_store``＝显式注入（单测不走进程单例）；None 走
    ``get_configured_store`` 生产路径。
    """
    try:
        from src.companion.goals.service import (
            get_configured_store,
            resolve_goals_cfg,
        )
        root = _cfg_root(config_obj)
        gcfg = resolve_goals_cfg(root)
        if not gcfg.get("enabled", False):
            return None, {}
        store = goals_store if goals_store is not None else get_configured_store(
            root, getattr(config_obj, "config_path", None))
        return store, gcfg
    except Exception:
        logger.debug("goals store 不可用（care 联动静默）", exc_info=True)
        return None, {}


def _find_goal(store: Any, *, conversation_id: str, platform: str,
               account_id: str, chat_key: str) -> Optional[Dict[str, Any]]:
    try:
        return store.find_active_goal(
            conversation_id=str(conversation_id or ""),
            platform=str(platform or ""), chat_key=str(chat_key or ""),
            account_id=str(account_id or ""))
    except Exception:
        return None


# ── P0-2：拟稿目标背景块 ────────────────────────────────────────────────────

def care_goal_hint(
    config_obj: Any,
    *,
    conversation_id: str,
    platform: str = "",
    account_id: str = "",
    chat_key: str = "",
    now: Optional[float] = None,
    goals_store: Any = None,
) -> str:
    """该会话活跃 auto 档目标 → 一小段【工作目标背景】块；其余情况 ""。

    今日拍已被坐席驳回/受阻，或今日力度为 none（「今天只陪伴」）→ 返回
    显式的「只陪伴」指令而非目标方向——与 bridge 的拍语义一致，人的驳回
    在 care 出口同样生效。
    """
    try:
        if not bool(_care_cfg(config_obj).get("goal_hint", True)):
            return ""
        store, _gcfg = _goals_ready(config_obj, goals_store)
        if store is None:
            return ""
        goal = _find_goal(store, conversation_id=conversation_id,
                          platform=platform, account_id=account_id,
                          chat_key=chat_key)
        if goal is None:
            return ""
        if str(goal.get("autonomy") or "") != "auto":
            return ""
        title = str(goal.get("title") or "").strip()
        if not title:
            return ""
        intent = ""
        calm_only = False
        try:
            # 槽位键随节奏档走（P0 2026-08-30 修）：限时档的槽是小时/回合键，
            # 旧的 day_key（日历日）对 sprint 目标永远查不到拍 → intent 上不了
            # 关怀稿。找不到当前槽的拍就回落最近一行（与路由 _find_beat_action
            # 同哲学）。
            from src.companion.goals.pace import resolve_pace, slot_key
            gid = str(goal.get("goal_id") or "")
            action = store.get_action(
                gid, slot_key(resolve_pace(goal), now, 0.0))
            if action is None:
                rows = store.list_actions(gid, limit=4)
                action = rows[0] if rows else None
            if action is not None:
                st = str(action.get("status") or "")
                push = str(action.get("push_level") or "soft")
                if st in ("skipped", "blocked") or push == "none":
                    calm_only = True
                else:
                    intent = str(action.get("intent") or "").strip()
        except Exception:
            intent = ""
        if calm_only:
            return ("【工作目标背景】今天对这位客户只陪伴：营销/推进的内容"
                    "只字不提，专心关心对方。")
        lines = [f"你正在推进与对方相关的一个工作方向：「{title}」。"]
        if intent:
            lines.append(f"今日方向：{intent}。")
        lines.append("这条关怀以关心对方为先，只有话题自然贴近时才轻轻带到"
                     "上述方向；绝不硬销、不催促、不甩链接。")
        return "【工作目标背景】" + "".join(lines)
    except Exception:
        logger.debug("care_goal_hint 异常（返回空）", exc_info=True)
        return ""


# ── P1-1：捕获事件回流目标时间线 ────────────────────────────────────────────

def record_capture_event(
    config_obj: Any,
    *,
    conversation_id: str,
    platform: str = "",
    account_id: str = "",
    chat_key: str = "",
    text: str = "",
    count: int = 1,
    goals_store: Any = None,
) -> bool:
    """care 捕获到约定 → 写进该会话活跃目标的 ``goal_events``（任何 autonomy 档
    都写——事件只是给坐席看的情报，不是机器触达）。返回是否写入。"""
    try:
        care_cfg = _care_cfg(config_obj)
        link = dict(care_cfg.get("goal_link") or {})
        if not link.get("enabled", False):
            return False
        if not link.get("capture_events", True):
            return False
        store, _gcfg = _goals_ready(config_obj, goals_store)
        if store is None:
            return False
        goal = _find_goal(store, conversation_id=conversation_id,
                          platform=platform, account_id=account_id,
                          chat_key=chat_key)
        if goal is None:
            return False
        gid = str(goal.get("goal_id") or "")
        if not gid:
            return False
        snippet = " ".join(str(text or "").split())[:80]
        store.add_event(gid, "care_capture",
                        f"关怀捕获 {int(count)} 条约定：{snippet}")
        return True
    except Exception:
        logger.debug("record_capture_event 异常（忽略）", exc_info=True)
        return False


# ── P1-2：目标到期 → care 排期 ──────────────────────────────────────────────

def goal_care_topic_norm(goal_id: str) -> str:
    return f"{GOAL_CARE_NORM_PREFIX}{str(goal_id or '')[:56]}"


def scan_goal_deadlines(
    care_store: Any,
    config_obj: Any,
    *,
    now: Optional[float] = None,
    goals_store: Any = None,
) -> int:
    """扫 auto 档活跃目标：deadline 已进 ``days_before`` 窗（且未过期）→ 排一条
    目标推进关怀。返回新排期条数。幂等：``add_scheduled_care`` 的 30 天
    topic_norm 去重保证同一 deadline 只排一次（发过/跳过也不重排）。"""
    n = float(now if now is not None else time.time())
    care_cfg = _care_cfg(config_obj)
    link = dict(care_cfg.get("goal_link") or {})
    if not link.get("enabled", False):
        return 0
    store, _gcfg = _goals_ready(config_obj, goals_store)
    if store is None:
        return 0
    try:
        days_before = max(0.5, float(link.get("days_before", 3) or 3))
    except (TypeError, ValueError):
        days_before = 3.0
    try:
        goals = store.list_goals(status="active", limit=200) or []
    except Exception:
        return 0
    scheduled = 0
    for g in goals:
        try:
            if str(g.get("autonomy") or "") != "auto":
                continue
            # 冲刺档（today/session）归 sprint_ticker 全权（P0 2026-08-30）：
            # days_before=3 天对 2-12h 目标意味着建单即入窗、文案「剩余 0.1 天」
            # 也对不上刻度——双通道重复触达比漏发更糟，这里让路。
            try:
                from src.companion.goals.pace import is_sprint, resolve_pace
                if is_sprint(resolve_pace(g)):
                    continue
            except Exception:
                pass
            deadline = float(g.get("deadline_ts") or 0)
            if deadline <= 0:
                continue
            if deadline <= n:
                continue  # 已过期：结算归 goals 侧，不发「快到期」关怀
            lead_start = deadline - days_before * _DAY
            if n < lead_start:
                continue  # 还没进提前窗
            cid = str(g.get("conversation_id") or "")
            if not cid:
                continue
            title = str(g.get("title") or "").strip() or "这件事"
            # due：进窗当刻排（已在窗内 → 稍后即到期）；安静时段由派发层顺延
            due_at = max(lead_start, n + 300.0)
            rid = care_store.add_scheduled_care(
                contact_key=cid,
                platform=str(g.get("platform") or ""),
                account_id=str(g.get("account_id") or ""),
                chat_key=str(g.get("chat_key") or ""),
                due_at=due_at,
                event_at=deadline,
                topic=title[:40],
                topic_norm=goal_care_topic_norm(str(g.get("goal_id") or "")),
                source_text=f"工作目标「{title[:40]}」剩余 "
                            f"{max(0.0, (deadline - n) / _DAY):.1f} 天到期",
                confidence=1.0,
            )
            if rid:
                scheduled += 1
                try:
                    store.add_event(str(g.get("goal_id") or ""),
                                    "care_scheduled",
                                    f"到期前关怀已排期（care #{int(rid)}）")
                except Exception:
                    pass
        except Exception:
            logger.debug("scan_goal_deadlines 单目标异常（跳过）", exc_info=True)
    return scheduled


class CareGoalScanner:
    """后台循环：定期跑 ``scan_goal_deadlines``（常备接线 + 配置热闸，与
    care 派发器同哲学——``goal_link.enabled=false`` 时每 tick 空转零副作用）。"""

    def __init__(self, *, care_store: Any, config_obj: Any,
                 interval_sec: float = 1800.0, goals_store: Any = None) -> None:
        self._care_store = care_store
        self._config_obj = config_obj
        self._goals_store = goals_store
        self._interval = max(300.0, float(interval_sec))
        self.last_tick_ts: float = 0.0
        self.last_scheduled: int = 0
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def snapshot(self) -> dict:
        care_cfg = _care_cfg(self._config_obj)
        link = dict(care_cfg.get("goal_link") or {})
        return {
            "running": self.is_running(),
            "enabled": bool(link.get("enabled", False)),
            "interval_sec": self._interval,
            "last_tick_ts": self.last_tick_ts,
            "last_scheduled": self.last_scheduled,
        }

    def run_once(self, *, now: Optional[float] = None) -> int:
        self.last_tick_ts = float(now if now is not None else time.time())
        n = scan_goal_deadlines(self._care_store, self._config_obj, now=now,
                                goals_store=self._goals_store)
        self.last_scheduled = n
        if n:
            logger.info("[care_goal] 本轮排期 %d 条目标到期关怀", n)
        return n

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="care_goal_scanner")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    self.run_once()
                except Exception:
                    logger.exception("care_goal_scanner run_once 异常")
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(
                            self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("care_goal_scanner 退出")


__all__ = [
    "CareGoalScanner",
    "care_goal_hint",
    "goal_care_topic_norm",
    "record_capture_event",
    "scan_goal_deadlines",
]
