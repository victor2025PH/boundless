"""
StructuredLogger — JSON structured logging with context enrichment.

All log entries are JSON objects with standardized fields:
  ts, level, module, message, context (dict of extra fields)

Dual output:
  1. File: JSONL format → data/logs/YYYY-MM-DD.jsonl (rotated daily)
  2. Console: human-readable colored format (optional)

Integration: replaces standard logging for automation modules.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.host.device_registry import data_dir


class StructuredLogger:
    """
    Thread-safe structured logger with JSONL file output.

    Usage:
        slog = get_structured_logger()
        slog.info("Message sent", platform="telegram", user="alice", duration=1.2)
        slog.error("Send failed", platform="linkedin", error="timeout")
    """

    def __init__(self, log_dir: Optional[str] = None, console: bool = True):
        self._log_dir = Path(log_dir) if log_dir else (data_dir() / "logs")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._console = console
        self._lock = threading.Lock()
        self._current_date: str = ""
        self._file = None
        self._std_logger = logging.getLogger("openclaw.structured")

    def _ensure_file(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date or self._file is None:
            if self._file:
                self._file.close()
            path = self._log_dir / f"{today}.jsonl"
            self._file = open(path, "a", encoding="utf-8")
            self._current_date = today

    def _write(self, level: str, message: str, **context):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        if context:
            entry["ctx"] = {k: _safe_serialize(v) for k, v in context.items()}

        line = json.dumps(entry, ensure_ascii=False, default=str)

        with self._lock:
            self._ensure_file()
            self._file.write(line + "\n")
            self._file.flush()

        if self._console:
            self._std_logger.log(
                _level_map.get(level, logging.INFO),
                "[%s] %s %s", level.upper(), message,
                " ".join(f"{k}={v}" for k, v in context.items()) if context else "",
            )

    def info(self, message: str, **ctx):
        self._write("info", message, **ctx)

    def warn(self, message: str, **ctx):
        self._write("warn", message, **ctx)

    def error(self, message: str, **ctx):
        self._write("error", message, **ctx)

    def debug(self, message: str, **ctx):
        self._write("debug", message, **ctx)

    def action(self, action_name: str, platform: str, success: bool,
               duration_sec: float = 0.0, **extra):
        """Log an automation action with standardized fields."""
        self._write(
            "info" if success else "error",
            f"ACTION {'OK' if success else 'FAIL'}: {action_name}",
            platform=platform, action=action_name,
            success=success, duration_sec=round(duration_sec, 3),
            **extra,
        )

    def workflow(self, workflow_name: str, run_id: str, status: str,
                 steps_total: int = 0, steps_ok: int = 0,
                 elapsed_sec: float = 0.0, **extra):
        """Log a workflow execution."""
        self._write(
            "info" if status == "success" else "error",
            f"WORKFLOW {status.upper()}: {workflow_name}",
            workflow=workflow_name, run_id=run_id, status=status,
            steps_total=steps_total, steps_ok=steps_ok,
            elapsed_sec=round(elapsed_sec, 2), **extra,
        )

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

    def query_logs(self, date: Optional[str] = None, level: Optional[str] = None,
                   limit: int = 100, contains: str = "") -> list:
        """Query logs from file. Returns list of parsed entries."""
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._log_dir / f"{date}.jsonl"
        if not path.exists():
            return []

        results = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if level and entry.get("level") != level:
                    continue
                if contains and contains.lower() not in line.lower():
                    continue
                results.append(entry)

        return results[-limit:]

    def log_files(self) -> list:
        """List available log files."""
        return sorted(
            [f.name for f in self._log_dir.glob("*.jsonl")],
            reverse=True,
        )


_level_map = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


def _safe_serialize(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, dict):
        return {str(k): _safe_serialize(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe_serialize(i) for i in v[:50]]
    return str(v)[:500]


# Singleton
_logger: Optional[StructuredLogger] = None
_lock = threading.Lock()


def get_structured_logger(**kwargs) -> StructuredLogger:
    global _logger
    if _logger is None:
        with _lock:
            if _logger is None:
                _logger = StructuredLogger(**kwargs)
    return _logger
