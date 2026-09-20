"""出站语音连发监测 + 熔断（2026-07-15 三连发事故的告警指纹；A3 2026-09-03 升级熔断）。

事故复盘：一条用户语音换来 3 条出站语音（双流水线竞态 + 分条发送叠加）。竞态
已在消息去重/会话串行层根治，本模块是**回归监测防线**：同一会话短窗内出站语音
条数超阈值 → 记指标 + 发 EventBus 告警（``voice_burst_alert``，webhook 可订阅）。

设计：进程内滑动窗口（chat_id → 最近发送时刻 deque），纯内存零依赖；每 chat
告警间有本地冷却（默认 300s），叠加 WebhookNotifier 每 key 每小时限流双保险。
注意口径：分条发送（split_send）本身 2-3 条是**设计内**行为——默认阈值 3
（>3 条才告警）不会误报正常分条；连发异常（重复处理回归）通常 ≥4 条。

A3（2026-09-03，证据钧机 JZPBAC 21:03-21:09）：**告警不等于止损**
--------------------------------------------------------------
旧行为只有 WARNING + 告警：连发照发。JZPBAC 窗里两个自家 AI 号互聊，语音一条接
一条地堆，日志里 WARNING 刷了一片而**语音一条都没少发**——对客户而言就是被语音
轰炸，对我们而言是 GPU/TTS 白烧。告警是给值守看的，止损得自动做。

故本模块加一层**熔断**（与告警分离、各自阈值）：同会话 ``window_sec``（默认 60s）
内出站语音达 ``threshold_n``（**≥**，配置默认 3）→ 打开熔断，此后
``cooldown_sec``（默认 300s）内该会话一律**降级发文字**；冷却到期自动恢复。

两套阈值刻意分开、语义也不同：
  - 告警 ``max_sends``：``> 3``（第 4 条才吵值守，避免正常分条噪声）——**口径不变**；
  - 熔断 ``threshold_n``：``>= 3``（第 3 条就降级，正好压住分条上限之上的失控）。
「降级文字」而不是「不发」：客户该收到的回复照收，只是不再以语音形态轰炸——静默
才是真事故（族4 的教训），降级是可见且无损的止损。
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

# ── A3 熔断默认值（与上面的告警阈值分离，见模块 docstring）─────────────────────
DEFAULT_CIRCUIT_THRESHOLD = 3      # 窗口内 **>=** 此条数 → 降级文字
DEFAULT_CIRCUIT_COOLDOWN_SEC = 300.0   # 熔断后该会话降级文字的时长


class VoiceBurstGuard:
    """per-chat 出站语音滑动窗口。``record()`` 返回突破阈值的 breach 信息或 None。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sends: "OrderedDict[str, deque]" = OrderedDict()   # chat -> ts deque
        self._last_alert: Dict[str, float] = {}
        # A3：chat -> 熔断到期时刻（此刻之前一律降级文字）
        self._degrade_until: Dict[str, float] = {}
        self.total_bursts = 0
        self.total_circuit_opens = 0
        self.total_degraded = 0

    # ── A3 熔断 ───────────────────────────────────────────────────────────
    def window_count(
        self, chat_id: Any, *, window_sec: float = DEFAULT_WINDOW_SEC,
        now: Optional[float] = None,
    ) -> int:
        """当前窗口内已发语音条数（只读，不记账）。"""
        try:
            ts = float(now if now is not None else time.time())
            key = str(chat_id if chat_id is not None else "")
            with self._lock:
                dq = self._sends.get(key)
                if not dq:
                    return 0
                cutoff = ts - float(window_sec)
                return sum(1 for t in dq if t >= cutoff)
        except Exception:
            return 0

    def open_circuit(
        self, chat_id: Any, *, cooldown_sec: float = DEFAULT_CIRCUIT_COOLDOWN_SEC,
        now: Optional[float] = None,
    ) -> float:
        """打开熔断（幂等续期取**更晚**的那个到期时刻）；返回到期时刻。"""
        try:
            ts = float(now if now is not None else time.time())
            key = str(chat_id if chat_id is not None else "")
            until = ts + max(0.0, float(cooldown_sec))
            with self._lock:
                prev = float(self._degrade_until.get(key, 0.0))
                if until > prev:
                    self._degrade_until[key] = until
                    if prev <= ts:      # 从「未熔断」→「熔断」才算一次新开路
                        self.total_circuit_opens += 1
                else:
                    until = prev
                while len(self._degrade_until) > _MAX_CHATS:
                    self._degrade_until.pop(next(iter(self._degrade_until)), None)
            return until
        except Exception:
            return 0.0

    def circuit_open(self, chat_id: Any, now: Optional[float] = None) -> float:
        """熔断剩余秒数（>0 ＝该降级文字）。到期自动恢复，无需清理任务。"""
        try:
            ts = float(now if now is not None else time.time())
            key = str(chat_id if chat_id is not None else "")
            with self._lock:
                until = float(self._degrade_until.get(key, 0.0))
                if until <= ts:
                    self._degrade_until.pop(key, None)
                    return 0.0
                return until - ts
        except Exception:
            return 0.0

    def note_degraded(self) -> None:
        with self._lock:
            self.total_degraded += 1

    def snapshot(self) -> Dict[str, Any]:
        """观测读数（autosend-status / metrics 可挂）。"""
        with self._lock:
            return {
                "total_bursts": self.total_bursts,
                "circuit_opens": self.total_circuit_opens,
                "degraded_to_text": self.total_degraded,
                "circuits_open_now": len(self._degrade_until),
            }

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


def voice_degrade_reason(
    chat_id: Any, vr_cfg: Optional[Dict[str, Any]] = None,
    *, now: Optional[float] = None,
) -> str:
    """A3 熔断决策入口——发送方在「决定发语音」**之前**调用。

    返回空串＝放行语音；非空＝本条应降级发文字，串值即原因（进决策日志/指标）：
      - ``burst_circuit_open``   该会话熔断中（冷却未到期）；
      - ``burst_circuit_tripped`` 本条会成为窗口内第 ``threshold_n`` 条 → 当场开路。
    配置 ``telegram.voice_reply.burst_alert.circuit.{enabled,threshold_n,cooldown_sec}``
    （默认开，3 条，300s）；``burst_alert.enabled=false`` 整段关。任何异常放行——
    熔断是止损防线，绝不能反过来把正常语音吞掉。
    """
    try:
        ba = (vr_cfg or {}).get("burst_alert")
        ba = ba if isinstance(ba, dict) else {}
        if not ba.get("enabled", True):
            return ""
        cc = ba.get("circuit")
        cc = cc if isinstance(cc, dict) else {}
        if not cc.get("enabled", True):
            return ""
        g = get_voice_burst_guard()
        if g.circuit_open(chat_id, now=now) > 0:
            g.note_degraded()
            return "burst_circuit_open"
        window = float(ba.get("window_sec", DEFAULT_WINDOW_SEC) or DEFAULT_WINDOW_SEC)
        n = int(cc.get("threshold_n", DEFAULT_CIRCUIT_THRESHOLD)
                or DEFAULT_CIRCUIT_THRESHOLD)
        # 已发 count 条，本条将是第 count+1 条：达到阈值即开路并降级本条。
        if g.window_count(chat_id, window_sec=window, now=now) + 1 >= n:
            until = g.open_circuit(
                chat_id,
                cooldown_sec=float(cc.get("cooldown_sec", DEFAULT_CIRCUIT_COOLDOWN_SEC)
                                   or DEFAULT_CIRCUIT_COOLDOWN_SEC),
                now=now)
            g.note_degraded()
            logger.warning(
                "[voice_burst] 熔断开路 chat=%s：%ds 内将达 %d 条语音 → 本会话"
                "降级文字至 %s（A3；对端疑似 AI/连发轰炸）",
                str(chat_id), int(window), n,
                time.strftime("%H:%M:%S", time.localtime(until)))
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("voice_burst_circuit", {
                    "chat_id": str(chat_id), "threshold_n": n,
                    "window_sec": int(window), "until": until,
                    "rate_key": f"voice_burst_circuit:{chat_id}",
                })
            except Exception:
                pass
            return "burst_circuit_tripped"
        return ""
    except Exception:
        return ""


__all__ = ["VoiceBurstGuard", "get_voice_burst_guard", "note_voice_send",
           "voice_degrade_reason"]
