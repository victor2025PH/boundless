# -*- coding: utf-8 -*-
"""「AI 指纹」四格计数（P-1 D · #259 #254，2026-09-08）。

质检卡四个数（近 24h）：
  * ``draft`` / ``draft_dash``       起草层净化的稿数 / 其中 punct_fix>0 的稿数 → **破折号率**
  * ``svc_checked`` / ``svc_hit``    客服腔守卫检查数 / 命中（rewrite 或 review）数 → **客服腔命中率**
  * ``claim_checked`` / ``claim_unanchored`` 引用锚点守卫检查数 / 无锚点改写数 → **引用无锚点**
  * ``promise_checked`` / ``promise_no_action`` 承诺句检查数 / 无动作数 → **承诺无动作**（P-3 落日志后
    调 :func:`record`，本批先留位）
  * ``gate_checked`` / ``gate_leak``  发送门兜底处理数 / 其中 punct_fix>0（说明有绕过起草层的路径）

与 ``autosend_shadow_log`` 同款：进程内事件环 + ``logs/ai_fingerprint/fp_YYYYMMDD.jsonl`` 持久
（相对 CWD＝实例数据根；测试用 ``AITR_AI_FINGERPRINT_DIR`` 覆写），首次 snapshot 从磁盘回填近两天。
每个事件一行 ``{"ts":…, "k":"draft_dash", "n":1}``，不落文本。绝不抛。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DIR = "logs/ai_fingerprint"
ENV_DIR = "AITR_AI_FINGERPRINT_DIR"
FILE_PREFIX = "fp_"

KINDS = (
    "draft", "draft_dash",
    "svc_checked", "svc_hit",
    "claim_checked", "claim_unanchored",
    "promise_checked", "promise_no_action",
    "gate_checked", "gate_leak",
    # Q-2 D 接口约定④：质检卡自动多两格
    "commitment_claim", "self_blame_repromise", "media_claim",
)

# 红黄绿阈值（百分比）：0 绿 / <2 黄 / ≥2 红
YELLOW_BELOW_PCT = 2.0

_lock = threading.Lock()
_events: Deque[Tuple[float, str, int]] = deque()
_warmed = False
_persist = True
_MAX_KEEP_SEC = 48 * 3600.0


def stats_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def _day_file(ts: float) -> Path:
    return stats_dir() / f"{FILE_PREFIX}{time.strftime('%Y%m%d', time.localtime(ts))}.jsonl"


def _prune(now: float) -> None:
    cutoff = now - _MAX_KEEP_SEC
    while _events and _events[0][0] < cutoff:
        _events.popleft()


def record(kind: str, n: int = 1, *, ts: Optional[float] = None) -> None:
    """记一个事件（``kind ∈ KINDS``；未知 kind 忽略）。绝不抛。"""
    k = str(kind or "")
    if k not in KINDS:
        return
    try:
        cnt = int(n)
    except (TypeError, ValueError):
        cnt = 1
    if cnt <= 0:
        return
    now = float(ts if ts is not None else time.time())
    try:
        # 先回填再追加：保证「进程内事件」与「已落盘事件」不会重复计（回填只在首次、锁内）
        warm_from_disk(now)
        with _lock:
            _events.append((now, k, cnt))
            _prune(now)
        if _persist:
            _append_disk(now, k, cnt)
    except Exception:
        logger.debug("[ai-fingerprint] record 失败（忽略）", exc_info=True)


def record_draft(punct_fix: int, *, ts: Optional[float] = None) -> None:
    record("draft", ts=ts)
    if int(punct_fix or 0) > 0:
        record("draft_dash", ts=ts)


def record_service_tone(action: str, *, ts: Optional[float] = None) -> None:
    record("svc_checked", ts=ts)
    if str(action or "clean") not in ("clean", ""):
        record("svc_hit", ts=ts)


def record_claim(action: str, *, ts: Optional[float] = None) -> None:
    record("claim_checked", ts=ts)
    if str(action or "") == "rewrite":
        record("claim_unanchored", ts=ts)


def record_gate(punct_fix: int, *, ts: Optional[float] = None) -> None:
    record("gate_checked", ts=ts)
    if int(punct_fix or 0) > 0:
        record("gate_leak", ts=ts)


def _append_disk(ts: float, kind: str, n: int) -> None:
    try:
        p = _day_file(ts)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(ts, 3), "k": kind, "n": int(n)}, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("[ai-fingerprint] 落盘失败（忽略）", exc_info=True)


def warm_from_disk(now: Optional[float] = None) -> int:
    """首次读数前从近两天 JSONL 回填（幂等：只做一次）。返回回填条数。"""
    global _warmed
    n0 = float(now if now is not None else time.time())
    loaded = 0
    with _lock:
        if _warmed:
            return 0
        _warmed = True
        if not _persist:
            return 0
        try:
            d = stats_dir()
            if not d.exists():
                return 0
            files = {_day_file(n0), _day_file(n0 - 86400.0)}
            rows: list = []
            for f in sorted(files):
                if not f.exists():
                    continue
                with f.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                            ts = float(o.get("ts") or 0)
                            k = str(o.get("k") or "")
                            n = int(o.get("n") or 1)
                        except Exception:
                            continue
                        if k in KINDS and ts > n0 - _MAX_KEEP_SEC:
                            rows.append((ts, k, n))
            rows.sort()
            # 首次回填发生在任何进程内事件之前（record 也先 warm）→ 直接替换即可，不会重复计
            _events.clear()
            _events.extend(rows)
            _prune(n0)
            loaded = len(rows)
        except Exception:
            logger.debug("[ai-fingerprint] warm_from_disk 失败（忽略）", exc_info=True)
    return loaded


def _level(rate_pct: Optional[float]) -> str:
    if rate_pct is None:
        return "na"
    if rate_pct <= 0.0:
        return "green"
    if rate_pct < YELLOW_BELOW_PCT:
        return "yellow"
    return "red"


def _rate(hit: int, total: int) -> Optional[float]:
    if total <= 0:
        return None
    return round(hit * 100.0 / total, 2)


def snapshot(hours: int = 24, *, now: Optional[float] = None) -> Dict[str, Any]:
    """metrics / ops 卡消费口。四格各带 ``hits / total / rate_pct / level``；``ready`` 表示
    P-3 是否已开始落承诺数据（``promise_checked>0``）。"""
    try:
        warm_from_disk(now)
    except Exception:
        pass
    n0 = float(now if now is not None else time.time())
    win = max(1, min(int(hours or 24), 48)) * 3600.0
    counts: Dict[str, int] = {k: 0 for k in KINDS}
    with _lock:
        for ts, k, n in _events:
            if ts >= n0 - win and ts <= n0 + 60:
                counts[k] = counts.get(k, 0) + int(n)

    def _card(hit_k: str, tot_k: str) -> Dict[str, Any]:
        hit, tot = counts.get(hit_k, 0), counts.get(tot_k, 0)
        r = _rate(hit, tot)
        return {"hits": hit, "total": tot, "rate_pct": r, "level": _level(r)}

    out: Dict[str, Any] = {
        "window_hours": int(win // 3600),
        "dash": _card("draft_dash", "draft"),
        "service_tone": _card("svc_hit", "svc_checked"),
        "claim_unanchored": _card("claim_unanchored", "claim_checked"),
        "promise_no_action": _card("promise_no_action", "promise_checked"),
        "gate_leak": _card("gate_leak", "gate_checked"),
        "counts": counts,
    }
    out["promise_no_action"]["ready"] = counts.get("promise_checked", 0) > 0
    # Q-2 D：CLAIM_KINDS 额外格（命中计数；无样本时 total=0 → 卡上「—」）
    for extra_k in ("commitment_claim", "self_blame_repromise", "media_claim"):
        n = counts.get(extra_k, 0)
        out[extra_k] = {"hits": n, "total": n, "rate_pct": (100.0 if n else None),
                        "level": "red" if n else "green"}
    levels = [out[k]["level"] for k in ("dash", "service_tone", "claim_unanchored", "promise_no_action")]
    out["level"] = "red" if "red" in levels else ("yellow" if "yellow" in levels else
                                                 ("green" if "green" in levels else "na"))
    return out


def _reset_for_tests(*, persist: bool = False) -> None:
    global _warmed, _persist
    with _lock:
        _events.clear()
        _warmed = False
    _persist = bool(persist)


__all__ = [
    "KINDS", "record", "record_draft", "record_service_tone", "record_claim", "record_gate",
    "snapshot", "warm_from_disk", "stats_dir",
]
