"""
AlertManager — rule-based alerting with multi-channel notification.

Features:
- Declarative alert rules (threshold, rate change, pattern match)
- Cooldown period per rule (avoid alert storms)
- Multiple notification channels: log, structured_log, callback
- Alert history for dashboard
- Thread-safe evaluation

Alert lifecycle: INACTIVE → FIRING → RESOLVED → INACTIVE
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional

log = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(str, Enum):
    INACTIVE = "inactive"
    FIRING = "firing"
    RESOLVED = "resolved"


@dataclass
class AlertRule:
    """
    Declarative alert rule.

    Examples:
        AlertRule(
            name="high_error_rate",
            description="Error rate > 50%",
            check=lambda mc: mc.get_counter("errors") / max(1, mc.get_counter("total")) > 0.5,
            severity=AlertSeverity.CRITICAL,
            cooldown_sec=300,
        )
    """
    name: str
    description: str = ""
    check: Callable = field(default=lambda mc: False)
    severity: AlertSeverity = AlertSeverity.WARNING
    cooldown_sec: float = 300.0
    _state: AlertState = AlertState.INACTIVE
    _last_fired: float = 0.0
    _fire_count: int = 0


@dataclass
class AlertEvent:
    rule_name: str
    severity: str
    description: str
    state: str
    timestamp: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "rule": self.rule_name,
            "severity": self.severity,
            "description": self.description,
            "state": self.state,
            "timestamp": self.timestamp,
            "details": self.details,
        }


class AlertManager:
    """
    Evaluates alert rules against metrics and dispatches notifications.

    Usage:
        am = get_alert_manager()
        am.add_rule(AlertRule(
            name="quota_low",
            description="LinkedIn quota below 20%",
            check=lambda mc: mc.get_gauge("quota_remaining", platform="linkedin") < 5,
            severity=AlertSeverity.WARNING,
        ))
        am.add_handler(lambda event: print(f"ALERT: {event.rule_name}"))
        am.evaluate(metrics_collector)
    """

    def __init__(self, history_size: int = 200):
        self._rules: Dict[str, AlertRule] = {}
        self._handlers: List[Callable[[AlertEvent], None]] = []
        self._history: Deque[AlertEvent] = deque(maxlen=history_size)
        self._lock = threading.Lock()

    def add_rule(self, rule: AlertRule):
        with self._lock:
            self._rules[rule.name] = rule

    def remove_rule(self, name: str) -> bool:
        with self._lock:
            return self._rules.pop(name, None) is not None

    def add_handler(self, handler: Callable[[AlertEvent], None]):
        """Add a notification handler. Called when an alert fires or resolves."""
        with self._lock:
            self._handlers.append(handler)

    def evaluate(self, metrics_collector) -> List[AlertEvent]:
        """
        Evaluate all rules against current metrics.
        Returns list of newly fired/resolved alerts.
        """
        events = []
        now = time.time()

        with self._lock:
            rules = list(self._rules.values())

        for rule in rules:
            try:
                triggered = rule.check(metrics_collector)
            except Exception as e:
                log.warning("Alert rule '%s' check failed: %s", rule.name, e)
                continue

            if triggered and rule._state != AlertState.FIRING:
                if now - rule._last_fired < rule.cooldown_sec:
                    continue
                rule._state = AlertState.FIRING
                rule._last_fired = now
                rule._fire_count += 1
                event = AlertEvent(
                    rule_name=rule.name,
                    severity=rule.severity.value,
                    description=rule.description,
                    state="firing",
                )
                events.append(event)
                self._dispatch(event)

            elif not triggered and rule._state == AlertState.FIRING:
                rule._state = AlertState.RESOLVED
                event = AlertEvent(
                    rule_name=rule.name,
                    severity=rule.severity.value,
                    description=rule.description,
                    state="resolved",
                )
                events.append(event)
                self._dispatch(event)

            elif not triggered and rule._state == AlertState.RESOLVED:
                rule._state = AlertState.INACTIVE

        return events

    def _dispatch(self, event: AlertEvent):
        with self._lock:
            self._history.append(event)
            handlers = list(self._handlers)

        log.log(
            logging.CRITICAL if event.severity == "critical" else
            logging.WARNING if event.severity == "warning" else logging.INFO,
            "ALERT [%s] %s: %s (%s)",
            event.severity.upper(), event.state.upper(),
            event.rule_name, event.description,
        )

        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                log.error("Alert handler failed: %s", e)

    def get_active_alerts(self) -> List[dict]:
        with self._lock:
            return [
                {"name": r.name, "severity": r.severity.value,
                 "description": r.description, "state": r._state.value,
                 "fire_count": r._fire_count}
                for r in self._rules.values()
                if r._state == AlertState.FIRING
            ]

    def get_alert_history(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return [e.to_dict() for e in list(self._history)[-limit:]]

    def get_rules(self) -> List[dict]:
        with self._lock:
            return [
                {"name": r.name, "severity": r.severity.value,
                 "description": r.description, "state": r._state.value,
                 "cooldown_sec": r.cooldown_sec, "fire_count": r._fire_count}
                for r in self._rules.values()
            ]


# -- Default rules -----------------------------------------------------------

def create_default_rules() -> List[AlertRule]:
    """Standard alert rules for automation monitoring."""
    return [
        AlertRule(
            name="high_error_rate",
            description="Action error rate exceeds 30%",
            check=lambda mc: (
                mc.get_counter("actions_total{status=error}") /
                max(1, mc.get_counter("actions_total{status=error}") +
                    mc.get_counter("actions_total{status=ok}"))
            ) > 0.3,
            severity=AlertSeverity.CRITICAL,
            cooldown_sec=600,
        ),
        AlertRule(
            name="workflow_failures",
            description="3+ workflow failures in recent history",
            check=lambda mc: mc.get_counter("workflows_failed") >= 3,
            severity=AlertSeverity.WARNING,
            cooldown_sec=1800,
        ),
        AlertRule(
            name="llm_budget_high",
            description="LLM API costs approaching limit",
            check=lambda mc: mc.get_gauge("llm_cost_usd") > 5.0,
            severity=AlertSeverity.WARNING,
            cooldown_sec=3600,
        ),
        AlertRule(
            name="vision_budget_exhausted",
            description="Vision API hourly budget depleted",
            check=lambda mc: mc.get_gauge("vision_budget_remaining") == 0,
            severity=AlertSeverity.INFO,
            cooldown_sec=3600,
        ),
    ]


# -- Singleton ---------------------------------------------------------------

_manager: Optional[AlertManager] = None
_am_lock = threading.Lock()


def get_alert_manager(with_defaults: bool = True) -> AlertManager:
    global _manager
    if _manager is None:
        with _am_lock:
            if _manager is None:
                _manager = AlertManager()
                if with_defaults:
                    for rule in create_default_rules():
                        _manager.add_rule(rule)
    return _manager
