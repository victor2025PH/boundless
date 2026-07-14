"""主动触达节奏：亲密度 + 关系阶段双轴自适应（Phase14/15 纯函数）。

Phase14：亲密度越高 → 更短沉默/冷却阈值。
Phase15：funnel stage 叠加——warming/steady 即使 intimacy 低也可更早主动
（「聊了很多轮但亲密度分还没涨」不再被 8h 硬门槛卡住）。

组合口径（默认 ``blend=max``）：``pacing_score = max(intimacy, stage_score)`` 再线性插值。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# funnel stage → 0..100  pacing 分（与 intimacy 同量纲）
_STAGE_SCORE: Dict[str, float] = {
    "initial": 0.0,
    "warming": 28.0,
    "reunion": 32.0,
    "re_engagement": 32.0,
    "steady": 52.0,
    "intimate": 78.0,
    "close": 85.0,
    "lover": 90.0,
}


def _clamp_intimacy(intimacy: float) -> float:
    try:
        v = float(intimacy or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(100.0, v))


def stage_pacing_score(stage: str) -> float:
    """关系阶段映射到 pacing 分（未知阶段给保守中间值 12）。"""
    s = str(stage or "").strip().lower().replace("-", "_")
    if not s:
        return 0.0
    return float(_STAGE_SCORE.get(s, 12.0))


def combined_pacing_score(
    intimacy: float,
    stage: str = "",
    *,
    blend: str = "max",
) -> float:
    """双轴合成 pacing 分（0..100）。

    ``max``（默认）：取 intimacy 与 stage 分较高者——任一维度够熟就可更早问候。
    ``avg``：二者平均，更保守。
    """
    i = _clamp_intimacy(intimacy)
    ss = stage_pacing_score(stage)
    mode = str(blend or "max").strip().lower()
    if mode == "avg":
        return (i + ss) / 2.0
    return max(i, ss)


def _lerp_hours(score: float, at_0: float, at_100: float) -> float:
    t = _clamp_intimacy(score) / 100.0
    return float(at_0) + (float(at_100) - float(at_0)) * t


def parse_adaptive_pacing_cfg(
    proactive_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.adaptive_pacing`` 解析，缺省 enabled=false。"""
    pc = proactive_cfg or {}
    ap = pc.get("adaptive_pacing") if isinstance(pc.get("adaptive_pacing"), dict) else {}
    base_silent = float(pc.get("min_silent_hours", 24) or 24)
    base_cool = float(pc.get("cooldown_hours", 72) or 72)
    silent_blk = (ap.get("min_silent_hours") or {}) if isinstance(ap, dict) else {}
    cool_blk = (ap.get("cooldown_hours") or {}) if isinstance(ap, dict) else {}
    return {
        "enabled": bool((ap or {}).get("enabled", False)),
        "blend": str((ap or {}).get("blend") or "max").strip().lower(),
        "min_silent_base": float(silent_blk.get("base", base_silent) or base_silent),
        "min_silent_at_0": float(silent_blk.get("at_intimacy_0", base_silent * 2) or base_silent * 2),
        "min_silent_at_100": float(silent_blk.get("at_intimacy_100", max(1.0, base_silent * 0.5))
                              or max(1.0, base_silent * 0.5)),
        "cooldown_base": float(cool_blk.get("base", base_cool) or base_cool),
        "cooldown_at_0": float(cool_blk.get("at_intimacy_0", base_cool * 1.5) or base_cool * 1.5),
        "cooldown_at_100": float(cool_blk.get("at_intimacy_100", max(1.0, base_cool * 0.65))
                            or max(1.0, base_cool * 0.65)),
    }


def effective_min_silent_hours(
    intimacy: float,
    *,
    stage: str = "",
    base_hours: float,
    pacing_cfg: Optional[Dict[str, Any]] = None,
) -> float:
    """该会话生效的沉默阈值（小时）。"""
    p = pacing_cfg or {}
    if not p.get("enabled"):
        return max(0.0, float(base_hours or 0))
    score = combined_pacing_score(
        intimacy, stage, blend=str(p.get("blend") or "max"))
    return max(0.0, _lerp_hours(
        score,
        p.get("min_silent_at_0", float(base_hours) * 2),
        p.get("min_silent_at_100", max(1.0, float(base_hours) * 0.5)),
    ))


def effective_cooldown_hours(
    intimacy: float,
    *,
    stage: str = "",
    base_hours: float,
    pacing_cfg: Optional[Dict[str, Any]] = None,
) -> float:
    """该会话生效的主动冷却（小时）。"""
    p = pacing_cfg or {}
    if not p.get("enabled"):
        return max(0.0, float(base_hours or 0))
    score = combined_pacing_score(
        intimacy, stage, blend=str(p.get("blend") or "max"))
    return max(0.0, _lerp_hours(
        score,
        p.get("cooldown_at_0", float(base_hours) * 1.5),
        p.get("cooldown_at_100", max(1.0, float(base_hours) * 0.65)),
    ))


__all__ = [
    "stage_pacing_score",
    "combined_pacing_score",
    "parse_adaptive_pacing_cfg",
    "effective_min_silent_hours",
    "effective_cooldown_hours",
]
