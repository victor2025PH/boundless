"""入站结果台账：每条走到统一落库口的消息都留下一个可查的结局（#279 / #44 / #68）。

事故形态：客户「发了消息但收件箱没有」，运维只能靠猜——是没到网关、到了被去重、
会话被删过（墓碑）、空正文（贴纸/撤回/系统事件）、还是落库时抛了异常。此前这些
分叉全是 ``return None`` / ``continue`` / ``logger.debug``，日常日志级别下一行不留。

本模块是纯观测（绝不 raise、绝不改变落库行为）：
- ``record(outcome, ...)``：进程级计数 + 最近 N 条环形缓冲 + 一行 INFO 日志
  （``inserted`` 走 DEBUG 避免刷屏；``store_error`` 走 WARNING）；
- ``dump_stats()`` / ``recent(...)``：供运维接口 / 排障脚本按会话或 chat_key 追一条消息的下落。

结局码（``OUTCOMES``）：
  inserted        新插入
  duplicate       主键已存在（同 msg_id 重投 / hash 孪生），不是丢
  tombstone       会话被删且消息 ts 不晚于删除时刻（历史重放，按已删处理）
  no_content      空正文且无媒体（贴纸/反应/撤回/协议事件等不支持的载荷）
  no_store        落库口拿到的 store 为 None
  no_chat_key     缺 chat_key，无法定位会话
  no_conv_id      归一化后缺 conversation_id / platform
  from_store_skip store 读出的会话回灌被跳过（设计如此，不是丢）
  store_error     落库过程抛异常
  ts_defaulted    实时入站缺 ts → 已用当前时间补齐（附带信息，不是丢）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "OUTCOMES",
    "DROP_OUTCOMES",
    "record",
    "dump_stats",
    "recent",
    "reset_for_tests",
]

OUTCOMES = (
    "inserted", "duplicate", "tombstone", "no_content", "no_store",
    "no_chat_key", "no_conv_id", "from_store_skip", "store_error", "ts_defaulted",
)
# 客户视角「消息消失了」的结局；其余是设计内的静默（重投去重 / 回灌跳过）。
DROP_OUTCOMES = frozenset({
    "tombstone", "no_content", "no_store", "no_chat_key", "no_conv_id", "store_error",
})

_RECENT_CAP = 500
_LOCK = threading.Lock()
_STATS: Dict[str, int] = {k: 0 for k in OUTCOMES}
_RECENT: Deque[Dict[str, Any]] = deque(maxlen=_RECENT_CAP)


def record(
    outcome: str,
    *,
    platform: str = "",
    account_id: str = "",
    chat_key: str = "",
    conversation_id: str = "",
    msg_id: str = "",
    ts: Any = 0,
    direction: str = "in",
    detail: str = "",
) -> None:
    """记一条结局。未知 outcome 归入 ``store_error`` 之外单独计数键（不丢信息）。绝不 raise。"""
    try:
        oc = str(outcome or "").strip() or "unknown"
        try:
            tsf = float(ts or 0)
        except Exception:
            tsf = 0.0
        row = {
            "outcome": oc,
            "platform": str(platform or ""),
            "account_id": str(account_id or ""),
            "chat_key": str(chat_key or ""),
            "conversation_id": str(conversation_id or ""),
            "msg_id": str(msg_id or ""),
            "ts": tsf,
            "direction": str(direction or "in"),
            "detail": str(detail or "")[:200],
            "at": time.time(),
        }
        with _LOCK:
            _STATS[oc] = _STATS.get(oc, 0) + 1
            _RECENT.append(row)
        if oc == "inserted":
            level = logging.DEBUG
        elif oc == "store_error":
            level = logging.WARNING
        elif oc == "from_store_skip":
            level = logging.DEBUG
        else:
            level = logging.INFO
        if logger.isEnabledFor(level):
            logger.log(
                level,
                "[inbound] outcome=%s dir=%s conv=%s platform=%s account=%s chat_key=%s "
                "msg_id=%s ts=%s%s",
                oc, row["direction"], row["conversation_id"] or "-", row["platform"] or "-",
                row["account_id"] or "-", row["chat_key"] or "-", row["msg_id"] or "-",
                int(tsf), (" detail=" + row["detail"]) if row["detail"] else "",
            )
    except Exception:
        pass


def dump_stats() -> Dict[str, Any]:
    with _LOCK:
        stats = dict(_STATS)
    stats["dropped_total"] = sum(v for k, v in stats.items() if k in DROP_OUTCOMES)
    return stats


def recent(
    limit: int = 50,
    *,
    conversation_id: str = "",
    chat_key: str = "",
    drops_only: bool = False,
) -> List[Dict[str, Any]]:
    """最近结局（新→旧）。可按会话 / chat_key 过滤；``drops_only`` 只看客户视角的丢失。"""
    try:
        n = max(1, min(int(limit or 50), _RECENT_CAP))
    except Exception:
        n = 50
    cid = str(conversation_id or "").strip()
    ck = str(chat_key or "").strip()
    with _LOCK:
        rows = list(_RECENT)
    out: List[Dict[str, Any]] = []
    for row in reversed(rows):
        if cid and row.get("conversation_id") != cid:
            continue
        if ck and row.get("chat_key") != ck:
            continue
        if drops_only and row.get("outcome") not in DROP_OUTCOMES:
            continue
        out.append(dict(row))
        if len(out) >= n:
            break
    return out


def reset_for_tests() -> None:
    with _LOCK:
        for k in list(_STATS):
            _STATS[k] = 0
        for k in OUTCOMES:
            _STATS[k] = 0
        _RECENT.clear()
