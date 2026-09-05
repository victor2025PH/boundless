"""R8 危机升级 → 工作台落点桥（#185 决策 D7，2026-09-05）。

R8 此前「叫人」只有一条路：``escalation_needed`` webhook。桌面包/多数客户机根本没配
webhook ⇒ severe 危机触发升级后**没有任何人被叫到**，页面还是一张正常空表。本桥把
升级事件接进工作台三处既有落点，让「叫人」不依赖外部通道：

1. **「需人工」红徽标**：``tag_needs_human``（HANDOFF_TAG，rail 徽标 / 需人工 chip 同源）
   + handoff_meta ``{reason: crisis:<category>, source: wellbeing}`` 可解释；
2. **会话置顶**：``store.set_conversation_pinned(cid, True)``（全坐席共见）；
3. **案例跟进落一条**：``store.record_escalation(cid, reason="crisis", …)``
   （既有 ``escalations`` 表，问责/接管时延口径同款）。

接线两路（互为兜底，幂等）：
- **推**：``crisis_event_store.add_crisis_event_listener`` —— skill_manager 照常只调
  ``record()``，落库成功即同步进桥（零轮询延迟；**不改 skill_manager**）；
- **扫**：看门狗每 tick ``sweep``——只处理 id 高于启动水位的升级事件（不追溯历史，
  防重启后把陈年事件全钉顶），补「监听器晚于事件注册」的窄窗。

会话定位：危机事件只有 ``user_id``/``chat_id``（A 线原生 id），inbox 会话键是
``platform:account:chat_key``——按裸 chat_key 反查（``find_conversations_by_chat_key``，
先 chat_id 再 user_id），取最近活跃那条。反查不到＝该 peer 不在工作台（如纯 A 线
无 inbox 镜像）→ 如实记 ``unresolved`` 不猜。任何异常吞掉：这是旁路，绝不反噬主回复。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("WellbeingEscalationBridge")

#: escalations.reason / handoff_meta.reason 前缀（数据值，非 i18n 键）
CRISIS_REASON = "crisis"
#: handoff_meta.source（与 autosend/takeover 的 source 同族）
CRISIS_SOURCE = "wellbeing"
#: 同会话 record_escalation 去重窗（与 skill_manager R8 冷却同量级，防连击刷表）
ESCALATION_DEDUP_SEC = 3600.0


def should_bridge(event: Dict[str, Any]) -> bool:
    """只桥接**真正触发了升级**的 severe 事件（R8 语义：streak/冷却已由 skill_manager 判定）。

    elevated / 未升级的 severe 只留痕不叫人——否则每条负面情绪都钉顶＝徽标失去意义。
    """
    if not isinstance(event, dict):
        return False
    if not bool(event.get("escalated")):
        return False
    return str(event.get("level") or "") == "severe"


def resolve_conversation(store: Any, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """危机事件 → inbox 会话行（最近活跃优先）；找不到返回 None。"""
    if store is None or not hasattr(store, "find_conversations_by_chat_key"):
        return None
    keys: List[str] = []
    for k in (event.get("chat_id"), event.get("user_id")):
        ks = str(k or "").strip()
        if ks and ks not in keys:
            keys.append(ks)
    for ks in keys:
        try:
            rows = store.find_conversations_by_chat_key(ks, limit=5) or []
        except Exception:
            logger.debug("[crisis-bridge] find_conversations_by_chat_key 失败", exc_info=True)
            rows = []
        if rows:
            # find_* 已按 last_ts DESC 排序；再显式取最大防实现漂移
            return max(rows, key=lambda r: float(r.get("last_ts") or 0))
    return None


def apply_crisis_escalation(
    store: Any, event: Dict[str, Any], *, now: Optional[float] = None,
) -> Dict[str, Any]:
    """把一条升级事件落到工作台三处；返回落点结果（可观测，供测试/日志）。

    返回键：``bridged``(bool) / ``conversation_id`` / ``tagged`` / ``pinned`` /
    ``escalation_recorded`` / ``reason``（skipped_not_escalated | unresolved | ok）。
    """
    out: Dict[str, Any] = {
        "bridged": False, "conversation_id": "", "tagged": False,
        "pinned": False, "escalation_recorded": False, "reason": "",
        "event_id": event.get("id") if isinstance(event, dict) else None,
    }
    if not should_bridge(event):
        out["reason"] = "skipped_not_escalated"
        return out
    if store is None:
        out["reason"] = "no_store"
        return out
    conv = resolve_conversation(store, event)
    if not conv:
        out["reason"] = "unresolved"
        return out
    cid = str(conv.get("conversation_id") or "")
    out["conversation_id"] = cid
    ts = float(now if now is not None else time.time())
    category = str(event.get("category") or "").strip()
    reason = f"{CRISIS_REASON}:{category}" if category else CRISIS_REASON

    # 1) 需人工徽标（HANDOFF_TAG 单源；已打过则 False，不算失败）
    try:
        from src.integrations.protocol_autoreply import tag_needs_human
        out["tagged"] = bool(tag_needs_human(
            store,
            {
                "platform": conv.get("platform"),
                "account_id": conv.get("account_id"),
                "chat_key": conv.get("chat_key"),
            },
            reason=reason, source=CRISIS_SOURCE, now=ts,
        ))
    except Exception:
        logger.debug("[crisis-bridge] tag_needs_human 失败", exc_info=True)

    # 2) 置顶
    try:
        if hasattr(store, "set_conversation_pinned"):
            store.set_conversation_pinned(cid, True)
            out["pinned"] = True
    except Exception:
        logger.debug("[crisis-bridge] set_conversation_pinned 失败", exc_info=True)

    # 3) 案例跟进（escalations 表；dedup 窗内同会话不重复入账）
    try:
        if hasattr(store, "record_escalation"):
            out["escalation_recorded"] = bool(store.record_escalation(
                cid, reason=reason, agent_id="", agent_name=CRISIS_SOURCE,
                wait_sec=0, dedup_sec=ESCALATION_DEDUP_SEC, ts=ts,
            ))
    except Exception:
        logger.debug("[crisis-bridge] record_escalation 失败", exc_info=True)

    out["bridged"] = True
    out["reason"] = "ok"
    logger.warning(
        "[wellbeing] 危机升级已落工作台 event=%s cid=%s tagged=%s pinned=%s case=%s",
        out["event_id"], cid, out["tagged"], out["pinned"], out["escalation_recorded"],
    )
    return out


class CrisisEscalationBridge:
    """进程级桥：推（监听器）+ 扫（水位补扫）两路，幂等安装。

    ``store_getter`` 延迟解析 inbox store（bootstrap 顺序无关：事件来时再取）。
    """

    def __init__(self, store_getter: Callable[[], Any]) -> None:
        self._store_getter = store_getter
        self._lock = threading.Lock()
        self._installed = False
        self._high_water: Optional[int] = None
        self._seen_ids: set = set()
        self.stats: Dict[str, int] = {
            "pushed": 0, "swept": 0, "bridged": 0, "unresolved": 0, "skipped": 0,
        }

    # ── 推 ─────────────────────────────────────────────────────────────
    def install(self) -> bool:
        """注册落库监听器（幂等）；返回是否本次新装。"""
        with self._lock:
            if self._installed:
                return False
            try:
                from src.utils.crisis_event_store import add_crisis_event_listener
                add_crisis_event_listener(self.on_event)
                self._installed = True
                return True
            except Exception:
                logger.debug("[crisis-bridge] install 失败", exc_info=True)
                return False

    def uninstall(self) -> None:
        with self._lock:
            if not self._installed:
                return
            try:
                from src.utils.crisis_event_store import remove_crisis_event_listener
                remove_crisis_event_listener(self.on_event)
            except Exception:
                pass
            self._installed = False

    def on_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        self.stats["pushed"] += 1
        return self._handle(event)

    # ── 扫 ─────────────────────────────────────────────────────────────
    def sweep(self, crisis_store: Any, *, limit: int = 50) -> List[Dict[str, Any]]:
        """补扫水位以上的升级事件。首次调用只立水位（不追溯历史）。"""
        if crisis_store is None:
            return []
        if self._high_water is None:
            try:
                self._high_water = int(crisis_store.max_id())
            except Exception:
                self._high_water = 0
            return []
        try:
            rows = crisis_store.list_recent(
                limit=limit, since_id=self._high_water, only_escalated=True,
            ) or []
        except Exception:
            logger.debug("[crisis-bridge] sweep list_recent 失败", exc_info=True)
            return []
        results: List[Dict[str, Any]] = []
        for ev in sorted(rows, key=lambda r: int(r.get("id") or 0)):
            eid = int(ev.get("id") or 0)
            if eid > (self._high_water or 0):
                self._high_water = eid
            if eid in self._seen_ids:
                continue
            self.stats["swept"] += 1
            results.append(self._handle(ev))
        return results

    # ── 共用 ────────────────────────────────────────────────────────────
    def _handle(self, event: Dict[str, Any]) -> Dict[str, Any]:
        eid = event.get("id") if isinstance(event, dict) else None
        if eid is not None:
            with self._lock:
                if eid in self._seen_ids:
                    return {"bridged": False, "reason": "duplicate", "event_id": eid}
                self._seen_ids.add(eid)
                if len(self._seen_ids) > 5000:   # 有界：进程长跑不涨内存
                    self._seen_ids = set(sorted(self._seen_ids)[-2500:])
                if self._high_water is not None and int(eid) > self._high_water:
                    self._high_water = int(eid)
        try:
            store = self._store_getter()
        except Exception:
            store = None
        res = apply_crisis_escalation(store, event)
        if res.get("bridged"):
            self.stats["bridged"] += 1
        elif res.get("reason") == "unresolved":
            self.stats["unresolved"] += 1
        else:
            self.stats["skipped"] += 1
        return res


__all__ = [
    "CRISIS_REASON",
    "CRISIS_SOURCE",
    "CrisisEscalationBridge",
    "apply_crisis_escalation",
    "resolve_conversation",
    "should_bridge",
]
