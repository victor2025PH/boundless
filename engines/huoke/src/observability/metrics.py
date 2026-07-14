"""
MetricsCollector — lightweight in-process metrics for automation monitoring.

Tracks:
- Counters: actions performed, errors, messages sent
- Gauges: active devices, queue depth, budget remaining
- Histograms: action duration, API latency
- Rates: actions per minute, success rate over windows

Exposes Prometheus text format via /metrics endpoint, and
structured dict via .snapshot() for dashboard.

Design: No external dependencies. Thread-safe. Minimal overhead.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class _HistogramBucket:
    """Fixed-bucket histogram for latency tracking."""
    bounds: Tuple[float, ...] = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
    counts: List[int] = field(default_factory=lambda: [0] * 10)
    total: float = 0.0
    count: int = 0

    def observe(self, value: float):
        self.total += value
        self.count += 1
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                self.counts[i] += 1
                return
        self.counts[-1] += 1  # +Inf bucket


class MetricsCollector:
    """
    Central metrics collector.

    Usage:
        mc = get_metrics_collector()
        mc.inc("actions_total", platform="telegram", action="send_message")
        mc.gauge("devices_online", 3)
        mc.observe("action_duration_seconds", 1.23, platform="telegram")

        # Prometheus format
        print(mc.prometheus())

        # Dict snapshot
        data = mc.snapshot()
    """

    def __init__(self):
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, _HistogramBucket] = {}
        self._lock = threading.Lock()
        self._start_time = time.time()

        # Sliding window for rate calculation
        self._rate_windows: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=1000))

    def inc(self, name: str, value: float = 1.0, **labels):
        """Increment a counter."""
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value
            self._rate_windows[key].append(time.time())

    def gauge(self, name: str, value: float, **labels):
        """Set a gauge value."""
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, **labels):
        """Add observation to a histogram."""
        key = self._key(name, labels)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = _HistogramBucket()
            self._histograms[key].observe(value)

    def get_counter(self, name: str, **labels) -> float:
        key = self._key(name, labels)
        return self._counters.get(key, 0.0)

    def get_gauge(self, name: str, **labels) -> float:
        key = self._key(name, labels)
        return self._gauges.get(key, 0.0)

    def rate_per_minute(self, name: str, window_sec: float = 300, **labels) -> float:
        """Calculate rate per minute over a sliding window."""
        key = self._key(name, labels)
        with self._lock:
            window = self._rate_windows.get(key)
            if not window:
                return 0.0
            cutoff = time.time() - window_sec
            recent = [t for t in window if t > cutoff]
            if not recent:
                return 0.0
            elapsed = time.time() - recent[0]
            return len(recent) / max(elapsed / 60, 1/60)

    def snapshot(self) -> dict:
        """Full metrics snapshot as dict."""
        with self._lock:
            result = {
                "uptime_sec": round(time.time() - self._start_time, 1),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {},
            }
            for key, h in self._histograms.items():
                result["histograms"][key] = {
                    "count": h.count,
                    "total": round(h.total, 3),
                    "avg": round(h.total / max(1, h.count), 3),
                }
            return result

    def prometheus(self) -> str:
        """Export metrics in Prometheus text exposition format."""
        lines = []
        with self._lock:
            for key, val in sorted(self._counters.items()):
                name, labels = self._parse_key(key)
                label_str = self._label_str(labels)
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name}{label_str} {val}")

            for key, val in sorted(self._gauges.items()):
                name, labels = self._parse_key(key)
                label_str = self._label_str(labels)
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name}{label_str} {val}")

            for key, h in sorted(self._histograms.items()):
                name, labels = self._parse_key(key)
                label_str = self._label_str(labels)
                lines.append(f"# TYPE {name} histogram")
                lines.append(f"{name}_count{label_str} {h.count}")
                lines.append(f"{name}_sum{label_str} {h.total:.3f}")
                cumulative = 0
                for i, bound in enumerate(h.bounds):
                    cumulative += h.counts[i]
                    le_labels = {**labels, "le": str(bound)}
                    lines.append(f'{name}_bucket{self._label_str(le_labels)} {cumulative}')
                cumulative += h.counts[-1]
                inf_labels = {**labels, "le": "+Inf"}
                lines.append(f'{name}_bucket{self._label_str(inf_labels)} {cumulative}')

        return "\n".join(lines) + "\n"

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._rate_windows.clear()

    # -- Key encoding -------------------------------------------------------

    @staticmethod
    def _key(name: str, labels: dict) -> str:
        if not labels:
            return name
        label_parts = sorted(f"{k}={v}" for k, v in labels.items())
        return f"{name}{{{','.join(label_parts)}}}"

    @staticmethod
    def _parse_key(key: str) -> Tuple[str, dict]:
        if "{" not in key:
            return key, {}
        name = key[:key.index("{")]
        label_str = key[key.index("{") + 1:key.rindex("}")]
        labels = {}
        for part in label_str.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                labels[k] = v
        return name, labels

    @staticmethod
    def _label_str(labels: dict) -> str:
        if not labels:
            return ""
        parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
        return "{" + ",".join(parts) + "}"


# Singleton
_collector: Optional[MetricsCollector] = None
_mc_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    global _collector
    if _collector is None:
        with _mc_lock:
            if _collector is None:
                _collector = MetricsCollector()
    return _collector
