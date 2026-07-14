# -*- coding: utf-8 -*-
"""审计日志路由 — SQLite 持久化。"""

from fastapi import APIRouter

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs")
def get_audit_logs(limit: int = 100, action: str = ""):
    """Get recent audit log entries from SQLite."""
    try:
        from ..database import get_conn
        with get_conn() as conn:
            if action:
                rows = conn.execute(
                    "SELECT timestamp, action, target, detail, source "
                    "FROM audit_logs WHERE action LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    (f"%{action}%", limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT timestamp, action, target, detail, source "
                    "FROM audit_logs ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        logs = [dict(r) for r in rows]
        logs.reverse()
        return {"logs": logs, "total": len(logs), "persistent": True}
    except Exception:
        from ..api import _audit_log, _audit_lock
        with _audit_lock:
            logs = list(_audit_log)
        if action:
            logs = [e for e in logs if action in e.get("action", "")]
        return {"logs": logs[-limit:], "total": len(logs),
                "persistent": False}


@router.delete("/logs")
def clear_old_audit_logs(days: int = 30):
    """Delete audit logs older than N days."""
    try:
        from ..database import get_conn
        with get_conn() as conn:
            result = conn.execute(
                "DELETE FROM audit_logs WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",))
            deleted = result.rowcount
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        return {"ok": False, "error": str(e)}
