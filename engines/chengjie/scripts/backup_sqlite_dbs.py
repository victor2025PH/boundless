#!/usr/bin/env python3
"""
将 config 目录下 SQLite 库备份到 <config>/backups/YYYYMMDD_HHMMSS/。
优先使用 SQLite backup API 生成一致快照（WAL 下更安全）；失败时回退为文件复制并打印告警。

用法：
  python scripts/backup_sqlite_dbs.py                          # 默认引擎 config/
  python scripts/backup_sqlite_dbs.py --config-dir D:\\chengjie-instances\\zhiliao\\data\\config
  python scripts/backup_sqlite_dbs.py --retention 14           # 仅保留最近 14 份快照

Sprint1 增强：新增 --config-dir（对接双实例生产库路径）与 --retention（保留策略，
清理旧快照），配套 restore_sqlite_dbs.py / verify_restore_drill.py。
``backup_one_file`` 签名保持不变（外部工具/测试依赖）。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 项目根：scripts/ 上一级
ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
PATTERNS = ("*.db",)


def _backup_one_sqlite(src: Path, dest: Path) -> None:
    """使用 connection.backup 写入目标文件（在线库亦可得到一致快照）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dest_conn = sqlite3.connect(str(dest), timeout=30)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def backup_one_file(src: Path, dest: Path) -> str:
    """
    备份单个 .db 文件。返回 'sqlite' 或 'copy'，表示所用方式。
    供测试与外部工具导入（importlib 加载本脚本）。
    """
    try:
        _backup_one_sqlite(src, dest)
        return "sqlite"
    except Exception as e:
        print(
            "WARN: sqlite backup failed, fallback to copy2:",
            src.name,
            e,
            file=sys.stderr,
        )
        shutil.copy2(src, dest)
        return "copy"


def prune_old_backups(backups_dir: Path, retention: int) -> int:
    """保留最近 ``retention`` 份快照子目录，删除更旧的。retention<=0 → 不清理。

    返回删除的快照目录数。仅删除形如 ``YYYYMMDD_HHMMSS`` 的时间戳目录（防误删）。
    """
    if retention <= 0 or not backups_dir.is_dir():
        return 0
    snaps = [
        d for d in backups_dir.iterdir()
        if d.is_dir() and len(d.name) == 15 and d.name[8] == "_"
        and d.name.replace("_", "").isdigit()
    ]
    snaps.sort(key=lambda d: d.name)  # 时间戳字典序 == 时间序
    to_delete = snaps[:-retention] if len(snaps) > retention else []
    removed = 0
    for d in to_delete:
        try:
            shutil.rmtree(d)
            removed += 1
            print("pruned old backup", d.name)
        except Exception as e:  # noqa: BLE001
            print("WARN: prune failed", d.name, e, file=sys.stderr)
    return removed


def run_backup(config_dir: Path, retention: int = 0) -> int:
    """对 ``config_dir`` 下直接子级 *.db 生成一份时间戳快照，并按需清理旧快照。"""
    if not config_dir.is_dir():
        print("ERROR: config 目录不存在:", config_dir, file=sys.stderr)
        return 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backups_dir = config_dir / "backups"
    out = backups_dir / ts
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    modes: dict[str, int] = {"sqlite": 0, "copy": 0}
    for pat in PATTERNS:
        for f in sorted(config_dir.glob(pat)):
            if f.parent != config_dir:
                continue
            dest = out / f.name
            mode = backup_one_file(f, dest)
            modes[mode] = modes.get(mode, 0) + 1
            n += 1
            print("backed up", f.name, "->", dest, f"({mode})")
    if n == 0:
        print("WARN: no *.db files under", config_dir)
        # 空快照目录清掉，避免留下空壳
        try:
            out.rmdir()
        except Exception:
            pass
        return 0
    print(
        "OK:", n, "files to", str(out),
        f"[sqlite={modes.get('sqlite', 0)} copy={modes.get('copy', 0)}]",
    )
    prune_old_backups(backups_dir, retention)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="备份 config 目录下的 SQLite 库（一致快照 + 保留策略）")
    ap.add_argument("--config-dir", default=str(CFG),
                    help="含 *.db 的 config 目录（双实例传实例数据根下 config）")
    ap.add_argument("--retention", type=int, default=0,
                    help="保留最近 N 份快照（<=0 不清理）")
    args = ap.parse_args(argv)
    return run_backup(Path(args.config_dir), retention=args.retention)


if __name__ == "__main__":
    raise SystemExit(main())
