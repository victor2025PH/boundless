# -*- coding: utf-8 -*-
"""Tests for database schema drift recovery (2026-04-24).

覆盖场景:
  * 全新 DB — init_db() 无任何错误完整建表
  * 老 DB 有 audit_logs.ts 列 (PR #17 报告的 drift) — init_db() 自动 RENAME
    COLUMN 使 executescript 继续, 下游 FB 业务表被正确建立
  * 幂等性 — 连续多次 init_db() 不崩
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _fresh_init(monkeypatch, tmp_path: Path):
    """把 DB_PATH 切到临时文件, 返回 (init_db 函数, db 路径).

    注: database.DB_PATH 是模块加载时的常量 (data_file("openclaw.db")),
    不读 env. 所以直接 monkeypatch.setattr 改常量.
    """
    db_file = tmp_path / "test_openclaw.db"
    import src.host.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    return db_mod.init_db, db_file


class TestInitDbFreshDb:
    """新 DB 首次 init: 所有表应都建起来."""

    def test_fresh_init_creates_all_fb_tables(self, tmp_path, monkeypatch):
        init_db, db = _fresh_init(monkeypatch, tmp_path)
        init_db()
        with sqlite3.connect(db) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        # PR #17 报告的 "FB 业务表全建不起来" 场景 — 必须存在
        assert "facebook_friend_requests" in tables
        assert "facebook_inbox_messages" in tables
        assert "facebook_groups" in tables
        assert "fb_contact_events" in tables
        # audit_logs 本身也在
        assert "audit_logs" in tables

    def test_fresh_init_audit_logs_has_timestamp_col(self, tmp_path, monkeypatch):
        """新 DB 直接有 timestamp 列, 不是 ts."""
        init_db, db = _fresh_init(monkeypatch, tmp_path)
        init_db()
        with sqlite3.connect(db) as c:
            cols = [r[1] for r in c.execute(
                "PRAGMA table_info(audit_logs)").fetchall()]
        assert "timestamp" in cols
        assert "ts" not in cols


class TestInitDbOldDbDrift:
    """老 DB schema drift 场景: audit_logs 用旧列名 `ts`."""

    def _create_old_db_with_ts_col(self, db_path: Path):
        """模拟旧版代码建的 audit_logs — 列名是 `ts`, 带老索引 `idx_audit_ts`."""
        with sqlite3.connect(db_path) as c:
            c.executescript("""
                CREATE TABLE audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    source TEXT DEFAULT 'api',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX idx_audit_ts_old ON audit_logs(ts);
                INSERT INTO audit_logs (ts, action) VALUES
                    ('2026-04-20T10:00:00', 'old_record');
            """)

    def test_drift_db_init_succeeds_and_fb_tables_created(self, tmp_path, monkeypatch):
        """这是 B PR #17 报告的核心场景 — 修复前此测试会失败(FB 表不建)."""
        init_db, db = _fresh_init(monkeypatch, tmp_path)
        # 先建一个老 schema 的 DB, 再调 init_db
        self._create_old_db_with_ts_col(db)
        init_db()
        with sqlite3.connect(db) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            cols = [r[1] for r in c.execute(
                "PRAGMA table_info(audit_logs)").fetchall()]
            # audit_logs 应该已从 ts 改为 timestamp
            assert "timestamp" in cols
            assert "ts" not in cols
            # 老数据保留
            n_old = c.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='old_record'"
            ).fetchone()[0]
            assert n_old == 1
            # 核心: FB 业务表被建起来 (修复前建不起来)
            assert "facebook_friend_requests" in tables
            assert "facebook_inbox_messages" in tables
            assert "fb_contact_events" in tables


class TestInitDbIdempotent:
    """幂等性 — 多次调用不崩."""

    def test_double_init_no_error(self, tmp_path, monkeypatch):
        init_db, _ = _fresh_init(monkeypatch, tmp_path)
        init_db()
        init_db()  # 第二次不应抛错


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
