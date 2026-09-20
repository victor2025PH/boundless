#!/usr/bin/env python3
"""恢复演练校验（只读）：对一份备份快照逐库做 integrity_check 并抽样表行数。

用法：
  python scripts/verify_restore_drill.py --backup-dir <config>/backups/20260721_010203
  python scripts/verify_restore_drill.py --config-dir <cfg> --latest

只读、不落盘、不改现网库——用于「备份可恢复」的周期性演练/CI 冒烟：
- 每个 *.db：PRAGMA integrity_check == ok？
- inbox.db：抽样 conversations / messages / reply_drafts 行数（存在则打印）。

退出码：全部 ok → 0；任一损坏/无快照 → 1。Sprint1：把备份从「有产物」升级为「可验证」。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 抽样表（存在才查；缺失不算失败——不同实例库集合不同）
_SAMPLE_TABLES = ("conversations", "messages", "reply_drafts", "conversation_settings")


def _table_count(conn: sqlite3.Connection, table: str):
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def verify_one(db: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db), timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {db.name}: 打开失败 {e}")
        return False
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        ok = bool(row) and str(row[0]).lower() == "ok"
        status = "OK" if ok else f"CORRUPT({row[0] if row else '?'})"
        counts = []
        for t in _SAMPLE_TABLES:
            c = _table_count(conn, t)
            if c is not None:
                counts.append(f"{t}={c}")
        extra = ("  " + " ".join(counts)) if counts else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {db.name}: {status}{extra}")
        return ok
    finally:
        conn.close()


def latest_backup(config_dir: Path):
    backups = config_dir / "backups"
    if not backups.is_dir():
        return None
    snaps = sorted(
        d for d in backups.iterdir()
        if d.is_dir() and len(d.name) == 15 and d.name[8] == "_"
    )
    return snaps[-1] if snaps else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="恢复演练：校验备份快照可用性（只读）")
    ap.add_argument("--config-dir", default=str(ROOT / "config"))
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--latest", action="store_true", help="校验最近一份快照")
    args = ap.parse_args(argv)
    config_dir = Path(args.config_dir)
    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
    elif args.latest:
        backup_dir = latest_backup(config_dir)
    else:
        backup_dir = latest_backup(config_dir)
    if not backup_dir or not backup_dir.is_dir():
        print("ERROR: 找不到可校验的备份快照", file=sys.stderr)
        return 1
    dbs = sorted(p for p in backup_dir.glob("*.db") if p.is_file())
    if not dbs:
        print("ERROR: 快照目录无 *.db:", backup_dir, file=sys.stderr)
        return 1
    print(f"恢复演练校验：{backup_dir}（{len(dbs)} 库）")
    all_ok = True
    for db in dbs:
        if not verify_one(db):
            all_ok = False
    print("演练结果:", "全部通过 ✓" if all_ok else "存在损坏 ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
