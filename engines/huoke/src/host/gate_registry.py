# -*- coding: utf-8 -*-
"""Gate 注册表（2026-04-23 Phase 3 P3-2）。

动机
----
之前 ``routers/tasks.py`` 和 ``executor.py`` 里 gate 调用散装 if-elif:

    if task_type == "facebook_add_friend": check_add_friend_gate(...)
    if task_type == "facebook_add_friend_and_greet": check_add_friend_gate(...)
    if task_type == "facebook_send_greeting": check_send_greeting_gate(...)

每加一个新 task_type 就要改 3 处:
  * ``routers/tasks.py::create_task_endpoint`` 的 if 链
  * ``routers/tasks.py`` 里 campaign step 分支
  * ``executor.py`` 里 _execute_facebook 执行前分支

对于 **机器 B 后续加自己的 task_type** (比如 check_inbox 可能需要 reply gate),
这种散装结构会导致双方改同一文件爆冲突。

本模块抽象 gate 逻辑为:
  * 按 **task_type** 查 gate 函数
  * 按 **campaign step name** 查 gate 函数
  * 统一入口 ``check_gate_for_task`` / ``check_gates_for_campaign_steps``

双方只需要 **注册自己的 gate**, 不改 tasks.py 的调用骨架。

使用示例
--------
注册 (A 或 B 在自己模块的模块级调一次)::

    from src.host.gate_registry import register_task_gate, register_campaign_step_gate
    from src.host.fb_add_friend_gate import check_add_friend_gate

    register_task_gate("facebook_add_friend", check_add_friend_gate)
    register_task_gate("facebook_add_friend_and_greet", check_add_friend_gate)
    register_campaign_step_gate("add_friends", check_add_friend_gate)

调用 (routers/tasks.py 创建任务前)::

    from src.host.gate_registry import check_gate_for_task
    err, meta = check_gate_for_task(task_type, device_id, params)
    if err:
        raise HTTPException(400, {"error": err, "meta": meta})
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# gate 函数签名: (device_id, params) -> (err_msg_or_None, meta_dict)
GateFn = Callable[[str, Dict[str, Any]], Tuple[Optional[str], Dict[str, Any]]]

_TASK_GATES: Dict[str, GateFn] = {}
_CAMPAIGN_STEP_GATES: Dict[str, GateFn] = {}


def register_task_gate(task_type: str, gate_fn: GateFn) -> None:
    """注册 task_type -> gate 函数映射。

    同一 task_type 允许**覆盖注册**（运行时热替换场景），但会 warn log,
    避免测试里误重复注册没察觉。
    """
    if task_type in _TASK_GATES:
        logger.warning("[gate_registry] task=%s gate 被覆盖,前一个被替换", task_type)
    _TASK_GATES[task_type] = gate_fn


def register_campaign_step_gate(step_name: str, gate_fn: GateFn) -> None:
    """注册 campaign step name -> gate 函数映射。

    step_name 是 ``_run_facebook_campaign`` 的 step 字符串(如 "add_friends" /
    "send_greeting" / "check_inbox")。
    """
    if step_name in _CAMPAIGN_STEP_GATES:
        logger.warning("[gate_registry] step=%s gate 被覆盖", step_name)
    _CAMPAIGN_STEP_GATES[step_name] = gate_fn


def check_gate_for_task(task_type: str, device_id: str,
                        params: Dict[str, Any]
                        ) -> Tuple[Optional[str], Dict[str, Any]]:
    """按 task_type 查 gate 并执行。未注册则放行(保持向后兼容)。"""
    gate_fn = _TASK_GATES.get(task_type)
    if not gate_fn:
        return None, {"gate": "not_registered", "task_type": task_type}
    if not device_id:
        return None, {"gate": "no_device_id"}
    try:
        return gate_fn(device_id, params)
    except Exception as e:
        logger.warning("[gate_registry] gate 异常放行 task=%s: %s", task_type, e)
        return None, {"gate_error": str(e), "task_type": task_type}


def check_gates_for_campaign_steps(step_names, device_id: str,
                                   params: Dict[str, Any]
                                   ) -> Tuple[Optional[str], Dict[str, Any]]:
    """按 campaign steps 序列查 gate, 任何一个拒绝就整体拒绝。

    step_names 可以是 list/tuple/set (``campaign_step_names`` 的返回)。
    """
    if not device_id:
        return None, {"gate": "no_device_id"}
    checked: Dict[str, Any] = {}
    for name in step_names or ():
        gate_fn = _CAMPAIGN_STEP_GATES.get(name)
        if not gate_fn:
            continue
        try:
            err, meta = gate_fn(device_id, params)
        except Exception as e:
            logger.warning("[gate_registry] campaign step=%s gate 异常放行: %s",
                           name, e)
            checked[name] = {"gate_error": str(e)}
            continue
        checked[name] = meta
        if err:
            return err, {"failed_step": name, "checked": checked}
    return None, {"checked": checked}


def registered_task_types() -> list:
    """调试/诊断用 —— 返回所有已注册 gate 的 task_type 列表。"""
    return sorted(_TASK_GATES.keys())


def registered_campaign_steps() -> list:
    return sorted(_CAMPAIGN_STEP_GATES.keys())


def _reset_for_tests() -> None:
    """测试专用 —— 清空所有注册, 避免跨测试污染。"""
    _TASK_GATES.clear()
    _CAMPAIGN_STEP_GATES.clear()


# ─── 模块加载时自动注册 —— A 的 gate ─────────────────────────────────────
# 注意: A 负责的 gate 在这里注册; B 在自己的新模块里 append 注册即可,
# 不必改本文件。建议 B 写一个 ``src/host/gate_registry_b.py`` (或就在
# check_inbox_gate.py 末尾) 调 register_*_gate 一次。

def _register_default_gates() -> None:
    """模块加载时自动把 A 已有的 gate 挂上去。"""
    try:
        from src.host.fb_add_friend_gate import (check_add_friend_gate,
                                                  check_send_greeting_gate)
    except Exception as e:
        logger.warning("[gate_registry] 加载默认 gate 失败: %s", e)
        return
    # 加好友系 task 共用 add_friend gate
    register_task_gate("facebook_add_friend", check_add_friend_gate)
    register_task_gate("facebook_add_friend_and_greet", check_add_friend_gate)
    # 独立打招呼任务
    register_task_gate("facebook_send_greeting", check_send_greeting_gate)
    # campaign steps
    register_campaign_step_gate("add_friends", check_add_friend_gate)
    register_campaign_step_gate("send_greeting", check_send_greeting_gate)


_register_default_gates()
