# -*- coding: utf-8 -*-
"""
结构化日志配置。一次 setup_logging() 即可全局生效。

特性:
  - 控制台: 彩色人类可读格式
  - 文件: JSON 每行一条（便于 grep/jq 解析）
  - 文件轮转: 10MB x 5 个备份
  - 任务上下文注入: task_id, device_id
"""

import json
import logging
import logging.handlers
import threading
import time
from pathlib import Path

_task_context = threading.local()


def set_task_context(task_id: str = "", device_id: str = ""):
    """在当前线程注入任务上下文，后续所有日志自动携带。"""
    _task_context.task_id = task_id
    _task_context.device_id = device_id


def clear_task_context():
    _task_context.task_id = ""
    _task_context.device_id = ""


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.task_id = getattr(_task_context, "task_id", "")
        record.device_id = getattr(_task_context, "device_id", "")
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.task_id:
            obj["task_id"] = record.task_id
        if record.device_id:
            obj["device_id"] = record.device_id
        if record.exc_info and record.exc_info[1]:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    GREY = "\033[38;5;244m"
    BLUE = "\033[34m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD_RED = "\033[31;1m"
    RESET = "\033[0m"

    COLORS = {
        logging.DEBUG: GREY,
        logging.INFO: BLUE,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ctx = ""
        if getattr(record, "task_id", ""):
            ctx = f" [{record.task_id[:8]}]"
        return f"{color}{ts} {record.levelname:<7s}{self.RESET} {record.name}{ctx} | {record.getMessage()}"


class RingBufferHandler(logging.Handler):
    """In-memory ring buffer for recent log entries (dashboard API)."""

    _instance = None

    def __init__(self, capacity: int = 500):
        super().__init__()
        from collections import deque
        self._buffer = deque(maxlen=capacity)
        self._lock_buf = threading.Lock()
        RingBufferHandler._instance = self

    def emit(self, record):
        try:
            entry = self.format(record)
            with self._lock_buf:
                self._buffer.append(entry)
        except Exception:
            self.handleError(record)

    def get_entries(self, limit: int = 100, level: str = "") -> list:
        with self._lock_buf:
            items = list(self._buffer)
        if level:
            filtered = []
            for item in items:
                try:
                    obj = json.loads(item)
                    if obj.get("level", "").upper() == level.upper():
                        filtered.append(item)
                except Exception:
                    pass
            items = filtered
        return items[-limit:]

    @classmethod
    def get_instance(cls):
        return cls._instance


def setup_logging(level: int = logging.INFO, log_dir: str = "logs"):
    """
    一次配置，全局生效。

    level:    最低日志级别
    log_dir:  日志文件目录
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler 避免重复
    root.handlers.clear()

    ctx_filter = _ContextFilter()

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(_ConsoleFormatter())
    ch.addFilter(ctx_filter)
    root.addHandler(ch)

    # JSON file handler (rotating)
    fh = logging.handlers.RotatingFileHandler(
        str(log_path / "openclaw.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(_JsonFormatter())
    fh.addFilter(ctx_filter)
    root.addHandler(fh)

    # In-memory ring buffer for dashboard log panel
    rbh = RingBufferHandler(capacity=500)
    rbh.setLevel(logging.INFO)
    rbh.setFormatter(_JsonFormatter())
    rbh.addFilter(ctx_filter)
    root.addHandler(rbh)

    # uvicorn access log -> quieter
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
