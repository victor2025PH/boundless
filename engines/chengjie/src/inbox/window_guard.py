# -*- coding: utf-8 -*-
"""平台回复窗口 / 每轮配额的**执行器**（实施96 DY P0 第二轮，2026-09-08）——从收件箱事实推导，不另记账。

规则来自 ``channel_policy.window_rule(platform)``（抖音 24h/6 条、TikTok 48h/10 条、QQ 机器人
60min/4 条……）；本模块只回答「此刻这条还能不能发」。

**为什么不另开一本账**：窗口的两个变量——「对方最后一条入站时间」与「此后我方已发条数」——
收件箱里本来就有：``conversations.last_in_ts``（入站落库即精确推进，P0 未读可信化 v2）与
``messages.direction='out'``。再记一本账就多一处会漂移的状态；从事实源现算，重启不丢、
回放不重、无需在入站链路（``protocol_bridge`` / ``ingest``）加钩子。

**与实施97 ``kf_window_guard`` 的分工**：微信客服有平台回推的「关窗」事件（``msg_send_fail``
fail_type 4/5/6/10），那是事实源里没有的信息，所以微信客服继续由它记账执行；本模块对
``kf_window_guard.is_quota_platform()`` 认领的平台一律让路，不做双重判定。两边数值同源于
``channel_policy``（门禁钉住）。

**接线**：``AccountOrchestrator.send`` / ``send_media``（与 channel_policy 文本/媒体硬规则同一处）；
UI 经 ``snapshot()``（``/api/unified-inbox/send-caps?chat_key=`` 的 ``reply_window`` 字段）画倒计时
与「本轮剩余 N 条」。

**边界**：抖音的「进私事件 30 秒快路径」与「已授权用户的主动私信」是官方 worker 才有的场景，
由该 worker 携带 scene 走自己的判定；本模块按最保守口径——**没有入站就不许发**——这正是
抖音/TikTok 对未授权用户的平台规则。全程绝不抛：读不到 store / 异常 → 放行（broken guard 不得把发送卡死）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

REASON_NO_INBOUND = "policy_window_no_inbound"          # 对方从未发言（平台只允许被动回复）
REASON_EXPIRED = "policy_window_expired"                # 距对方最后一条已超窗
REASON_EXHAUSTED = "policy_window_exhausted"            # 本轮配额用完
REASON_RESERVED = "policy_window_reserved_for_manual"   # 自动链让路给坐席预留

#: 现算时最多回看多少条消息（配额 ≤10，60 条足够覆盖一轮；防大会话全表扫）
_LOOKBACK = 60


@dataclass(frozen=True)
class WindowState:
    platform: str
    window_sec: float
    cap: int
    reserve: int
    last_inbound_ts: float
    sent_since_inbound: int
    now: float

    @property
    def no_inbound(self) -> bool:
        return self.last_inbound_ts <= 0

    @property
    def remaining_sec(self) -> float:
        if self.no_inbound:
            return 0.0
        return max(0.0, self.window_sec - (self.now - self.last_inbound_ts))

    @property
    def expired(self) -> bool:
        return (not self.no_inbound) and self.remaining_sec <= 0

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.sent_since_inbound)

    @property
    def deadline_ts(self) -> float:
        return (self.last_inbound_ts + self.window_sec) if not self.no_inbound else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform, "window_sec": self.window_sec, "cap": self.cap,
            "reserve_for_manual": self.reserve, "last_inbound_ts": self.last_inbound_ts,
            "sent": self.sent_since_inbound, "remaining": self.remaining,
            "remaining_sec": round(self.remaining_sec, 1), "deadline_ts": self.deadline_ts,
            "no_inbound": self.no_inbound, "expired": self.expired,
        }


def _handled_by_kf(platform: str) -> bool:
    try:
        from src.inbox.kf_window_guard import is_quota_platform
        return bool(is_quota_platform(platform))
    except Exception:
        return False


def _default_store() -> Any:
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def _conv_id(platform: str, account_id: str, chat_key: str) -> str:
    try:
        from src.inbox.normalizer import conv_id
        return conv_id(platform, account_id, chat_key)
    except Exception:
        return f"{platform}:{account_id}:{chat_key}"


def _facts(store: Any, cid: str) -> Optional[tuple]:
    """(last_in_ts, sent_since_inbound, has_any_outbound)；会话不存在 → (0, 0, False)；读挂 → None。"""
    try:
        conv = store.get_conversation(cid) or {}
        last_in = float(conv.get("last_in_ts") or 0.0) if conv else 0.0
        msgs = store.list_recent_messages(cid, limit=_LOOKBACK, include_deleted=True) if conv else []
        if last_in <= 0 and conv:
            # 存量会话 last_in_ts 未回填时按消息表兜底（与 store 回填 SQL 同语义）
            ins = [float(m.get("ts") or 0) for m in msgs if str(m.get("direction")) == "in"]
            last_in = max(ins) if ins else 0.0
        outs = [m for m in msgs if str(m.get("direction")) == "out"]
        sent = sum(1 for m in outs if float(m.get("ts") or 0) >= last_in) if last_in > 0 else 0
        return last_in, sent, bool(outs)
    except Exception:
        logger.debug("[window_guard] 读收件箱事实失败（放行）", exc_info=True)
        return None


def window_state(platform: Any, account_id: Any, chat_key: Any, *, mode: str = "",
                 store: Any = None, now: Optional[float] = None,
                 config: Optional[Dict[str, Any]] = None) -> Optional[WindowState]:
    """该会话此刻的窗口状态；平台无窗口规则或读不到事实 → None。"""
    from src.inbox.channel_policy import window_rule
    rule = window_rule(platform, mode, config)
    if rule is None:
        return None
    st = store if store is not None else _default_store()
    if st is None:
        return None
    facts = _facts(st, _conv_id(str(platform).lower(), str(account_id or "default"), str(chat_key or "")))
    if facts is None:
        return None
    last_in, sent, _ = facts
    return WindowState(platform=str(platform).lower(), window_sec=float(rule[0]), cap=int(rule[1]),
                       reserve=int(rule[2]), last_inbound_ts=last_in, sent_since_inbound=sent,
                       now=float(now if now is not None else time.time()))


def send_block_reason(platform: Any, account_id: Any, chat_key: Any, *, mode: str = "",
                      origin: str = "auto", store: Any = None, now: Optional[float] = None,
                      config: Optional[Dict[str, Any]] = None) -> str:
    """能不能发：空串＝放行；否则 ``policy_window_*`` 原因码（进 send_gate_status 的 policy_window 族）。

    判序：无入站 → 超窗 → 配额用完 → 自动链让路人工预留（``origin="manual"`` 可用满）。
    """
    try:
        p = str(platform or "").lower()
        if not p or not str(chat_key or "").strip() or _handled_by_kf(p):
            return ""
        st = window_state(p, account_id, chat_key, mode=mode, store=store, now=now, config=config)
        if st is None:
            return ""
        if st.no_inbound:
            return REASON_NO_INBOUND
        if st.expired:
            return REASON_EXPIRED
        if st.remaining <= 0:
            return REASON_EXHAUSTED
        if str(origin or "auto") != "manual" and st.remaining <= st.reserve:
            return REASON_RESERVED
        return ""
    except Exception:
        logger.debug("[window_guard] 判定异常（放行）", exc_info=True)
        return ""


def is_first_message(platform: Any, account_id: Any, chat_key: Any, *, store: Any = None) -> bool:
    """该会话我方是否**从未**发过消息（TikTok「首条禁链」用）。读不到事实 → False（不误拦）。"""
    try:
        st = store if store is not None else _default_store()
        if st is None:
            return False
        facts = _facts(st, _conv_id(str(platform).lower(), str(account_id or "default"), str(chat_key or "")))
        return bool(facts) and not facts[2]
    except Exception:
        return False


def snapshot(platform: Any, account_id: Any, chat_key: Any, *, mode: str = "", store: Any = None,
             now: Optional[float] = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给 UI 的一屏（非窗口平台 → ``{}``）：倒计时 / 剩余条数 / 自动与人工各自能否再发。"""
    try:
        p = str(platform or "").lower()
        if _handled_by_kf(p):
            return {}
        st = window_state(p, account_id, chat_key, mode=mode, store=store, now=now, config=config)
        if st is None:
            return {}
        d = st.as_dict()
        d["manual_allowed"] = send_block_reason(p, account_id, chat_key, mode=mode, origin="manual",
                                                store=store, now=now, config=config) == ""
        d["auto_allowed"] = send_block_reason(p, account_id, chat_key, mode=mode, origin="auto",
                                              store=store, now=now, config=config) == ""
        d["reason"] = send_block_reason(p, account_id, chat_key, mode=mode, origin="manual",
                                        store=store, now=now, config=config)
        return d
    except Exception:
        logger.debug("[window_guard] snapshot 异常", exc_info=True)
        return {}


__all__ = ["REASON_NO_INBOUND", "REASON_EXPIRED", "REASON_EXHAUSTED", "REASON_RESERVED",
           "WindowState", "window_state", "send_block_reason", "is_first_message", "snapshot"]
