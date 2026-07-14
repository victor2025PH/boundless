"""
Device Matrix — multi-device parallel task orchestration.

Architecture:
  ┌──────────────────────────────────────────────────┐
  │                  Task Queue (SQLite)              │
  │   task_id │ platform │ action │ status │ claimed  │
  ├──────────────────────────────────────────────────┤
  │  Device Worker D1   │  Device Worker D2          │
  │  ┌───────────────┐  │  ┌───────────────┐         │
  │  │ poll → claim  │  │  │ poll → claim  │         │
  │  │ → execute     │  │  │ → execute     │         │
  │  │ → report      │  │  │ → report      │         │
  │  └───────────────┘  │  └───────────────┘         │
  └──────────────────────────────────────────────────┘

Key features:
  - SQLite-backed atomic task claiming (no double-execution)
  - Device-platform affinity (assign platforms to specific devices)
  - Auto health check before each task
  - Load balancing with priority queuing
  - Self-healing: dead workers' tasks get reclaimed
  - EventBus integration for real-time status
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.host.device_registry import data_file

from .device_manager import DeviceManager, DeviceStatus, get_device_manager

log = logging.getLogger(__name__)

_DB_PATH = data_file("device_matrix.db")


class TaskStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


@dataclass
class MatrixTask:
    task_id: str
    platform: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    priority: int = 5
    status: TaskStatus = TaskStatus.QUEUED
    device_id: str = ""
    result: Any = None
    error: str = ""
    created_at: str = ""
    claimed_at: str = ""
    completed_at: str = ""
    retry_count: int = 0
    max_retries: int = 2
    timeout_sec: float = 300.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "action": self.action,
            "params": self.params,
            "priority": self.priority,
            "status": self.status.value if isinstance(self.status, TaskStatus) else self.status,
            "device_id": self.device_id,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass
class DeviceProfile:
    """Per-device configuration within the matrix."""
    device_id: str
    display_name: str = ""
    platforms: List[str] = field(default_factory=list)
    max_concurrent: int = 1
    enabled: bool = True
    health_ok: bool = True
    current_task: str = ""
    tasks_completed: int = 0
    tasks_failed: int = 0
    last_heartbeat: float = 0.0


class DeviceMatrix:
    """
    Orchestrates task distribution across multiple devices.

    Usage:
        matrix = DeviceMatrix()
        matrix.register_device("D1", platforms=["telegram", "linkedin"])
        matrix.register_device("D2", platforms=["tiktok", "twitter", "whatsapp"])

        matrix.submit("telegram", "send_message", {"recipient": "user", "text": "hi"})
        matrix.submit("tiktok", "browse_feed", {"video_count": 10})

        matrix.start_workers()
    """

    def __init__(self, dm: Optional[DeviceManager] = None,
                 db_path: Optional[str] = None,
                 action_handler: Optional[Callable] = None):
        self.dm = dm or get_device_manager()
        self._db_path = str(db_path or _DB_PATH)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._devices: Dict[str, DeviceProfile] = {}
        self._workers: Dict[str, threading.Thread] = {}
        self._running = False
        self._lock = threading.Lock()
        self._action_handler = action_handler
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS matrix_tasks (
                task_id      TEXT PRIMARY KEY,
                platform     TEXT NOT NULL,
                action       TEXT NOT NULL,
                params       TEXT DEFAULT '{}',
                priority     INTEGER DEFAULT 5,
                status       TEXT DEFAULT 'queued',
                device_id    TEXT DEFAULT '',
                result       TEXT DEFAULT '',
                error        TEXT DEFAULT '',
                retry_count  INTEGER DEFAULT 0,
                max_retries  INTEGER DEFAULT 2,
                timeout_sec  REAL DEFAULT 300.0,
                created_at   TEXT,
                claimed_at   TEXT DEFAULT '',
                completed_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_mt_status ON matrix_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_mt_priority ON matrix_tasks(priority DESC, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_mt_platform ON matrix_tasks(platform);
            CREATE INDEX IF NOT EXISTS idx_mt_device ON matrix_tasks(device_id);
        """)
        conn.commit()
        conn.close()

    # ── Device Registration ───────────────────────────────────────────────

    def register_device(self, device_id: str,
                        display_name: str = "",
                        platforms: Optional[List[str]] = None,
                        max_concurrent: int = 1) -> DeviceProfile:
        profile = DeviceProfile(
            device_id=device_id,
            display_name=display_name or device_id[:8],
            platforms=platforms or [],
            max_concurrent=max_concurrent,
        )
        with self._lock:
            self._devices[device_id] = profile
        log.info("Matrix: registered device %s (platforms=%s)", device_id, platforms)
        return profile

    def auto_register(self):
        """Discover connected devices and register them."""
        self.dm.discover_devices()
        for info in self.dm.get_all_devices():
            if info.status == DeviceStatus.CONNECTED and info.device_id not in self._devices:
                self.register_device(info.device_id, info.display_name)

    def get_device_profile(self, device_id: str) -> Optional[DeviceProfile]:
        return self._devices.get(device_id)

    def set_device_platforms(self, device_id: str, platforms: List[str]):
        if device_id in self._devices:
            self._devices[device_id].platforms = platforms

    # ── Task Submission ───────────────────────────────────────────────────

    def submit(self, platform: str, action: str,
               params: Optional[Dict[str, Any]] = None,
               priority: int = 5, max_retries: int = 2,
               timeout_sec: float = 300.0) -> str:
        """Submit a task to the queue. Returns task_id."""
        task_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        conn.execute("""
            INSERT INTO matrix_tasks
                (task_id, platform, action, params, priority, status,
                 max_retries, timeout_sec, created_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
        """, (task_id, platform, action, json.dumps(params or {}),
              priority, max_retries, timeout_sec, now))
        conn.commit()
        conn.close()

        self._emit("matrix.task_submitted", task_id=task_id,
                    platform=platform, action=action)
        log.debug("Matrix: submitted task %s (%s.%s)", task_id, platform, action)
        return task_id

    def submit_batch(self, tasks: List[Dict[str, Any]]) -> List[str]:
        """Submit multiple tasks at once."""
        ids = []
        for t in tasks:
            tid = self.submit(
                platform=t["platform"], action=t["action"],
                params=t.get("params"), priority=t.get("priority", 5),
            )
            ids.append(tid)
        return ids

    # ── Task Claiming (Atomic) ────────────────────────────────────────────

    def _claim_task(self, device_id: str) -> Optional[MatrixTask]:
        """Atomically claim the highest-priority queued task for this device."""
        profile = self._devices.get(device_id)
        if not profile or not profile.enabled:
            return None

        platform_filter = ""
        params = [device_id]
        if profile.platforms:
            placeholders = ",".join("?" for _ in profile.platforms)
            platform_filter = f"AND platform IN ({placeholders})"
            params.extend(profile.platforms)

        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        try:
            row = conn.execute(f"""
                SELECT task_id FROM matrix_tasks
                WHERE status = 'queued' {platform_filter}
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            """, params[1:] if platform_filter else []).fetchone()

            if not row:
                return None

            tid = row[0]
            affected = conn.execute("""
                UPDATE matrix_tasks
                SET status = 'claimed', device_id = ?, claimed_at = ?
                WHERE task_id = ? AND status = 'queued'
            """, (device_id, now, tid)).rowcount
            conn.commit()

            if affected == 0:
                return None

            task_row = conn.execute(
                "SELECT * FROM matrix_tasks WHERE task_id = ?", (tid,)
            ).fetchone()
            return self._row_to_task(task_row)
        finally:
            conn.close()

    def _update_task(self, task_id: str, **fields):
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            if isinstance(v, (dict, list)):
                vals.append(json.dumps(v))
            else:
                vals.append(v)
        vals.append(task_id)
        conn = self._conn()
        conn.execute(f"UPDATE matrix_tasks SET {', '.join(sets)} WHERE task_id = ?", vals)
        conn.commit()
        conn.close()

    # ── Worker Lifecycle ──────────────────────────────────────────────────

    def start_workers(self):
        """Start a worker thread for each registered device."""
        self._running = True
        for did, profile in self._devices.items():
            if profile.enabled and did not in self._workers:
                t = threading.Thread(
                    target=self._worker_loop, args=(did,),
                    daemon=True, name=f"matrix-{did[:8]}",
                )
                self._workers[did] = t
                t.start()
                log.info("Matrix: worker started for %s", did)

    def stop_workers(self):
        self._running = False
        for did, t in self._workers.items():
            t.join(timeout=5)
        self._workers.clear()
        log.info("Matrix: all workers stopped")

    def _worker_loop(self, device_id: str):
        """Main loop for a device worker."""
        profile = self._devices[device_id]
        idle_backoff = 2.0

        while self._running and profile.enabled:
            try:
                # Health check
                if not self._check_device_health(device_id):
                    profile.health_ok = False
                    self._emit("matrix.device_unhealthy", device_id=device_id)
                    time.sleep(30)
                    continue
                profile.health_ok = True
                profile.last_heartbeat = time.time()

                # Claim and execute
                task = self._claim_task(device_id)
                if task is None:
                    time.sleep(idle_backoff)
                    idle_backoff = min(idle_backoff * 1.3, 15.0)
                    continue

                idle_backoff = 2.0
                profile.current_task = task.task_id
                self._execute_task(device_id, task)
                profile.current_task = ""

            except Exception as e:
                log.error("Matrix worker %s error: %s", device_id, e)
                time.sleep(5)

    def _execute_task(self, device_id: str, task: MatrixTask):
        """Execute a claimed task on the device."""
        now = datetime.now(timezone.utc).isoformat()
        self._update_task(task.task_id, status="running")
        self._emit("matrix.task_started", task_id=task.task_id,
                    device_id=device_id, action=task.action)

        start = time.time()
        try:
            result = self._dispatch(device_id, task)
            elapsed = time.time() - start
            self._update_task(
                task.task_id, status="success",
                result=json.dumps(result) if result else "",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            profile = self._devices[device_id]
            profile.tasks_completed += 1
            self._emit("matrix.task_completed", task_id=task.task_id,
                        device_id=device_id, elapsed=round(elapsed, 1))

        except Exception as e:
            elapsed = time.time() - start
            error_msg = f"{type(e).__name__}: {e}"
            log.warning("Task %s failed on %s: %s", task.task_id, device_id, error_msg)

            if task.retry_count < task.max_retries:
                self._update_task(
                    task.task_id, status="queued", device_id="",
                    retry_count=task.retry_count + 1, error=error_msg,
                )
                self._emit("matrix.task_retrying", task_id=task.task_id,
                            retry=task.retry_count + 1)
            else:
                self._update_task(
                    task.task_id, status="failed", error=error_msg,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                profile = self._devices[device_id]
                profile.tasks_failed += 1
                self._emit("matrix.task_failed", task_id=task.task_id,
                            device_id=device_id, error=error_msg)

    def _dispatch(self, device_id: str, task: MatrixTask) -> Any:
        """Route task to the appropriate handler."""
        if self._action_handler:
            return self._action_handler(device_id, task.platform, task.action,
                                        task.params)
        # Default: use ActionRegistry
        from ..workflow.actions import get_action_registry
        registry = get_action_registry()
        action_name = f"{task.platform}.{task.action}"
        action = registry.get(action_name)
        if not action:
            raise ValueError(f"Unknown action: {action_name}")
        fn, _meta = action
        params = dict(task.params)
        params["device_id"] = device_id
        return fn(**params)

    # ── Health Check ──────────────────────────────────────────────────────

    def _check_device_health(self, device_id: str) -> bool:
        """Quick device health check."""
        try:
            d = self.dm.get_u2(device_id)
            if d is None:
                return False
            info = d.info
            return info.get("currentPackageName") is not None
        except Exception:
            return False

    # ── Stale Task Recovery ───────────────────────────────────────────────

    def recover_stale_tasks(self, stale_minutes: int = 15):
        """Re-queue tasks that were claimed but never completed."""
        cutoff = datetime.now(timezone.utc)
        conn = self._conn()
        rows = conn.execute("""
            SELECT task_id, claimed_at FROM matrix_tasks
            WHERE status IN ('claimed', 'running')
            AND claimed_at != ''
        """).fetchall()

        recovered = 0
        for row in rows:
            try:
                claimed = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
                age_min = (cutoff - claimed).total_seconds() / 60
                if age_min > stale_minutes:
                    conn.execute("""
                        UPDATE matrix_tasks SET status = 'queued', device_id = ''
                        WHERE task_id = ? AND status IN ('claimed', 'running')
                    """, (row["task_id"],))
                    recovered += 1
            except (ValueError, TypeError):
                continue
        conn.commit()
        conn.close()
        if recovered:
            log.info("Matrix: recovered %d stale tasks", recovered)
        return recovered

    # ── Queries ───────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> Optional[MatrixTask]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM matrix_tasks WHERE task_id = ?",
                           (task_id,)).fetchone()
        conn.close()
        return self._row_to_task(row) if row else None

    def list_tasks(self, status: str = "", platform: str = "",
                   device_id: str = "", limit: int = 50) -> List[MatrixTask]:
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if device_id:
            conditions.append("device_id = ?")
            params.append(device_id)
        where = " AND ".join(conditions) if conditions else "1=1"
        conn = self._conn()
        rows = conn.execute(
            f"SELECT * FROM matrix_tasks WHERE {where} "
            "ORDER BY priority DESC, created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        conn.close()
        return [self._row_to_task(r) for r in rows]

    def queue_stats(self) -> Dict[str, Any]:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM matrix_tasks").fetchone()[0]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM matrix_tasks GROUP BY status"
        ):
            by_status[row[0]] = row[1]
        by_platform = {}
        for row in conn.execute(
            "SELECT platform, COUNT(*) FROM matrix_tasks WHERE status = 'queued' GROUP BY platform"
        ):
            by_platform[row[0]] = row[1]
        conn.close()

        device_stats = {}
        for did, profile in self._devices.items():
            device_stats[did] = {
                "name": profile.display_name,
                "platforms": profile.platforms,
                "enabled": profile.enabled,
                "health_ok": profile.health_ok,
                "current_task": profile.current_task,
                "completed": profile.tasks_completed,
                "failed": profile.tasks_failed,
                "worker_alive": did in self._workers and self._workers[did].is_alive(),
            }

        return {
            "total_tasks": total,
            "by_status": by_status,
            "queued_by_platform": by_platform,
            "devices": device_stats,
            "workers_running": self._running,
        }

    def cancel_task(self, task_id: str) -> bool:
        conn = self._conn()
        affected = conn.execute(
            "UPDATE matrix_tasks SET status = 'cancelled' "
            "WHERE task_id = ? AND status IN ('queued', 'claimed')",
            (task_id,),
        ).rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def purge_completed(self, older_than_hours: int = 24) -> int:
        conn = self._conn()
        affected = conn.execute(
            "DELETE FROM matrix_tasks WHERE status IN ('success', 'failed', 'cancelled') "
            "AND completed_at < datetime('now', ?)",
            (f"-{older_than_hours} hours",),
        ).rowcount
        conn.commit()
        conn.close()
        return affected

    # ── Internal ──────────────────────────────────────────────────────────

    def _emit(self, event_type: str, **data):
        try:
            from ..workflow.event_bus import get_event_bus
            get_event_bus().emit_simple(event_type, source="device_matrix", **data)
        except Exception:
            pass

    @staticmethod
    def _row_to_task(row) -> MatrixTask:
        d = dict(row)
        params = d.get("params", "{}")
        try:
            params = json.loads(params) if isinstance(params, str) else params
        except (json.JSONDecodeError, TypeError):
            params = {}
        result = d.get("result", "")
        try:
            result = json.loads(result) if result else None
        except (json.JSONDecodeError, TypeError):
            pass
        return MatrixTask(
            task_id=d["task_id"], platform=d["platform"], action=d["action"],
            params=params, priority=d.get("priority", 5),
            status=d.get("status", "queued"), device_id=d.get("device_id", ""),
            result=result, error=d.get("error", ""),
            created_at=d.get("created_at", ""),
            claimed_at=d.get("claimed_at", ""),
            completed_at=d.get("completed_at", ""),
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 2),
            timeout_sec=d.get("timeout_sec", 300.0),
        )


# ── Singleton ─────────────────────────────────────────────────────────────

_matrix: Optional[DeviceMatrix] = None
_matrix_lock = threading.Lock()


def get_device_matrix() -> DeviceMatrix:
    global _matrix
    if _matrix is None:
        with _matrix_lock:
            if _matrix is None:
                _matrix = DeviceMatrix()
    return _matrix
