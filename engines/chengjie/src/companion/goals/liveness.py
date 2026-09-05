"""D1b P0-5：自动推进「有货却 24h 零真发」判据（纯函数）。

进程心跳停走已由 ``_check_scan_loop_stall`` 盯 ticker/dispatcher。
本模块盯另一面：引擎配置说能发、库里有足够老的 auto 目标，但 24h 内
``care_sent`` / ``beat_sent`` 一条都没有——卡上仍可能写着「下一次跟进」，
实际一条都没出去（会话全是人审档 / 排拍后派发吞了 / 代码没装载）。

只减误报：引擎没开、目标太新、已经有真发，一律不算停摆。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

SENT_KINDS = ("care_sent", "beat_sent")


def collect_send_liveness(store: Any, *, now: float,
                          lookback_sec: float = 86400.0) -> Dict[str, Any]:
    """活跃 auto 目标盘点 + 近窗真发条数。读失败回空快照，绝不抛。"""
    out: Dict[str, Any] = {
        "active_auto": 0, "sent_24h": 0, "oldest_age_sec": 0.0,
    }
    try:
        goals = store.list_goals(status="active", limit=200) or []
    except Exception:
        return out
    sent = 0
    oldest = 0.0
    auto_n = 0
    for g in goals:
        if not isinstance(g, dict):
            continue
        if str(g.get("autonomy") or "") != "auto":
            continue
        auto_n += 1
        try:
            born = float(g.get("created_at") or g.get("start_ts") or 0)
        except (TypeError, ValueError):
            born = 0.0
        if born > 0:
            oldest = max(oldest, now - born)
        gid = str(g.get("goal_id") or "")
        if not gid:
            continue
        try:
            evs = store.list_events(gid, limit=40) or []
        except Exception:
            evs = []
        for ev in evs:
            if str((ev or {}).get("kind") or "") not in SENT_KINDS:
                continue
            try:
                ts = float((ev or {}).get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts >= now - lookback_sec:
                sent += 1
    out["active_auto"] = auto_n
    out["sent_24h"] = sent
    out["oldest_age_sec"] = oldest
    return out


def stall_verdict(
    engine: Any,
    snap: Any,
    *,
    min_active: int = 2,
    min_age_sec: float = 4 * 3600.0,
) -> Optional[str]:
    """``sprint_effective`` + 够老的 auto 目标 + 近窗零真发 → ``\"stalled\"``。"""
    if not isinstance(engine, dict) or not engine.get("sprint_effective"):
        return None
    if not isinstance(snap, dict):
        return None
    try:
        if int(snap.get("active_auto") or 0) < int(min_active):
            return None
        if float(snap.get("oldest_age_sec") or 0) < float(min_age_sec):
            return None
        if int(snap.get("sent_24h") or 0) > 0:
            return None
    except (TypeError, ValueError):
        return None
    return "stalled"


__all__ = ["SENT_KINDS", "collect_send_liveness", "stall_verdict"]
