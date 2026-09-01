"""相册挑图门禁的进程级计数（实施90 阶段二）——「为什么没发」从翻日志变成看板。

数据源＝``persona_media.select_media`` 的 trace 出参（纯函数填、零 IO），
``pick_media`` 每次调用后 best-effort 喂进来。**进程口径**（重启清零，与
avatar_voice_stats/frontend_error_stats 同族）；持久口径若以后需要再落库，
先看这层读数值不值。

消费面：``/api/workspace/metrics.persona_media.gates`` + Prometheus
``ws_persona_media_gate_*`` + ops-overview「🖼️ 人设相册」卡拦截行。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

# 拒发原因（select_media 返回 None 时归到哪道门）
REFUSAL_REASONS = ("no_pool", "cooldown", "scene", "season", "place",
                   "strict_no_resend")
# 逐门剔除计数（没拒发也可能剔过条目）
CUT_GATES = ("family", "cooldown", "scene", "season", "place", "tod")

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "picks": 0,
    "refused": 0,
    "refused_by": {k: 0 for k in REFUSAL_REASONS},
    "cut": {k: 0 for k in CUT_GATES},
    "tod_softened": 0,
    "last_ts": 0.0,
}


def record(trace: Optional[Dict[str, Any]], picked: bool) -> None:
    """吃一次挑图 trace（None/坏结构安全忽略）。绝不抛。"""
    try:
        t = trace if isinstance(trace, dict) else {}
        with _LOCK:
            if picked:
                _STATS["picks"] += 1
            else:
                _STATS["refused"] += 1
                reason = str(t.get("refused") or "no_pool")
                if reason not in REFUSAL_REASONS:
                    reason = "no_pool"
                _STATS["refused_by"][reason] += 1
            cut = t.get("cut") if isinstance(t.get("cut"), dict) else {}
            for k in CUT_GATES:
                try:
                    n = int(cut.get(k) or 0)
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    _STATS["cut"][k] += n
            if t.get("tod_softened"):
                _STATS["tod_softened"] += 1
            _STATS["last_ts"] = time.time()
    except Exception:
        pass


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        out = {
            "picks": _STATS["picks"],
            "refused": _STATS["refused"],
            "refused_by": dict(_STATS["refused_by"]),
            "cut": dict(_STATS["cut"]),
            "tod_softened": _STATS["tod_softened"],
            "last_ts": _STATS["last_ts"],
        }
    out["active"] = bool(out["picks"] or out["refused"])
    return out


def reset_for_tests() -> None:
    with _LOCK:
        _STATS["picks"] = 0
        _STATS["refused"] = 0
        _STATS["refused_by"] = {k: 0 for k in REFUSAL_REASONS}
        _STATS["cut"] = {k: 0 for k in CUT_GATES}
        _STATS["tod_softened"] = 0
        _STATS["last_ts"] = 0.0


__all__ = ["REFUSAL_REASONS", "CUT_GATES", "record", "snapshot",
           "reset_for_tests"]
