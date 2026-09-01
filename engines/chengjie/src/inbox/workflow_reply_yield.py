# -*- coding: utf-8 -*-
"""实施92 P0-1：客户回复即让路（工作链停止规则）。

事故形态（本功能的存在理由）：链是纯时间驱动的跟进节奏表，客户已经开口、
坐席已经在聊，链还按固定时钟弹「第 3 天该发破冰第二步了」——提醒变噪音、
auto 档甚至会背着正在进行的真实对话再投一条营销话术。2026 年业界节奏工具
的共识是 engagement-triggered stopping rules：回复=退出或转入人工。

实现：随 workflow_autorun 每 tick（60s）巡检 running 执行——会话最后入站
时间晚于「链启动时刻 / 上次让路确认点」→ 按链的 ``on_reply`` 档位处置：

    pause     暂停（默认）：坐席接管，处理完可恢复（时钟停走语义已有）；
    complete  完成：链的使命就是等回复（唤回类），客户开口＝目标达成；
    continue  不让路：明确要按节奏走完的链（如成交后关怀回访）。

「让路确认点」``reply_yield_ack_ts``（context_json）：暂停时记下触发的
入站时间戳——坐席恢复后，同一条旧回复不会再次触发暂停；只有**更新**的
客户消息才会再让路。巡检先于 process_due_executions 跑（同 tick 内先让路
再推步，到期步不会抢在暂停前发出）。

配置 ``inbox.workflows.reply_yield``：
    enabled: true        # 护栏性质默认开（生产零执行=零行为变化风险）；
                         # 显式 false = 完全恢复旧「时钟不看人」行为
    default_action: pause

时序噪声防护：链启动后 5s 内的入站不触发（启动动作常由「客户刚说完话」
触发，那条消息不是对链的回应）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("pause", "complete", "continue")
# 启动时刻附近的入站宽限（秒）：启动往往因为客户刚来消息，别把它当「新回复」
_START_GRACE_SEC = 5.0


def resolve_reply_yield_cfg(cfg_root: Any) -> Dict[str, Any]:
    out = {"enabled": True, "default_action": "pause"}
    try:
        if not isinstance(cfg_root, dict):
            return out
        ry = (((cfg_root.get("inbox") or {}).get("workflows") or {})
              .get("reply_yield") or {})
        if not isinstance(ry, dict):
            return out
        out["enabled"] = bool(ry.get("enabled", True))
        da = str(ry.get("default_action") or "pause").strip().lower()
        out["default_action"] = da if da in _VALID_ACTIONS else "pause"
    except Exception:
        pass
    return out


def effective_action(chain_on_reply: str, default_action: str) -> str:
    """链档位（空=随全局默认）→ 生效动作。纯函数。"""
    a = str(chain_on_reply or "").strip().lower()
    if a in _VALID_ACTIONS:
        return a
    d = str(default_action or "pause").strip().lower()
    return d if d in _VALID_ACTIONS else "pause"


def _publish(evt: str, ex: Dict[str, Any], reason: str) -> None:
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish(evt, {
            "conversation_id": ex.get("conversation_id"),
            "exec_id": ex.get("exec_id"),
            "chain_id": ex.get("chain_id"),
            "chain_name": ex.get("chain_name", ""),
            "reason": reason,
            "step": int(ex.get("current_step") or 0),
            "ts": time.time(),
        })
    except Exception:
        logger.debug("reply_yield 事件发布失败", exc_info=True)


def sweep(
    store: Any, cfg_root: Any, *,
    now: Optional[float] = None,
    goal_event_hook: Any = None,
) -> Dict[str, int]:
    """单轮巡检。返回 {checked, paused, completed} 计数。绝不抛。"""
    stats = {"checked": 0, "paused": 0, "completed": 0}
    n = float(now if now is not None else time.time())
    cfg = resolve_reply_yield_cfg(cfg_root)
    if not cfg["enabled"] or store is None:
        return stats
    try:
        execs = store.list_chain_executions(status="running", limit=200)
    except Exception:
        return stats
    if not execs:
        return stats
    conv_ids = list({str(e.get("conversation_id") or "") for e in execs
                     if e.get("conversation_id")})
    try:
        in_map = store.last_inbound_ts_map(conv_ids)
    except Exception:
        return stats
    for ex in execs:
        cid = str(ex.get("conversation_id") or "")
        exec_id = str(ex.get("exec_id") or "")
        if not cid or not exec_id:
            continue
        stats["checked"] += 1
        last_in = float(in_map.get(cid, 0) or 0)
        if last_in <= 0:
            continue
        try:
            ctx = json.loads(ex.get("context_json") or "{}")
        except Exception:
            ctx = {}
        ack = float((ctx or {}).get("reply_yield_ack_ts") or 0)
        baseline = max(float(ex.get("started_at") or 0) + _START_GRACE_SEC, ack)
        if last_in <= baseline:
            continue
        action = effective_action(
            str(ex.get("chain_on_reply") or ""), cfg["default_action"])
        if action == "continue":
            continue
        try:
            if action == "complete":
                store.complete_workflow_execution(exec_id, status="completed")
                stats["completed"] += 1
                _publish("workflow_execution_completed", ex, "customer_reply")
                if goal_event_hook:
                    try:
                        goal_event_hook(cid, "chain_completed",
                                        str(ex.get("chain_name")
                                            or ex.get("chain_id") or ""))
                    except Exception:
                        pass
                logger.info("[reply-yield] 客户已回，链按完成收束: %s conv=%s",
                            ex.get("chain_id"), cid)
                continue
            # pause：状态守卫在 store 层（WHERE status='running'），并发安全
            if store.pause_workflow_execution(exec_id):
                store.patch_execution_context(
                    exec_id, {"reply_yield_ack_ts": last_in,
                              "reply_yield_at": n})
                stats["paused"] += 1
                _publish("workflow_execution_paused", ex, "customer_reply")
                logger.info("[reply-yield] 客户已回，链让路暂停: %s conv=%s",
                            ex.get("chain_id"), cid)
        except Exception:
            logger.debug("reply_yield 单条处置失败（已忽略）", exc_info=True)
    return stats
