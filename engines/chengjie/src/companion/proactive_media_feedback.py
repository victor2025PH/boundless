"""主动触达媒体形态反哺（P3，2026-07-29，纯函数核心）。

问题：voice/photo 的发送概率是拍脑袋配置（50%/25%），而 outreach_log 里已经躺着
分形态回复率（kind_ab：photo/voice/text 触达后 72h 是否有入站）——数据一直在，
却从没反哺过形态选择。本模块把「哪种形态对这批用户真有效」交给数据：

    factor = clamp(sqrt(kind_rate / text_rate), max_cut, max_boost)
    有效概率 = clamp(配置概率 × factor, 0.02, 0.95)

设计要点：
- **只换形态不换总量**：调整的是"这条消息发语音还是发文字"的概率，消息总数由
  pacing/退避层决定，本模块绝不多发一条。
- **以 text 为基线**：语音/照片各与文本比。sqrt 软化噪声（回复率翻倍 → 概率只
  ×1.41），上下限夹紧（默认 [0.5, 1.5]）防小样本震荡。
- **双臂都要够样本**（默认各 ≥8 条）才动，否则 factor=1.0 走配置值——冷启动期
  照常按配置概率探索攒样本，无死锁。
- **已知偏差，如实面对**：photo 只发给 intimacy≥20 的熟客（回复率天然偏高），
  boost 后仍要过 min_intimacy 闸——形态反哺只动概率，其他护栏全不动。
- **新鲜度**：只看近 lookback_days（默认 14 天）发出的触达，三个月前的回复习惯
  不决定今天。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

# 反哺形态臂：text 是基线，voice/photo 是被调节的臂
FEEDBACK_KINDS = ("voice", "photo")
BASELINE_KIND = "text"

# 有效概率硬边界：下限保探索（形态永不彻底死掉，坏了也能被数据发现恢复），
# 上限防「每条都发语音」的机械感。
_PROB_FLOOR = 0.02
_PROB_CEIL = 0.95


def parse_media_feedback_cfg(
    proactive_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.media_feedback`` 解析（缺省 enabled=false——
    新子系统默认关约定；生产 overlay 显式开）。"""
    pc = proactive_cfg or {}
    blk = pc.get("media_feedback") if isinstance(
        pc.get("media_feedback"), dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(blk.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(blk.get("enabled", False)),
        "min_sent_per_arm": max(1, int(_f("min_sent_per_arm", 8.0))),
        "lookback_days": max(1.0, _f("lookback_days", 14.0)),
        "response_window_days": max(0.5, _f("response_window_days", 3.0)),
        "max_boost": max(1.0, _f("max_boost", 1.5)),
        "max_cut": min(1.0, max(0.1, _f("max_cut", 0.5))),
    }


def media_probability_factor(
    kind_rate: float, kind_sent: int,
    text_rate: float, text_sent: int,
    cfg: Optional[Dict[str, Any]],
) -> Tuple[float, str]:
    """某媒体形态相对文本基线的概率倍率（纯函数）：``(factor, reason)``。

    reason ∈ disabled / insufficient / ok。样本不足/未启用一律 1.0（走配置值）。
    """
    c = cfg or {}
    if not c.get("enabled"):
        return (1.0, "disabled")
    try:
        ks = int(kind_sent or 0)
        ts_n = int(text_sent or 0)
        kr = max(0.0, float(kind_rate or 0.0))
        tr = max(0.0, float(text_rate or 0.0))
    except (TypeError, ValueError):
        return (1.0, "insufficient")
    min_n = int(c.get("min_sent_per_arm", 8) or 8)
    if ks < min_n or ts_n < min_n:
        return (1.0, "insufficient")
    boost = max(1.0, float(c.get("max_boost", 1.5) or 1.5))
    cut = min(1.0, max(0.1, float(c.get("max_cut", 0.5) or 0.5)))
    if tr <= 0.0:
        # 文本基线全无回应：该形态有回应 → 顶格加权；同样没有 → 无信号不动
        return (boost, "ok") if kr > 0.0 else (1.0, "ok")
    factor = math.sqrt(kr / tr)
    return (min(boost, max(cut, factor)), "ok")


def effective_media_probability(base_prob: float, factor: float) -> float:
    """配置概率 × 反哺倍率，夹在 [0.02, 0.95]（保探索、防机械）。"""
    try:
        p = float(base_prob or 0.0) * float(factor or 1.0)
    except (TypeError, ValueError):
        return float(base_prob or 0.0)
    return min(_PROB_CEIL, max(_PROB_FLOOR, p))


def collect_media_feedback(
    store: Any, cfg: Optional[Dict[str, Any]],
    now: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """从 outreach_log 汇总三臂回复率并算倍率（编排器缓存与看板共用同一口径）。

    返回 ``{kind: {sent, rate, factor, reason}}``（text 恒 factor=1.0 作基线行）。
    store 不可用/查询异常 → 空 dict（调用方按 factor=1.0 处理）。
    """
    c = cfg or {}
    if store is None or not hasattr(store, "outreach_response_stats"):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        arms: Dict[str, Dict[str, Any]] = {}
        for kind in (BASELINE_KIND,) + FEEDBACK_KINDS:
            r = store.outreach_response_stats(
                f"proactive_topic:{kind}",
                response_window_days=float(c.get("response_window_days", 3.0)),
                lookback_days=float(c.get("lookback_days", 14.0)),
                now=now,
            ) or {}
            arms[kind] = {
                "sent": int(r.get("sent") or 0),
                "rate": float(r.get("response_rate") or 0.0),
            }
        base = arms[BASELINE_KIND]
        out[BASELINE_KIND] = dict(base, factor=1.0, reason="baseline")
        for kind in FEEDBACK_KINDS:
            arm = arms[kind]
            factor, reason = media_probability_factor(
                arm["rate"], arm["sent"], base["rate"], base["sent"], c)
            out[kind] = dict(arm, factor=round(factor, 3), reason=reason)
    except Exception:
        return {}
    return out


__all__ = [
    "FEEDBACK_KINDS",
    "BASELINE_KIND",
    "parse_media_feedback_cfg",
    "media_probability_factor",
    "effective_media_probability",
    "collect_media_feedback",
]
