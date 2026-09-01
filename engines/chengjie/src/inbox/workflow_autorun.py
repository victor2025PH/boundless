# -*- coding: utf-8 -*-
"""工作链推进的常备宿主（P3 2026-08-09）。

**为什么存在**：链推进（到期步骤执行 + 条件自动启动）原先挂在
``ScheduledReporter`` tick 上，而那个调度器受 ``report.enabled`` 闸——生产
常年关 ⇒ **链推进从未运行过**（2026-08-09 只读探针实锤：4 条种子链在库、
执行记录为零；坐席「启动工作链」点了也不会走）。这与目标扫描循环是同一次
事故里发现的同一类病，修法同款（care 引擎「常备接线 + 配置热闸」哲学）：
循环无条件启动，每 tick 现读配置自闸，开关经 overlay 热重载免重启。

节奏与旧接线逐字一致：到期步骤每 tick（60s）处理、条件自动启动每 60 tick
（≈1h）扫一轮。``goal_event_hook``（链生命周期回写目标事件台账）与
contacts store 注入同样保留。

自闸三闸：``inbox.workflows.autorun``（本模块总闸，默认开——这是修复既有
功能的断线，不是新增行为；显式关=运营要冻结链推进）×
``inbox.workflows.enabled``（功能总闸，与路由同一口径）× store 在位。
全闸关闭时每 tick 只有 dict 读取，零 DB 开销。

``state`` 心跳快照（挂 ``app.state.workflow_autorun_state``，chain-funnel
API 捎带外露）——「没跑」和「没货」必须从外面分得出来，正是这次事故的教训。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

AUTO_START_EVERY_TICKS = 60     # ≈1h（与旧 ScheduledReporter 接线同节奏）


def autorun_enabled(cfg_root: Any) -> bool:
    """``inbox.workflows.autorun``（默认开）× ``inbox.workflows.enabled``（默认开）。"""
    try:
        if not isinstance(cfg_root, dict):
            return True
        wf = ((cfg_root.get("inbox") or {}).get("workflows") or {})
        return bool(wf.get("enabled", True)) and bool(wf.get("autorun", True))
    except Exception:
        return True


def workflow_tick(
    state: Dict[str, Any],
    app_state: Any,
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """单次 tick（循环体抽出供测试直驱）。绝不抛。"""
    n = float(now if now is not None else time.time())
    state["last_tick_ts"] = n
    state.setdefault("ticks", 0)
    state.setdefault("auto_start_tick", 0)
    # 口径：process_due_executions 返回「本 tick 处理的到期执行条数」（一条
    # 执行的多个零延迟步骤同 tick 排干也只计 1），不是步数——键名如实。
    state.setdefault("processed_total", 0)
    state.setdefault("auto_started_total", 0)
    state["ticks"] += 1
    try:
        store = getattr(app_state, "inbox_store", None)
        if store is None:
            state["gated"] = "no_store"
            return state
        cm = getattr(app_state, "config_manager", None)
        cfg_root = getattr(cm, "config", None) if cm is not None else None
        if not autorun_enabled(cfg_root):
            state["gated"] = "autorun_disabled"
            return state
        state["gated"] = ""
        from src.inbox.workflow_runner import WorkflowRunner
        contacts = getattr(getattr(app_state, "contacts", None), "store", None)
        goal_hook = None
        if cm is not None:
            try:
                from src.companion.goals.service import chain_event_recorder
                goal_hook = chain_event_recorder(cm)
            except Exception:
                goal_hook = None
        # P2 2026-08-13：链自动推进 hook（auto_advance 关=None=零开销旧行为）。
        # 每 tick 新建＝每 tick 拟稿预算自然复位；观测快照随心跳外露。
        auto_hook = None
        try:
            from src.inbox.workflow_auto_step import (
                make_auto_step_hook,
                stats_snapshot,
            )
            auto_hook = make_auto_step_hook(app_state, cfg_root)
            state["auto_step"] = stats_snapshot()
        except Exception:
            auto_hook = None
        runner = WorkflowRunner(
            store, contacts_store=contacts, goal_event_hook=goal_hook,
            auto_step_hook=auto_hook)
        # 实施92 P0-1：回复让路巡检**先于**步骤推进——同 tick 内客户已回的
        # 会话先暂停/完成，到期步不会抢在让路前发出。计数进心跳快照可观测。
        try:
            from src.inbox.workflow_reply_yield import sweep as _ry_sweep
            ry = _ry_sweep(store, cfg_root, now=n, goal_event_hook=goal_hook)
            if ry.get("paused") or ry.get("completed"):
                st_ry = state.setdefault(
                    "reply_yield", {"paused_total": 0, "completed_total": 0})
                st_ry["paused_total"] += int(ry.get("paused") or 0)
                st_ry["completed_total"] += int(ry.get("completed") or 0)
                st_ry["last_ts"] = n
        except Exception:
            logger.debug("reply_yield sweep failed", exc_info=True)
        # 实施92 P0-2：旅程阶段增量扫描（journey.enabled 关＝纯 dict 读取零开销）
        _journey_enabled = False
        try:
            from src.inbox.journey_stage import (
                resolve_journey_cfg, scan_and_update,
            )
            _journey_enabled = bool(resolve_journey_cfg(cfg_root)["enabled"])
            trans = scan_and_update(store, cfg_root, state, now=n)
            if trans:
                st_j = state.setdefault("journey", {"advanced_total": 0})
                st_j["advanced_total"] += len(trans)
                st_j["last_ts"] = n
        except Exception:
            logger.debug("journey scan failed", exc_info=True)
        processed = runner.process_due_executions()
        if processed:
            state["processed_total"] += int(processed)
            state["last_step_ts"] = n
            logger.info("[workflow-autorun] 处理 %d 条到期执行", processed)
        state["auto_start_tick"] += 1
        if state["auto_start_tick"] >= AUTO_START_EVERY_TICKS:
            state["auto_start_tick"] = 0
            # 每日自动开链预算（inbox.workflows.auto_start.max_per_day，默认 30；
            # 0=不限）——silence_days / stage_enter 两个触发面共用的总闸门
            try:
                _as = (((cfg_root or {}).get("inbox") or {})
                       .get("workflows") or {}).get("auto_start") or {}
                _budget = int(_as.get("max_per_day", 30))
            except Exception:
                _budget = 30
            started = runner.auto_start_chains(
                max_per_day=_budget, journey_enabled=_journey_enabled)
            if started:
                state["auto_started_total"] += int(started)
                logger.info("[workflow-autorun] 条件自动启动 %d 条工作链",
                            started)
    except Exception:
        logger.debug("workflow_tick failed", exc_info=True)
    return state


async def run_workflow_loop(
    app_state: Any,
    *,
    tick_sec: float = 60.0,
    state: Optional[Dict[str, Any]] = None,
) -> None:
    """常备循环（bootstrap 无条件启动）。任何单 tick 异常被吞，循环永不退出。"""
    import asyncio
    st = state if state is not None else {}
    logger.info("工作链推进循环已启动（tick=%ss，配置热自闸 inbox.workflows.autorun）",
                tick_sec)
    while True:
        try:
            workflow_tick(st, app_state)
        except Exception:
            logger.debug("workflow autorun loop tick failed", exc_info=True)
        await asyncio.sleep(max(5.0, float(tick_sec)))
