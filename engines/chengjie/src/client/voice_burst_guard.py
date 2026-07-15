"""出站语音连发监测（2026-07-15 三连发事故的告警指纹）。

事故复盘：一条用户语音换来 3 条出站语音（双流水线竞态 + 分条发送叠加）。竞态
已在消息去重/会话串行层根治，本模块是**回归监测防线**：同一会话短窗内出站语音
条数超阈值 → 记指标 + 发 EventBus 告警（``voice_burst_alert``，webhook 可订阅）。

设计：进程内滑动窗口（chat_id → 最近发送时刻 deque），纯内存零依赖；每 chat
告警间有本地冷却（默认 300s），叠加 WebhookNotifier 每 key 每小时限流双保险。
注意口径：分条发送（split_send）本身 2-3 条是**设计内**行为——默认阈值 3
（>3 条才告警）不会误报正常分条；连发异常（重复处理回归）通常 ≥4 条。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SEC = 60.0
DEFAULT_MAX_SENDS = 3        # 窗口内 > 此条数 → 异常（3 条=分条上限，仍属正常）
_ALERT_COOLDOWN_SEC = 300.0  # 同 chat 两次告警最小间隔（防持续连发刷屏）
_MAX_CHATS = 512


class VoiceBurstGuard:
    """per-chat 出站语音滑动窗口。``record()`` 返回突破阈值的 breach 信息或 None。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sends: "OrderedDict[str, deque]" = OrderedDict()   # chat -> ts deque
        self._last_alert: Dict[str, float] = {}
        self.total_bursts = 0

    def record(
        self,
        chat_id: Any,
        *,
        window_sec: float = DEFAULT_WINDOW_SEC,
        max_sends: int = DEFAULT_MAX_SENDS,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """记一次出站语音。窗口内条数**超过** ``max_sends`` 且不在冷却期 →
        返回 breach dict（调用方据此告警）；否则 None。绝不抛。"""
        try:
            ts = float(now if now is not None else time.time())
            key = str(chat_id if chat_id is not None else "")
            with self._lock:
                dq = self._sends.get(key)
                if dq is None:
                    dq = deque(maxlen=32)
                    self._sends[key] = dq
                self._sends.move_to_end(key)
                while len(self._sends) > _MAX_CHATS:
                    _old, _ = self._sends.popitem(last=False)
                    self._last_alert.pop(_old, None)
                dq.append(ts)
                cutoff = ts - float(window_sec)
                while dq and dq[0] < cutoff:
                    dq.popleft()
                count = len(dq)
                if count <= int(max_sends):
                    return None
                if ts - self._last_alert.get(key, 0.0) < _ALERT_COOLDOWN_SEC:
                    return None
                self._last_alert[key] = ts
                self.total_bursts += 1
                return {
                    "chat_id": key,
                    "count": count,
                    "window_sec": int(window_sec),
                }
        except Exception:
            return None


_SINGLETON: Optional[VoiceBurstGuard] = None
_SINGLETON_LOCK = threading.Lock()


def get_voice_burst_guard() -> VoiceBurstGuard:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = VoiceBurstGuard()
    return _SINGLETON


def note_voice_send(chat_id: Any, vr_cfg: Optional[Dict[str, Any]] = None) -> None:
    """发送方在每条出站语音后调用：滑窗记账，超阈值 → 指标 + WARNING + EventBus。

    配置 ``telegram.voice_reply.burst_alert.{enabled,window_sec,max_sends}``
    （默认开）。任何异常静默——监测防线绝不影响发送主链路。
    """
    try:
        ba = (vr_cfg or {}).get("burst_alert")
        ba = ba if isinstance(ba, dict) else {}
        if not ba.get("enabled", True):
            return
        breach = get_voice_burst_guard().record(
            chat_id,
            window_sec=float(ba.get("window_sec", DEFAULT_WINDOW_SEC)
                             or DEFAULT_WINDOW_SEC),
            max_sends=int(ba.get("max_sends", DEFAULT_MAX_SENDS)
                          or DEFAULT_MAX_SENDS),
        )
        if not breach:
            return
        logger.warning(
            "[voice_burst] 同会话短窗语音连发 chat=%s count=%d window=%ds"
            "（三连发事故指纹，请核查去重/串行是否回归）",
            breach["chat_id"], breach["count"], breach["window_sec"])
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_voice_burst()
        except Exception:
            pass
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("voice_burst_alert", {
                **breach,
                "rate_key": f"voice_burst:{breach['chat_id']}",
            })
        except Exception:
            pass
    except Exception:
        pass


__all__ = ["VoiceBurstGuard", "get_voice_burst_guard", "note_voice_send"]
