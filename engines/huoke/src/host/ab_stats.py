# -*- coding: utf-8 -*-
"""A/B 测试统计跟踪器 — 记录话术变体使用和回复情况。"""
import json
import threading
import time

from src.host.device_registry import data_file

_STATS_PATH = data_file("ab_stats.json")
_lock = threading.Lock()


def _load() -> dict:
    if _STATS_PATH.exists():
        try:
            return json.loads(_STATS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_sent(variant_id: str, device_id: str = ""):
    """Record a message sent using this variant."""
    with _lock:
        data = _load()
        v = data.setdefault(variant_id, {"sent": 0, "replied": 0, "devices": {}})
        v["sent"] += 1
        if device_id:
            v["devices"][device_id] = v["devices"].get(device_id, 0) + 1
        v["last_sent"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save(data)


def record_reply(variant_id: str):
    """Record a reply received for a message sent with this variant."""
    with _lock:
        data = _load()
        if variant_id in data:
            data[variant_id]["replied"] = data[variant_id].get("replied", 0) + 1
            _save(data)


def get_stats() -> dict:
    """Return all variant stats with reply rates."""
    data = _load()
    result = {}
    for vid, v in data.items():
        sent = v.get("sent", 0)
        replied = v.get("replied", 0)
        result[vid] = {
            "sent": sent,
            "replied": replied,
            "reply_rate": round(replied / max(sent, 1) * 100, 1),
            "last_sent": v.get("last_sent", ""),
        }
    return result


def get_adaptive_weight(variant_id: str, base_weight: float = 1.0) -> float:
    """P7-E: 基于回复率动态调整权重（需要 ≥30 次发送才生效）。
    reply_rate ≥15% → 2×权重（高效变体加量）
    reply_rate  8-15% → 1.5×权重
    reply_rate  3-8%  → 1×权重（保持观望）
    reply_rate  <3%  → 0.5×权重（低效变体减量）
    """
    data = _load()
    v = data.get(variant_id, {})
    sent = v.get("sent", 0)
    if sent < 30:
        return base_weight  # 数据不足，保持原始权重
    reply_rate = v.get("replied", 0) / sent
    if reply_rate >= 0.15:
        return base_weight * 2.0
    elif reply_rate >= 0.08:
        return base_weight * 1.5
    elif reply_rate >= 0.03:
        return base_weight * 1.0
    else:
        return base_weight * 0.5


def select_variant(variants: list) -> dict:
    """P7-E: 带自适应性能权重的变体选择。数据不足时使用原始 YAML 权重。"""
    import random
    if not variants:
        return {}
    weighted = []
    for v in variants:
        base_w = float(v.get("weight", 1))
        vid = v.get("id", "")
        eff_w = get_adaptive_weight(vid, base_w) if vid else base_w
        weighted.append((eff_w, v))
    total = sum(w for w, _ in weighted)
    r = random.uniform(0, total)
    cumulative = 0.0
    for w, v in weighted:
        cumulative += w
        if r <= cumulative:
            return v
    return weighted[-1][1]


def get_weights_report() -> dict:
    """返回当前各变体的有效权重（用于前端展示）。"""
    data = _load()
    result = {}
    for vid, v in data.items():
        sent = v.get("sent", 0)
        base = 1.0
        eff = get_adaptive_weight(vid, base)
        result[vid] = {
            "sent": sent,
            "replied": v.get("replied", 0),
            "reply_rate": round(v.get("replied", 0) / max(sent, 1) * 100, 1),
            "effective_weight": round(eff, 2),
            "adapted": sent >= 30,
        }
    return result
