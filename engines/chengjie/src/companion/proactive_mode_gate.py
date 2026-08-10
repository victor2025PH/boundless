"""gentle_checkin 效能门控（P5，2026-07-29，纯函数核心）。

问题：温和问候是「什么钩子都没有」时的兜底开场。若数据证明它的回复率远低于
富开场（记忆回访/生活分享/新鲜事/天气/剧情），继续按原频率发就是在烧账号信誉——
「没话找话」不如「今天不说」。本模块把这个判断交给数据：

    skip_prob = clamp(1 - checkin回复率/富开场回复率, 0, max_skip)

设计要点（与 media_feedback 同哲学——机制先上线，数据到位自动激活）：
- **只减发送不增**：门控只可能跳过 checkin，绝不多发一条；富开场不受影响。
- **样本不足不动作**：checkin 臂 ≥min_checkin_sent(30) 且富臂合计 ≥min_rich_sent(15)
  才启用——修复初期富开场还没流动时恒为 0，等待期就是 min 样本门槛本身，
  无需人回来拍参数。
- **有上限**：max_skip(默认 0.5) 封顶——数据再难看也保留一半探索量，
  checkin 永不彻底死（否则它的回复率永远无法被重新测量）。
- **确定性跳过**：crc32(会话#日期) 定夺——同会话同日恒定（15 分钟一 tick 的
  重掷会把「跳过」变成「延迟 15 分钟」，必须日级确定才是真降频），明日重掷。
- **富开场基线也不行时不迁怒**：富臂回复率为 0 → 无比较意义 → 不跳。
"""

from __future__ import annotations

import time
import zlib
from typing import Any, Dict, Optional, Tuple

# 富开场臂（有真实钩子的 mode；仪式/纪念日是时点驱动、ask_* 是采集意图，
# 回复动力学不同，均不入基线）。news_share（今日新鲜事，2026-08-03）＝
# 真实新闻钩子，与生活分享/天气同类。
RICH_MODES = (
    "follow_up", "life_share", "news_share", "weather_hook",
    "story_invite", "story_teaser",
)
CHECKIN_MODE = "gentle_checkin"


def parse_mode_gate_cfg(
    proactive_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.mode_gate`` 解析（缺省 enabled=false——
    新子系统默认关约定；生产 overlay 显式开）。"""
    pc = proactive_cfg or {}
    blk = pc.get("mode_gate") if isinstance(pc.get("mode_gate"), dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(blk.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(blk.get("enabled", False)),
        "min_checkin_sent": max(1, int(_f("min_checkin_sent", 30.0))),
        "min_rich_sent": max(1, int(_f("min_rich_sent", 15.0))),
        "max_skip": min(0.9, max(0.0, _f("max_skip", 0.5))),
        "lookback_days": max(1.0, _f("lookback_days", 14.0)),
        "response_window_days": max(0.5, _f("response_window_days", 3.0)),
    }


def checkin_skip_probability(
    checkin_rate: float, checkin_sent: int,
    rich_rate: float, rich_sent: int,
    cfg: Optional[Dict[str, Any]],
) -> Tuple[float, str]:
    """checkin 相对富开场的「本日跳过概率」（纯函数）：``(prob, reason)``。

    reason ∈ disabled / insufficient / no_signal / ok。
    """
    c = cfg or {}
    if not c.get("enabled"):
        return (0.0, "disabled")
    try:
        cs = int(checkin_sent or 0)
        rs = int(rich_sent or 0)
        cr = max(0.0, float(checkin_rate or 0.0))
        rr = max(0.0, float(rich_rate or 0.0))
    except (TypeError, ValueError):
        return (0.0, "insufficient")
    if cs < int(c.get("min_checkin_sent", 30) or 30):
        return (0.0, "insufficient")
    if rs < int(c.get("min_rich_sent", 15) or 15):
        return (0.0, "insufficient")
    if rr <= 0.0:
        return (0.0, "no_signal")  # 富开场也没人回：不迁怒 checkin
    cap = min(0.9, max(0.0, float(c.get("max_skip", 0.5) or 0.5)))
    skip = 1.0 - (cr / rr)
    return (min(cap, max(0.0, skip)), "ok")


def should_skip_checkin(
    conversation_id: str, skip_prob: float,
    now: Optional[float] = None,
) -> bool:
    """本日是否跳过该会话的 checkin（crc32(cid#日期) 确定性掷签）。

    同会话同日恒定——重掷会让 15min tick 把「跳过」磨成「延迟」；明日自动重掷。
    """
    try:
        p = float(skip_prob or 0.0)
    except (TypeError, ValueError):
        return False
    if p <= 0.0:
        return False
    if p >= 1.0:
        return True
    ts = now if now is not None else time.time()
    day = time.strftime("%Y%m%d", time.localtime(ts))
    seed = f"{conversation_id or ''}#checkin_gate#{day}".encode("utf-8", "ignore")
    return (zlib.crc32(seed) % 10000) < p * 10000


def collect_mode_gate(
    store: Any, cfg: Optional[Dict[str, Any]],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """从 outreach_log 汇总 checkin/富开场两臂并算跳过概率（引擎与看板同口径）。

    返回 ``{checkin: {sent, responded, rate}, rich: {...,+modes}, skip_prob,
    reason}``；store 不可用/异常 → 空 dict（调用方按不跳过处理）。
    """
    c = cfg or {}
    if store is None or not hasattr(store, "outreach_note_response_stats"):
        return {}
    try:
        rows = store.outreach_note_response_stats(
            "proactive_topic:",
            response_window_days=float(c.get("response_window_days", 3.0)),
            lookback_days=float(c.get("lookback_days", 14.0)),
            now=now,
        ) or {}
        ck = rows.get(CHECKIN_MODE) or {}
        checkin = {
            "sent": int(ck.get("sent") or 0),
            "responded": int(ck.get("responded") or 0),
        }
        checkin["rate"] = (
            round(checkin["responded"] / checkin["sent"], 4)
            if checkin["sent"] else 0.0)
        rich = {"sent": 0, "responded": 0, "modes": {}}
        for m in RICH_MODES:
            r = rows.get(m)
            if not r:
                continue
            rich["sent"] += int(r.get("sent") or 0)
            rich["responded"] += int(r.get("responded") or 0)
            rich["modes"][m] = int(r.get("sent") or 0)
        rich["rate"] = (
            round(rich["responded"] / rich["sent"], 4) if rich["sent"] else 0.0)
        prob, reason = checkin_skip_probability(
            checkin["rate"], checkin["sent"], rich["rate"], rich["sent"], c)
        return {
            "checkin": checkin, "rich": rich,
            "skip_prob": round(prob, 3), "reason": reason,
        }
    except Exception:
        return {}


__all__ = [
    "RICH_MODES",
    "CHECKIN_MODE",
    "parse_mode_gate_cfg",
    "checkin_skip_probability",
    "should_skip_checkin",
    "collect_mode_gate",
]
