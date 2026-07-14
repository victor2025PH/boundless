# -*- coding: utf-8 -*-
"""
SSE 实时事件流 — 替代轮询的服务端推送。

架构:
  - EventStreamHub 单例管理所有 SSE 订阅者
  - 发布事件: push_event(type, data) → 所有连接的浏览器即时收到
  - 事件类型: task.created, task.completed, task.failed, task.progress,
              device.online, device.offline, device.alert, system.log
  - 最近 100 条事件缓存，新连接可回放最近 N 条
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


class EventStreamHub:
    """Thread-safe SSE event hub with subscriber management."""

    _instance: Optional["EventStreamHub"] = None

    def __init__(self, history_size: int = 100):
        self._queues: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=history_size)
        self._counter = 0

    @classmethod
    def get(cls) -> "EventStreamHub":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def push_event(self, event_type: str, data: dict = None,
                   device_id: str = ""):
        """Publish an event to all SSE subscribers (thread-safe)."""
        self._counter += 1
        event = {
            "id": self._counter,
            "type": event_type,
            "data": data or {},
            "device_id": device_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self._lock:
            self._history.append(event)
            dead = []
            for q in self._queues:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._queues.remove(q)

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def get_recent(self, n: int = 50) -> list:
        with self._lock:
            items = list(self._history)
        return items[-n:]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._queues)


def push_event(event_type: str, data: dict = None, device_id: str = ""):
    """Convenience function to push events from anywhere."""
    EventStreamHub.get().push_event(event_type, data, device_id)
    try:
        from .websocket_hub import get_ws_hub
        hub = get_ws_hub()
        hub.broadcast(event_type, {"device_id": device_id, **(data or {})})
    except Exception:
        pass
