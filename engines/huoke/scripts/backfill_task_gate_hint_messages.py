# -*- coding: utf-8 -*-
"""
一次性回填：扫描 SQLite tasks.result JSON，对含 gate_evaluation.hint_code 但缺 hint_message 的记录
写入 hint_message（与运行时 resolve_gate_hint_message 一致）。默认 dry-run，加 --apply 才写库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from src.host.database import DB_PATH
    from src.host.task_dispatch_gate import result_dict_with_gate_hints

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="实际写库（否则仅统计）")
    args = p.parse_args()

    db = Path(DB_PATH)
    if not db.exists():
        print(f"数据库不存在: {db}")
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT task_id, result FROM tasks WHERE result IS NOT NULL AND result != ''"
    ).fetchall()

    would_update = 0
    updated = 0
    for row in rows:
        raw = row["result"]
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        merged = result_dict_with_gate_hints(data)
        if merged is data:
            continue
        would_update += 1
        if args.apply:
            conn.execute(
                "UPDATE tasks SET result = ? WHERE task_id = ?",
                (json.dumps(merged, ensure_ascii=False), row["task_id"]),
            )
            updated += 1

    if args.apply:
        conn.commit()
        print(f"已更新 {updated} 条任务 result（共扫描 {len(rows)} 条）")
    else:
        conn.rollback()
        print(f"dry-run：将可更新 {would_update} 条（共扫描 {len(rows)} 条），加 --apply 执行写库")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
