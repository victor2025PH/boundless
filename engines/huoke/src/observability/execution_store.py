"""
ExecutionStore — SQLite-backed workflow execution history.

Stores every workflow run with:
- Run metadata (name, run_id, start/end time, success)
- Per-step results (status, duration, retries, errors)
- Queryable history for dashboard and debugging

Optimization: WAL mode + batch inserts for high throughput.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import data_file


class ExecutionStore:
    """
    Persistent storage for workflow execution history.

    Usage:
        store = get_execution_store()
        store.save_run(workflow_result)
        runs = store.list_runs(limit=20)
        detail = store.get_run("abc123")
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(data_file("executions.db"))
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id       TEXT PRIMARY KEY,
                workflow     TEXT NOT NULL,
                success      INTEGER NOT NULL,
                aborted      INTEGER NOT NULL DEFAULT 0,
                steps_total  INTEGER NOT NULL DEFAULT 0,
                steps_ok     INTEGER NOT NULL DEFAULT 0,
                steps_fail   INTEGER NOT NULL DEFAULT 0,
                steps_skip   INTEGER NOT NULL DEFAULT 0,
                elapsed_sec  REAL NOT NULL DEFAULT 0,
                variables    TEXT,
                started_at   TEXT NOT NULL,
                finished_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS step_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT NOT NULL REFERENCES runs(run_id),
                step_id      TEXT NOT NULL,
                action       TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL,
                result_json  TEXT,
                error        TEXT NOT NULL DEFAULT '',
                duration_sec REAL NOT NULL DEFAULT 0,
                retries_used INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_runs_workflow ON runs(workflow);
            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
            CREATE INDEX IF NOT EXISTS idx_steps_run ON step_results(run_id);
        """)
        conn.commit()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def save_run(self, result) -> str:
        """
        Save a WorkflowResult to the store.
        Accepts a WorkflowResult object or a dict with the same fields.
        """
        if hasattr(result, "to_dict"):
            d = result.to_dict()
            run_id = result.run_id
            workflow = result.workflow_name
            success = result.success
            aborted = result.aborted
            elapsed = result.elapsed_sec
            variables = getattr(result, "variables", {})
            steps = result.steps
        else:
            d = result
            run_id = d["run_id"]
            workflow = d["workflow_name"]
            success = d["success"]
            aborted = d.get("aborted", False)
            elapsed = d.get("elapsed_sec", 0)
            variables = d.get("variables", {})
            steps = d.get("steps", {})

        now = datetime.now(timezone.utc).isoformat()

        steps_ok = sum(1 for s in steps.values() if s.get("status") == "success")
        steps_fail = sum(1 for s in steps.values() if s.get("status") == "failed")
        steps_skip = sum(1 for s in steps.values() if s.get("status") == "skipped")

        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO runs "
                    "(run_id, workflow, success, aborted, steps_total, steps_ok, "
                    "steps_fail, steps_skip, elapsed_sec, variables, started_at, finished_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, workflow, int(success), int(aborted),
                     len(steps), steps_ok, steps_fail, steps_skip,
                     elapsed, json.dumps(variables, ensure_ascii=False, default=str),
                     now, now),
                )

                for step_id, step_data in steps.items():
                    conn.execute(
                        "INSERT INTO step_results "
                        "(run_id, step_id, status, result_json, error, duration_sec, retries_used) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (run_id, step_id, step_data.get("status", ""),
                         json.dumps(step_data.get("result"), ensure_ascii=False, default=str),
                         step_data.get("error", ""),
                         step_data.get("duration_sec", 0),
                         step_data.get("retries_used", 0)),
                    )

                conn.commit()
            finally:
                conn.close()

        return run_id

    def list_runs(self, workflow: str = "", limit: int = 50,
                  offset: int = 0, success_only: bool = False) -> List[dict]:
        """List workflow runs, newest first."""
        conn = self._conn()
        try:
            query = "SELECT * FROM runs WHERE 1=1"
            params: list = []
            if workflow:
                query += " AND workflow = ?"
                params.append(workflow)
            if success_only:
                query += " AND success = 1"
            query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Optional[dict]:
        """Get full run detail including step results."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            steps = conn.execute(
                "SELECT * FROM step_results WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            result["steps"] = [dict(s) for s in steps]
            if result.get("variables"):
                try:
                    result["variables"] = json.loads(result["variables"])
                except (json.JSONDecodeError, TypeError):
                    pass
            for s in result["steps"]:
                if s.get("result_json"):
                    try:
                        s["result"] = json.loads(s["result_json"])
                    except (json.JSONDecodeError, TypeError):
                        s["result"] = s["result_json"]
            return result
        finally:
            conn.close()

    def get_stats(self, days: int = 7) -> dict:
        """Aggregate stats over recent days."""
        conn = self._conn()
        try:
            cutoff = datetime.now(timezone.utc).isoformat()[:10]
            row = conn.execute("""
                SELECT
                    COUNT(*) as total_runs,
                    SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as failed,
                    AVG(elapsed_sec) as avg_duration,
                    SUM(steps_total) as total_steps,
                    SUM(steps_ok) as total_steps_ok
                FROM runs
            """).fetchone()
            stats = dict(row) if row else {}
            successful = stats.get('successful') or 0
            total_runs = stats.get('total_runs') or 1
            stats["success_rate"] = f"{successful / max(1, total_runs) * 100:.1f}%"

            top_workflows = conn.execute("""
                SELECT workflow, COUNT(*) as runs,
                       SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as ok
                FROM runs GROUP BY workflow ORDER BY runs DESC LIMIT 10
            """).fetchall()
            stats["top_workflows"] = [dict(w) for w in top_workflows]
            return stats
        finally:
            conn.close()

    def cleanup(self, older_than_days: int = 30):
        """Remove old execution records."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        conn = self._conn()
        try:
            run_ids = conn.execute(
                "SELECT run_id FROM runs WHERE started_at < ?", (cutoff,)
            ).fetchall()
            ids = [r["run_id"] for r in run_ids]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM step_results WHERE run_id IN ({placeholders})", ids)
                conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", ids)
                conn.commit()
        finally:
            conn.close()


_store: Optional[ExecutionStore] = None
_store_lock = threading.Lock()


def get_execution_store(**kwargs) -> ExecutionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ExecutionStore(**kwargs)
    return _store
