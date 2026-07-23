#!/usr/bin/env python3
"""从 backup 快照恢复 SQLite 库到 config 目录（与 backup_sqlite_dbs.py 配对）。

用法：
  python scripts/restore_sqlite_dbs.py --backup-dir <config>/backups/20260721_010203
  python scripts/restore_sqlite_dbs.py --config-dir <cfg> --timestamp 20260721_010203

安全：
- 恢复前对每个来源快照做 PRAGMA integrity_check，损坏文件跳过并告警（不覆盖现网好库）；
- 覆盖前把当前库另存到 <config>/backups/_pre_restore_<ts>/（可回退）；
- 仅恢复快照目录里存在的 *.db。

Sprint1：与备份/恢复演练配套，让「业务库可恢复」从口头变为可执行、可验证。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def integrity_ok(db: Path) -> bool:
    """PRAGMA integrity_check == 'ok' 视为完好。无法打开/非 ok → False。"""
    try:
        conn = sqlite3.connect(str(db), timeout=15)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        return bool(row) and str(row[0]).lower() == "ok"
    except Exception as e:  # noqa: BLE001
        print("WARN: integrity_check 打开失败", db.name, e, file=sys.stderr)
        return False


def restore(config_dir: Path, backup_dir: Path) -> int:
    if not backup_dir.is_dir():
        print("ERROR: 备份目录不存在:", backup_dir, file=sys.stderr)
        return 1
    config_dir.mkdir(parents=True, exist_ok=True)
    snaps = sorted(p for p in backup_dir.glob("*.db") if p.is_file())
    if not snaps:
        print("ERROR: 备份目录无 *.db:", backup_dir, file=sys.stderr)
        return 1
    pre = config_dir / "backups" / ("_pre_restore_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    pre.mkdir(parents=True, exist_ok=True)
    restored, skipped = 0, 0
    for src in snaps:
        if not integrity_ok(src):
            print("SKIP: 快照损坏，未恢复", src.name, file=sys.stderr)
            skipped += 1
            continue
        dest = config_dir / src.name
        if dest.exists():
            try:
                shutil.copy2(dest, pre / src.name)  # 覆盖前留存当前库
            except Exception as e:  # noqa: BLE001
                print("WARN: 现库预留失败（继续恢复）", dest.name, e, file=sys.stderr)
        try:
            shutil.copy2(src, dest)
            restored += 1
            print("restored", src.name, "->", dest)
        except Exception as e:  # noqa: BLE001
            print("ERROR: 恢复失败", src.name, e, file=sys.stderr)
            skipped += 1
    print(f"OK: restored={restored} skipped={skipped} (pre-restore 备份于 {pre})")
    return 0 if restored > 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从备份快照恢复 SQLite 库")
    ap.add_argument("--config-dir", default=str(ROOT / "config"),
                    help="恢复目标 config 目录")
    ap.add_argument("--backup-dir", default="",
                    help="快照目录（优先）；给出则忽略 --timestamp")
    ap.add_argument("--timestamp", default="",
                    help="快照时间戳（<config>/backups/<ts>）")
    args = ap.parse_args(argv)
    config_dir = Path(args.config_dir)
    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
    elif args.timestamp:
        backup_dir = config_dir / "backups" / args.timestamp
    else:
        print("ERROR: 需给 --backup-dir 或 --timestamp", file=sys.stderr)
        return 2
    return restore(config_dir, backup_dir)


if __name__ == "__main__":
    raise SystemExit(main())
