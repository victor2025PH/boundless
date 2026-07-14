# -*- coding: utf-8 -*-
"""Facebook 加好友前置闸（P1-4 / P1-5）。

从 ``executor`` 抽出为独立模块，供：

* ``executor._execute_facebook`` — 执行前拦截
* ``routers.tasks.create_task_endpoint`` — **创建任务前**拦截，避免 pending 垃圾任务

逻辑与 ``add_friend_with_note`` 内闸一致，返回 human-readable 文案 + meta dict。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 与 executor._run_facebook_campaign 默认 steps 保持一致
FB_CAMPAIGN_DEFAULT_STEPS = (
    "warmup",
    "group_engage",
    "extract_members",
    "add_friends",
    "check_inbox",
)


def campaign_step_names(params: Dict[str, Any]) -> Tuple[str, ...]:
    """解析 ``facebook_campaign_run`` 的 ``params.steps`` 为规范 step 名元组。"""
    steps = params.get("steps")
    if steps is None:
        return FB_CAMPAIGN_DEFAULT_STEPS
    if isinstance(steps, str):
        return tuple(s.strip() for s in steps.replace(",", " ").split() if s.strip())
    if not isinstance(steps, (list, tuple)):
        return FB_CAMPAIGN_DEFAULT_STEPS
    out: list = []
    for item in steps:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
            continue
        if isinstance(item, dict):
            t = (item.get("type") or item.get("step") or "").strip()
            if t.startswith("facebook_"):
                t = t[len("facebook_") :]
            if t:
                out.append(t)
    return tuple(out) if out else FB_CAMPAIGN_DEFAULT_STEPS


def check_add_friend_gate(device_id: str, params: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """返回 ``(None, meta)`` 放行；``(err_msg, meta)`` 拒绝。

    * ``max_friends_per_run <= 0`` → phase 禁止加好友
    * ``daily_cap_per_account > 0`` 且 24h rolling ≥ cap → 拒绝
    * ``skip_add_friend_gate`` / ``force_add_friend`` → 不拦
    """
    meta: Dict[str, Any] = {"card_type": "fb_add_friend_gate"}
    try:
        from src.host.fb_playbook import local_rules_disabled
        if local_rules_disabled():
            meta["skipped"] = True
            meta["reason"] = "local_rules_disabled"
            return None, meta
    except Exception:
        pass
    if not device_id:
        meta["reason"] = "no_device_id"
        return None, meta
    if params.get("skip_add_friend_gate") or params.get("force_add_friend"):
        meta["skipped"] = True
        return None, meta
    try:
        phase = params.get("phase") or params.get("phase_override") or ""
        if not phase:
            from src.host.fb_account_phase import get_phase as _gp
            phase = (_gp(device_id) or {}).get("phase") or "cold_start"
        from src.host.fb_playbook import resolve_add_friend_params

        af = resolve_add_friend_params(phase=phase) or {}
        meta["phase"] = phase
        mx = int(af.get("max_friends_per_run") or 0)
        meta["max_friends_per_run"] = mx
        if mx <= 0:
            return (
                f"当前账号阶段 ({phase}) 禁止发送好友请求 (playbook max_friends_per_run=0)",
                meta,
            )
        cap = int(af.get("daily_cap_per_account") or 0)
        meta["daily_cap_per_account"] = cap
        if cap <= 0:
            meta["daily_gate"] = "disabled"
            return None, meta
        from src.host.fb_store import count_friend_requests_sent_since

        n24 = count_friend_requests_sent_since(device_id, hours=24)
        meta["friend_requests_24h"] = n24
        if n24 >= cap:
            return (
                f"加好友已达 rolling 24h 上限: 已发 {n24} 次 / 上限 {cap} (phase={phase})",
                meta,
            )
        return None, meta
    except Exception as e:
        logger.warning("[fb_add_friend_gate] 异常,放行: %s", e)
        meta["gate_error"] = str(e)
        return None, meta


# 2026-04-23: 打招呼(send_greeting)独立闸 ——
#   防止通过 facebook_send_greeting 绕开 add_friend 的 daily_cap 骚扰老朋友。
#   参数维度: playbook.send_greeting.max_greetings_per_run / daily_cap_per_account
def check_send_greeting_gate(device_id: str,
                             params: Dict[str, Any]
                             ) -> Tuple[Optional[str], Dict[str, Any]]:
    """返回 ``(None, meta)`` 放行；``(err_msg, meta)`` 拒绝。

    与 ``check_add_friend_gate`` 同构,但读 playbook.send_greeting 段 +
    count_outgoing_messages_since(ai_decision='greeting')。
    """
    meta: Dict[str, Any] = {"card_type": "fb_send_greeting_gate"}
    try:
        from src.host.fb_playbook import local_rules_disabled
        if local_rules_disabled():
            meta["skipped"] = True
            meta["reason"] = "local_rules_disabled"
            return None, meta
    except Exception:
        pass
    if not device_id:
        meta["reason"] = "no_device_id"
        return None, meta
    if params.get("skip_send_greeting_gate") or params.get("force_send_greeting"):
        meta["skipped"] = True
        return None, meta
    try:
        phase = params.get("phase") or params.get("phase_override") or ""
        if not phase:
            from src.host.fb_account_phase import get_phase as _gp
            phase = (_gp(device_id) or {}).get("phase") or "cold_start"
        from src.host.fb_playbook import resolve_send_greeting_params

        sg = resolve_send_greeting_params(phase=phase) or {}
        meta["phase"] = phase
        mx = int(sg.get("max_greetings_per_run") or 0)
        meta["max_greetings_per_run"] = mx
        if mx <= 0:
            return (
                f"当前账号阶段 ({phase}) 禁止打招呼 (playbook max_greetings_per_run=0)",
                meta,
            )
        cap = int(sg.get("daily_cap_per_account") or 0)
        meta["daily_cap_per_account"] = cap
        if cap <= 0:
            meta["daily_gate"] = "disabled"
            return None, meta
        from src.host.fb_store import count_outgoing_messages_since

        n24 = count_outgoing_messages_since(device_id, hours=24,
                                            ai_decision="greeting")
        meta["greetings_24h"] = n24
        if n24 >= cap:
            return (
                f"打招呼已达 rolling 24h 上限: 已发 {n24} 条 / 上限 {cap} (phase={phase})",
                meta,
            )
        return None, meta
    except Exception as e:
        logger.warning("[fb_send_greeting_gate] 异常,放行: %s", e)
        meta["gate_error"] = str(e)
        return None, meta
