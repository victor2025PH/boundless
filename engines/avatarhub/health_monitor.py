# -*- coding: utf-8 -*-
"""服务健康自愈监控：跟踪子服务 up/down 跳变、连续失败、可用率，并在关键服务恢复时触发回暖回调。"""
from __future__ import annotations

import time
from collections import deque
from typing import Iterable

# 关键服务【静态默认】：仅在上层未注入时兜底（掉线影响对话主链路 TTS/STT/口型推流）。
CRITICAL = {"fish_tts", "stt", "lipsync"}

# 关键服务集合可被上层按「开播模式」注入覆盖（见 avatar_hub._observe_health → broadcast.core）：
# 未注入(None) → 回落上面的静态默认，保持向后兼容；注入空集也视为"未指定"回落默认。
_critical_override = None            # type: set | None


def set_critical(names) -> None:
    """由上层注入当前关键服务集合（模式感知的 broadcast.core）。传 None/空 → 回落静态默认。"""
    global _critical_override
    _critical_override = set(names) if names else None


def _is_critical(name: str) -> bool:
    """当前该服务是否"关键"：优先注入集合，否则静态默认。"""
    return name in (_critical_override if _critical_override is not None else CRITICAL)


_state: dict[str, dict] = {}        # name -> {up, fail_streak, last_change, last_seen_up, ...}
_events: deque = deque(maxlen=120)  # 最近跳变事件
_probe_history: dict[str, deque] = {}  # name -> deque[(ts, up)] 滚动窗口算可用率
_HIST_MAX = 120
_started_at = time.time()


def _hist(name: str) -> deque:
    if name not in _probe_history:
        _probe_history[name] = deque(maxlen=_HIST_MAX)
    return _probe_history[name]


def observe(status: dict[str, bool]) -> list[dict]:
    """喂入一次 health 探测结果；返回本次发生的跳变事件（供调用方触发回暖）。"""
    now = time.time()
    transitions: list[dict] = []
    for name, up in status.items():
        st = _state.get(name)
        if st is None:
            st = {
                "up": up, "fail_streak": 0 if up else 1,
                "last_change": now, "last_seen_up": now if up else 0.0,
                "down_count": 0 if up else 1, "recover_count": 0,
                "critical": _is_critical(name),
            }
            _state[name] = st
            _hist(name).append((now, up))
            if not up:
                ev = {"ts": now, "service": name, "event": "down",
                      "critical": _is_critical(name)}
                _events.append(ev)
                transitions.append(ev)
            continue

        st["critical"] = _is_critical(name)    # 每轮按当前模式刷新关键性（跟随注入的 broadcast.core）
        if up:
            st["last_seen_up"] = now
            if not st["up"]:                       # down -> up 恢复
                st["up"] = True
                st["last_change"] = now
                st["fail_streak"] = 0
                st["recover_count"] += 1
                ev = {"ts": now, "service": name, "event": "recover",
                      "critical": _is_critical(name)}
                _events.append(ev)
                transitions.append(ev)
            else:
                st["fail_streak"] = 0
        else:
            st["fail_streak"] += 1
            if st["up"]:                           # up -> down 掉线
                st["up"] = False
                st["last_change"] = now
                st["down_count"] += 1
                ev = {"ts": now, "service": name, "event": "down",
                      "critical": _is_critical(name)}
                _events.append(ev)
                transitions.append(ev)
        _hist(name).append((now, up))
    return transitions


def _uptime_ratio(name: str) -> float:
    h = _probe_history.get(name)
    if not h:
        return 1.0
    ups = sum(1 for _, u in h if u)
    return round(ups / len(h), 3)


def alerts() -> list[dict]:
    """掉线/抖动告警：关键服务 down，或非关键服务连续失败。"""
    out: list[dict] = []
    now = time.time()
    for name, st in _state.items():
        if not st["up"]:
            # 非关键服务若【本会话从未在线】= 可选扩展未启动(预期状态)，不产生告警噪声；
            # 关键服务、或曾在线后掉线(真回归/崩溃)仍照常告警。
            if not _is_critical(name) and st.get("last_seen_up", 0) <= 0:
                continue
            down_for = int(now - st["last_change"])
            out.append({
                "service": name,
                "severity": "critical" if _is_critical(name) else "warning",
                "down_for_sec": down_for,
                "fail_streak": st["fail_streak"],
                "uptime_ratio": _uptime_ratio(name),
                "reason": f"{name} 已掉线 {down_for}s（连续失败 {st['fail_streak']} 次）",
            })
        elif _uptime_ratio(name) < 0.8 and len(_probe_history.get(name, [])) >= 10:
            out.append({
                "service": name,
                "severity": "warning",
                "down_for_sec": 0,
                "fail_streak": st["fail_streak"],
                "uptime_ratio": _uptime_ratio(name),
                "reason": f"{name} 近窗口可用率 {_uptime_ratio(name)*100:.0f}%（抖动）",
            })
    out.sort(key=lambda a: (a["severity"] != "critical", a["service"]))
    return out


def snapshot() -> dict:
    services = {}
    for name, st in _state.items():
        services[name] = {
            "up": st["up"],
            "critical": _is_critical(name),
            "fail_streak": st["fail_streak"],
            "down_count": st["down_count"],
            "recover_count": st["recover_count"],
            "uptime_ratio": _uptime_ratio(name),
            "last_change_ago_sec": int(time.time() - st["last_change"]),
        }
    al = alerts()
    return {
        "ok": True,
        "monitor_uptime_sec": int(time.time() - _started_at),
        "services": services,
        "alerts": al,
        "critical_down": [a["service"] for a in al if a["severity"] == "critical"],
        "recent_events": list(_events)[-20:][::-1],
    }
