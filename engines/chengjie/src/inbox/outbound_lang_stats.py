"""出站语言硬闸观测（P1-198，2026-08-03）。

背景：8/03「英文会话原样发出中文」事故的观测盲区——客户看得见、系统零告警、
零指标，两次同类事故（7/31、8/03）都靠人翻聊天记录才发现。本模块把语言硬闸
（``outbound_translate`` 的 CJK 冲突处理）的三类事件变成常驻可观测：

  - ``held``            冲突且翻译不可用 → HOLD 拦下（两种模式都记；恒为异常信号）
  - ``rescued``         冲突且翻译救回（**仅 gate_only 模式记**——常规翻译模式下
                        CJK→客户语言是设计内例行路径，计进来会淹没真异常）
  - ``no_target_sent``  CJK 文本盲发（客户证据 + 出站历史参照双缺位；不算事故，
                        但持续增长说明有会话在无判定依据地发中文，值得看一眼）
  - ``complaint``       客户语言困惑/抱怨检出（P3-198：错语言语境下的「看不懂/??」
                        ——客户已被伤到的**结果面**信号，覆盖闸门管不到的错配类）

风格对齐 ``src/web/frontend_error_stats.py``：无新增依赖、线程安全、进程级单例、
只存计数与消毒后的会话号（不存消息内容）。经 ``dump()`` →
``/api/workspace/metrics.outbound_lang_gate`` 与 ``/api/drafts/autosend-status``、
``dump_prom()`` → Prometheus ``outbound_lang_gate_total`` 观测。
进程内计数随重启清零——趋势化留给 P2（对齐 translation_trend_store 模式）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

_OUTCOMES = ("held", "rescued", "no_target_sent", "complaint")
_LAST_EVENTS_CAP = 8
_CONV_ID_MAX = 80


class OutboundLangStats:
    """语言硬闸事件计数器（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {k: 0 for k in _OUTCOMES}
        self._last_events: Deque[Dict[str, Any]] = deque(maxlen=_LAST_EVENTS_CAP)

    def record(
        self, outcome: str, *, conversation_id: str = "", target: str = "",
    ) -> None:
        key = str(outcome or "").strip()
        if key not in _OUTCOMES:
            return
        cid = str(conversation_id or "")[:_CONV_ID_MAX]
        with self._lock:
            self._counts[key] += 1
            self._last_events.append({
                "ts": time.time(),
                "outcome": key,
                "conversation_id": cid,
                "target": str(target or "")[:16],
            })

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            out: Dict[str, Any] = dict(self._counts)
            out["last_events"] = list(self._last_events)
            return out

    def dump_prom(self) -> str:
        with self._lock:
            counts = dict(self._counts)
        lines = [
            "# HELP outbound_lang_gate_total 出站语言硬闸事件累计（按结果分桶）",
            "# TYPE outbound_lang_gate_total counter",
        ]
        for k in _OUTCOMES:
            lines.append(
                'outbound_lang_gate_total{outcome="%s"} %d' % (k, counts.get(k, 0)))
        return "\n".join(lines) + "\n"

    def reset_for_tests(self) -> None:
        with self._lock:
            self._counts = {k: 0 for k in _OUTCOMES}
            self._last_events.clear()


_SINGLETON: Optional[OutboundLangStats] = None
_LOCK = threading.Lock()


def get_outbound_lang_stats() -> OutboundLangStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = OutboundLangStats()
    return _SINGLETON


__all__ = ["OutboundLangStats", "get_outbound_lang_stats"]
