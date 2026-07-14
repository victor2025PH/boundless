# -*- coding: utf-8 -*-
"""审计日志 + 配置快照辅助模块。"""
import logging
import sqlite3
import time
import threading

from src.host.device_registry import data_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit log (in-memory + SQLite)
# ---------------------------------------------------------------------------

_audit_log = []
_audit_lock = threading.Lock()


def audit(action: str, target: str = "", detail: str = "",
          source: str = "api"):
    """Record an audit log entry to SQLite + in-memory cache."""
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "timestamp": ts,
        "action": action,
        "target": target,
        "detail": detail[:200],
        "source": source,
    }
    with _audit_lock:
        _audit_log.append(entry)
        if len(_audit_log) > 2000:
            _audit_log[:] = _audit_log[-1000:]
    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_logs (timestamp, action, target, detail, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, action, target, detail[:200], source))
    except Exception:
        pass


def record_audit_log(*, user: str, action: str, path: str, status: int, ip: str):
    """Write audit entry to SQLite database (used by middleware)."""
    db_path = data_file("openclaw.db")
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_logs "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT, action TEXT, "
            "path TEXT, status INTEGER, ip TEXT)"
        )
        conn.execute(
            "INSERT INTO audit_logs (ts, user, action, path, status, ip) VALUES (?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), user, action, path, status, ip)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config history (rollback support)
# ---------------------------------------------------------------------------

_config_history = []
_config_history_lock = threading.Lock()


def save_config_snapshot(config_type: str, data: dict):
    """Save a config snapshot for potential rollback."""
    import datetime
    with _config_history_lock:
        _config_history.append({
            "id": len(_config_history),
            "type": config_type,
            "data": data,
            "timestamp": datetime.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"),
        })
        if len(_config_history) > 50:
            _config_history[:] = _config_history[-30:]
