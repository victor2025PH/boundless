# -*- coding: utf-8 -*-
"""小智「Windows 操控 runner」信任档（实施91 P2-C，2026-08-31）。

信任档 = 某台受控机在**计时窗口**内被标为 trusted → 小智对它的 L2 动作**免确认
卡**直接执行（真正的隔空自动驾驶）。但这只解掉「小智侧确认」这一层，**其余安全
地板全在**：
- ``always_confirm`` 动作（run_command）**永不免确认**（判定在 actions.runner_
  auto_execute + 路由，本模块不涉及）；
- runner 侧硬闸（kill-switch / PC_RUNNER_ACTIONS / PC_RUNNER_SHELL / 白名单）
  与信任档正交，照常拦；
- 屏幕/输出消毒、审计（审计行带 trusted 标记）照常。

时间窗是核心安全属性：受信是**会计时自动过期**的（默认 30min），不是永久开关；
过期即自清。运行时 revoke 立即收回。进程内存储 + lock，重启回落"全不受信"
（最保守）。与 runner_pairing（机器白名单/踢下线）正交、单独一模块便于测试。

门禁 ``tests/test_pc_trust.py``。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List

_LOCK = threading.Lock()
# machine_id -> 受信到期时间戳（epoch 秒）。过期即视为不受信、惰性清除。
_TRUST: Dict[str, float] = {}


def grant(machine: str, ttl_sec: float) -> float:
    """授予某机受信 ttl_sec 秒；返回到期时间戳（0=非法输入未授予）。

    ttl 由调用方（路由）按 max_ttl 夹紧——本函数只做最小健壮性（>=1）。
    重复授予=续期（覆盖到期时间）。"""
    mid = str(machine or "").strip()
    try:
        ttl = int(ttl_sec)
    except Exception:
        ttl = 0
    if not mid or ttl < 1:
        return 0.0
    exp = time.time() + ttl
    with _LOCK:
        _TRUST[mid] = exp
    return exp


def is_trusted(machine: str) -> bool:
    """当前是否受信（未过期）。过期则惰性自清、返回 False。"""
    mid = str(machine or "").strip()
    if not mid:
        return False
    now = time.time()
    with _LOCK:
        exp = _TRUST.get(mid, 0.0)
        if exp <= now:
            _TRUST.pop(mid, None)
            return False
        return True


def expires_at(machine: str) -> float:
    """受信到期时间戳；不受信/已过期=0.0。"""
    mid = str(machine or "").strip()
    now = time.time()
    with _LOCK:
        exp = _TRUST.get(mid, 0.0)
        return exp if exp > now else 0.0


def revoke(machine: str) -> bool:
    """立即收回受信（自动驾驶急停的一部分）。返回是否原本受信。"""
    mid = str(machine or "").strip()
    with _LOCK:
        return _TRUST.pop(mid, None) is not None


def list_trusted() -> List[Dict[str, float]]:
    """面板用：当前受信机 + 到期（已过期的不列、顺带清）。"""
    now = time.time()
    out: List[Dict[str, float]] = []
    with _LOCK:
        for mid in list(_TRUST):
            exp = _TRUST[mid]
            if exp <= now:
                _TRUST.pop(mid, None)
                continue
            out.append({"machine": mid, "expires": round(exp, 1)})
    out.sort(key=lambda r: str(r["machine"]))
    return out


def _reset_for_test() -> None:
    with _LOCK:
        _TRUST.clear()
