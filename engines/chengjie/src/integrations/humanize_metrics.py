"""拟人回执（已读 / 打字）按平台观测指标（进程内累计，线程安全）。

编排器的 ``mark_read`` / ``send_chat_action`` 是**全平台已读/打字的唯一派发点**，在那里
记一笔即覆盖所有调用方（autosend / L3 缓冲话术 / 未来路径），无需逐调用方埋点。用于回答
排障时最常问的「WhatsApp/Telegram 的已读到底有没有在发、成功率多少」——此前只能翻日志。

结构（每平台一组计数）：
    {platform: {"read_ok", "read_fail", "typing_ok", "typing_fail"}}
``read_ok`` = worker 确实标了已读（有未读可标）；``read_fail`` = worker 不支持/返回 False/异常。
经 /api/drafts/autosend-status 的 ``humanize`` 段暴露。
"""
from __future__ import annotations

import threading
from typing import Any, Dict

_LOCK = threading.Lock()
_METRICS: Dict[str, Dict[str, int]] = {}

# 拟人延迟（pacing）按路径累计：autosend / native_tg / …。用于校准 per_char_sec / max_sec /
# arousal swing——把「拍脑袋的节奏参数」换成「看分布调」。只记启用（enabled）的采样。
_PACING_LOCK = threading.Lock()
_PACING: Dict[str, Dict[str, float]] = {}
_PACING_FIELDS = (
    "count", "adaptive_count", "sum_delay", "sum_target", "sum_elapsed",
    "max_delay", "last_delay",
)


def _bump(platform: str, key: str) -> None:
    plat = str(platform or "unknown").lower()
    with _LOCK:
        row = _METRICS.setdefault(
            plat, {"read_ok": 0, "read_fail": 0, "typing_ok": 0, "typing_fail": 0})
        row[key] = int(row.get(key, 0)) + 1


def record_read(platform: str, ok: bool) -> None:
    _bump(platform, "read_ok" if ok else "read_fail")


def record_typing(platform: str, ok: bool) -> None:
    _bump(platform, "typing_ok" if ok else "typing_fail")


def snapshot() -> Dict[str, Dict[str, int]]:
    with _LOCK:
        return {p: dict(v) for p, v in _METRICS.items()}


def record_pacing(path: str, result: Any) -> None:
    """记一次拟人延迟采样（仅 enabled 的；未启用配置不污染分布）。

    ``result`` 为 humanize.PacingResult（鸭子类型取字段）。累计 count / adaptive 占比 /
    delay·target·elapsed 之和（供算均值）/ max / last。异常静默（观测绝不影响主流程）。
    """
    try:
        if not getattr(result, "enabled", False):
            return
        p = str(path or "unknown").lower()
        delay = float(getattr(result, "delay", 0.0) or 0.0)
        target = float(getattr(result, "target", 0.0) or 0.0)
        elapsed = float(getattr(result, "elapsed", 0.0) or 0.0)
        is_adaptive = bool(getattr(result, "adaptive", False))
        with _PACING_LOCK:
            row = _PACING.setdefault(p, {k: 0.0 for k in _PACING_FIELDS})
            row["count"] += 1
            if is_adaptive:
                row["adaptive_count"] += 1
            row["sum_delay"] += delay
            row["sum_target"] += target
            row["sum_elapsed"] += elapsed
            row["max_delay"] = max(row["max_delay"], delay)
            row["last_delay"] = round(delay, 2)
    except Exception:
        pass


def pacing_snapshot() -> Dict[str, Dict[str, float]]:
    """拟人延迟分布快照（每路径附 avg_delay/avg_target/avg_elapsed 便于直读）。"""
    with _PACING_LOCK:
        out: Dict[str, Dict[str, float]] = {}
        for p, row in _PACING.items():
            n = row.get("count", 0) or 0
            d = dict(row)
            if n > 0:
                d["avg_delay"] = round(row["sum_delay"] / n, 2)
                d["avg_target"] = round(row["sum_target"] / n, 2)
                d["avg_elapsed"] = round(row["sum_elapsed"] / n, 2)
            out[p] = d
        return out


def reset() -> None:
    """测试钩子：清空累计。"""
    with _LOCK:
        _METRICS.clear()
    with _PACING_LOCK:
        _PACING.clear()


__all__ = [
    "record_read", "record_typing", "snapshot",
    "record_pacing", "pacing_snapshot", "reset",
]
