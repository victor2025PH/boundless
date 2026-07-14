# -*- coding: utf-8 -*-
"""Phase-2: lead_mesh 实时事件推送 (Server-Sent Events).

订阅: 浏览器 EventSource('/lead-mesh/events/stream')
发布: customer_service.assign_to_human / record_outcome 等触发 emit_event()

设计:
- 单进程内 thread-safe broadcaster (queue list)
- 每个 EventSource 连接 = 一个 subscriber, 自己的 queue
- 30 秒 keep-alive ping 防代理超时关连接
- 订阅者断开自动清理 (queue.put 异常时)

事件类型:
- handoff_pending_changed  payload: {count}
- handoff_assigned         payload: {handoff_id, by, customer_name}
- handoff_outcome          payload: {handoff_id, by, outcome, customer_name}
- handoff_replied          payload: {handoff_id, by, peer_name, len}
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 单进程订阅者列表 (每个是 asyncio.Queue)
_subscribers: List[asyncio.Queue] = []
_lock = threading.Lock()


def emit_event(event_type: str, payload: Dict[str, Any]) -> None:
    """向所有订阅者广播事件. 同步调用, 失败的 subscriber 自动清理."""
    msg = {
        "type": event_type,
        "payload": payload,
        "ts": time.time(),
    }
    with _lock:
        # 复制 list 避免迭代时修改
        subs = list(_subscribers)
    dead: List[asyncio.Queue] = []
    for q in subs:
        try:
            q.put_nowait(msg)
        except Exception:  # noqa: BLE001
            dead.append(q)
    if dead:
        with _lock:
            for q in dead:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass


def _add_subscriber(q: asyncio.Queue) -> None:
    with _lock:
        _subscribers.append(q)


def _remove_subscriber(q: asyncio.Queue) -> None:
    with _lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


async def event_stream():
    """SSE async generator. 由 FastAPI router 调用."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _add_subscriber(q)
    try:
        # initial hello
        yield f"event: hello\ndata: {json.dumps({'subscribers': len(_subscribers)})}\n\n"
        last_ping = time.time()
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
                event_type = msg.get("type", "message")
                yield f"event: {event_type}\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                # keep-alive: 每 20s 发一个 comment 防代理切断
                yield f": ping {int(time.time() - last_ping)}s\n\n"
                last_ping = time.time()
    except asyncio.CancelledError:
        # 浏览器关掉
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("[events_stream] generator error: %s", e)
    finally:
        _remove_subscriber(q)


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)
