from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager


def test_set_task_step_handles_empty_checkpoint(monkeypatch):
    from src.host import task_store
    from src.utils.log_config import clear_task_context, set_task_context

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        "task_id TEXT PRIMARY KEY,"
        "status TEXT NOT NULL,"
        "checkpoint TEXT DEFAULT '',"
        "updated_at TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, status, checkpoint) VALUES (?, ?, ?)",
        ("task-1", "running", ""),
    )

    @contextmanager
    def fake_conn():
        yield conn
        conn.commit()

    monkeypatch.setattr(task_store, "get_conn", fake_conn)
    set_task_context(task_id="task-1", device_id="dev1")
    try:
        assert task_store.set_task_step("打开 Members tab", "ペット") is True
    finally:
        clear_task_context()

    row = conn.execute(
        "SELECT checkpoint FROM tasks WHERE task_id='task-1'"
    ).fetchone()
    checkpoint = json.loads(row["checkpoint"])
    assert checkpoint["current_step"] == "打开 Members tab"
    assert checkpoint["current_sub_step"] == "ペット"
    assert checkpoint["current_step_at"]
