"""计划维护预告状态（重启前广播的单一事实源，2026-07-31「连接中断」横幅根因治理）。

链路：``restart_instance.ps1`` 在停机**之前** POST
``/api/internal/ops/maintenance-notice``（unified_inbox_realtime_routes）→
本模块登记窗口 + 路由经 EventBus 广播 ``maintenance_notice`` SSE 事件 →
已打开的工作台立刻把接下来的连接失败标注为蓝色「服务维护窗口」并降速轮询，
而不是红色「连接中断」（旧行为：冷却状态在重启**之后**才写入、断站期间
status 接口又不可达 → 坐席永远只能看到吓人的红条）。

``seat_restart_banner`` 同时把活动窗口折进 ``quiet_poll``——宣告与真正停机
之间若恰好有 ai-runtime-status 轮询（60s 周期），读到的是**确认**而非清除
（否则轮询会把 SSE 刚置上的 quiet 标志冲掉，横幅退回红色）。

状态**刻意进程内**：它描述「本进程即将停机」；重启后新进程从干净状态起步，
预告绝不可能比它宣告的停机窗口活得更久。恢复后的静默语义由机器级冷却记录
（cooldown JSON）接棒，两者互不依赖。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

# 窗口上下限：下限防「刚宣告就过期」的无效广播；上限防手滑把工作台钉在维护态
# 半天（真实重启窗口 SLA 180s、重型冷启动尾部实测 ~3.5min，900s 已留足余量）。
MIN_WINDOW_SEC = 10
MAX_WINDOW_SEC = 900
DEFAULT_WINDOW_SEC = 240

_lock = threading.Lock()
_state: Dict[str, Any] = {"until": 0.0, "reason": "", "set_ts": 0.0}


def clamp_window_sec(raw: Any, *, default: int = DEFAULT_WINDOW_SEC) -> int:
    """把外部传入的 window_sec 收进 [MIN, MAX]；不可解析回落 default。"""
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(MIN_WINDOW_SEC, min(MAX_WINDOW_SEC, val))


def set_notice(
    window_sec: Any, *, reason: str = "", now: Optional[float] = None
) -> Dict[str, Any]:
    """登记维护预告并返回登记后的快照。重复宣告 = 刷新窗口（幂等语义）。"""
    now = time.time() if now is None else float(now)
    window = clamp_window_sec(window_sec)
    with _lock:
        _state["until"] = now + window
        _state["reason"] = str(reason or "").strip()[:200]
        _state["set_ts"] = now
    return snapshot(now=now)


def clear_notice() -> None:
    """清除预告（测试隔离用；生产靠进程重启/窗口过期自然消亡）。"""
    with _lock:
        _state["until"] = 0.0
        _state["reason"] = ""
        _state["set_ts"] = 0.0


def snapshot(now: Optional[float] = None) -> Dict[str, Any]:
    """当前预告快照；过期后各字段一律归零（消费方无需自行判窗）。"""
    now = time.time() if now is None else float(now)
    with _lock:
        until = float(_state["until"] or 0.0)
        reason = str(_state["reason"] or "")
        set_ts = float(_state["set_ts"] or 0.0)
    left = max(0.0, until - now)
    active = left > 0
    return {
        "active": active,
        "left_sec": int(left),
        "until_ts": until if active else 0,
        "reason": reason if active else "",
        "set_ts": set_ts if active else 0,
    }
