"""
Event Bus — publish/subscribe system for cross-platform orchestration.

Platform automation modules emit events (e.g. "telegram.message_received"),
and workflows or handlers subscribe to patterns. Decouples platforms from
each other — adding a new trigger doesn't require modifying existing code.

Features:
- Pattern matching (glob-style: "telegram.*", "*.message_sent")
- Async handler execution (non-blocking)
- Event history for debugging
- Thread-safe
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Event:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    timestamp: float = 0.0
    event_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.event_id:
            self.event_id = uuid.uuid4().hex[:10]


@dataclass
class Subscription:
    pattern: str
    handler: Callable[[Event], None]
    handler_name: str = ""
    sub_id: str = ""
    max_calls: int = 0
    _call_count: int = 0

    def __post_init__(self):
        if not self.sub_id:
            self.sub_id = uuid.uuid4().hex[:8]
        if not self.handler_name:
            self.handler_name = getattr(self.handler, "__name__", "anonymous")

    def matches(self, event_type: str) -> bool:
        return fnmatch.fnmatch(event_type, self.pattern)

    @property
    def exhausted(self) -> bool:
        return self.max_calls > 0 and self._call_count >= self.max_calls


class EventBus:
    """
    Thread-safe publish/subscribe event bus.

    Usage:
        bus = get_event_bus()

        # Subscribe
        bus.on("telegram.message_received", handle_new_message)
        bus.on("linkedin.*", handle_linkedin_events)

        # Publish
        bus.emit(Event(type="telegram.message_received", data={"text": "hello"}))

        # One-shot subscription (auto-unsubscribe after N calls)
        bus.once("whatsapp.message_sent", on_sent)
    """

    def __init__(self, history_size: int = 500):
        self._subscriptions: List[Subscription] = []
        self._lock = threading.Lock()
        self._history: Deque[Event] = deque(maxlen=history_size)
        self._executor = threading.Thread(target=lambda: None)  # placeholder

    def on(self, pattern: str, handler: Callable[[Event], None],
           max_calls: int = 0) -> str:
        """
        Subscribe to events matching pattern.
        Returns subscription ID for later unsubscribe.
        """
        sub = Subscription(pattern=pattern, handler=handler, max_calls=max_calls)
        with self._lock:
            self._subscriptions.append(sub)
        log.debug("EventBus: subscribed '%s' to pattern '%s'", sub.handler_name, pattern)
        return sub.sub_id

    def once(self, pattern: str, handler: Callable[[Event], None]) -> str:
        """Subscribe for exactly one event."""
        return self.on(pattern, handler, max_calls=1)

    def off(self, sub_id: str) -> bool:
        """Unsubscribe by subscription ID."""
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [s for s in self._subscriptions if s.sub_id != sub_id]
            removed = len(self._subscriptions) < before
        if removed:
            log.debug("EventBus: unsubscribed %s", sub_id)
        return removed

    def emit(self, event: Event, synchronous: bool = False):
        """
        Publish an event. Matching handlers are called in background threads
        unless synchronous=True.
        """
        with self._lock:
            self._history.append(event)
            matching = [s for s in self._subscriptions if s.matches(event.type) and not s.exhausted]

        log.debug("EventBus: emit '%s' → %d handlers", event.type, len(matching))

        for sub in matching:
            sub._call_count += 1
            if synchronous:
                self._invoke_handler(sub, event)
            else:
                t = threading.Thread(
                    target=self._invoke_handler, args=(sub, event),
                    daemon=True, name=f"event-{event.event_id[:6]}-{sub.sub_id[:6]}",
                )
                t.start()

        # Clean up exhausted subscriptions
        with self._lock:
            self._subscriptions = [s for s in self._subscriptions if not s.exhausted]

    def emit_simple(self, event_type: str, source: str = "", **data):
        """Convenience: emit an event from keyword args."""
        self.emit(Event(type=event_type, data=data, source=source))

    @staticmethod
    def _invoke_handler(sub: Subscription, event: Event):
        try:
            sub.handler(event)
        except Exception as e:
            log.error("EventBus handler '%s' failed for '%s': %s",
                      sub.handler_name, event.type, e)

    # -- Query & debugging --------------------------------------------------

    def recent_events(self, limit: int = 50, pattern: str = "") -> List[dict]:
        with self._lock:
            events = list(self._history)
        if pattern:
            events = [e for e in events if fnmatch.fnmatch(e.type, pattern)]
        return [
            {"event_id": e.event_id, "type": e.type, "source": e.source,
             "data": e.data, "timestamp": e.timestamp}
            for e in events[-limit:]
        ]

    def active_subscriptions(self) -> List[dict]:
        with self._lock:
            return [
                {"sub_id": s.sub_id, "pattern": s.pattern,
                 "handler": s.handler_name, "calls": s._call_count,
                 "max_calls": s.max_calls}
                for s in self._subscriptions
            ]

    def clear_subscriptions(self):
        with self._lock:
            self._subscriptions.clear()

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)


# -- Singleton ---------------------------------------------------------------

_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = EventBus()
    return _bus
