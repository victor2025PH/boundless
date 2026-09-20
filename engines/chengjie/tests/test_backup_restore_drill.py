"""Sprint1：SQLite 备份 → 恢复演练校验 → 恢复 的端到端 roundtrip。

覆盖 backup_sqlite_dbs（--config-dir/--retention 与保留清理）、verify_restore_drill
（integrity_check）、restore_sqlite_dbs（覆盖前预留 + 损坏跳过）。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load(name):
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _make_db(path: Path, value: int):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()


def test_backup_verify_restore_roundtrip(tmp_path):
    backup = _load("backup_sqlite_dbs")
    verify = _load("verify_restore_drill")
    restore = _load("restore_sqlite_dbs")

    cfg = tmp_path / "config"
    cfg.mkdir()
    _make_db(cfg / "inbox.db", 42)
    _make_db(cfg / "audit.db", 7)

    # 备份
    assert backup.run_backup(cfg, retention=0) == 0
    snaps = sorted((cfg / "backups").iterdir())
    assert len(snaps) == 1
    snap = snaps[0]
    assert (snap / "inbox.db").is_file() and (snap / "audit.db").is_file()

    # 恢复演练校验（只读）→ 全部完好
    assert verify.main(["--backup-dir", str(snap)]) == 0

    # 破坏现网库后恢复
    (cfg / "inbox.db").write_bytes(b"corrupted-not-sqlite")
    assert restore.restore(cfg, snap) == 0
    conn = sqlite3.connect(str(cfg / "inbox.db"))
    row = conn.execute("SELECT x FROM t").fetchone()
    conn.close()
    assert row == (42,), "恢复后数据应还原"
    # 覆盖前的当前库应被预留
    pre_dirs = [d for d in (cfg / "backups").iterdir() if d.name.startswith("_pre_restore_")]
    assert pre_dirs, "恢复应在覆盖前预留当前库"


def test_backup_retention_prunes_old(tmp_path):
    backup = _load("backup_sqlite_dbs")
    cfg = tmp_path / "config"
    cfg.mkdir()
    _make_db(cfg / "inbox.db", 1)
    backups = cfg / "backups"
    # 造 5 份旧快照目录（时间戳命名）
    for ts in ("20260101_000001", "20260101_000002", "20260101_000003",
               "20260101_000004", "20260101_000005"):
        (backups / ts).mkdir(parents=True)
        _make_db(backups / ts / "inbox.db", 1)
    # retention=2 → 清理后仅剩最近 2 份旧的 + 本次新建 1 份
    assert backup.run_backup(cfg, retention=2) == 0
    remaining = sorted(d.name for d in backups.iterdir() if d.is_dir())
    # 旧的 5 份应只留最近 2 份（000004/000005），加本次新快照 = 3
    assert "20260101_000001" not in remaining
    assert "20260101_000002" not in remaining
    assert "20260101_000005" in remaining
