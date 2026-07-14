# -*- coding: utf-8 -*-
"""Phase 15 (2026-04-25): 一次性清洗 fb_contact_events 里被污染的 peer_name.

之前 _list_messenger_conversations 黑名单太短, "查看翻译" / "Reply" 等
Messenger UI 文本被当 peer_name 写进 contact_events 表. 现在 _is_valid_peer_name
已经修了源头, 但**已经入库的脏数据**还得清.

策略:
  - 扫 fb_contact_events 全表, 用新 _is_valid_peer_name 校验每行 peer_name
  - 对失败行打印 + 可选删除 (--delete) / 标记 (--mark-skipped)

用法:
  # 看一眼 (默认 dry-run, 不改 DB)
  python scripts/cleanup_dirty_peer_names.py

  # 实际删除
  python scripts/cleanup_dirty_peer_names.py --delete

  # 只看某 device
  python scripts/cleanup_dirty_peer_names.py --device 8DWOF6CYY5R8YHX8

零依赖 import 项目本体, 用 sqlite3 直接连 DB.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Phase 15 (2026-04-25): Windows cp936/gbk 撞 emoji/CJK → UTF-8 stdout.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true",
                     help="实际从 fb_contact_events 删除 (默认 dry-run)")
    ap.add_argument("--device", default=None,
                     help="只处理某 device_id (默认全部)")
    ap.add_argument("--limit", type=int, default=10000)
    ap.add_argument("--since-days", type=int, default=0,
                     help="Phase 15.1: 只处理近 N 天数据 (避免误删历史业务)")
    args = ap.parse_args()

    from src.app_automation.facebook import FacebookAutomation
    from src.host.database import _connect

    valid = FacebookAutomation._is_valid_peer_name

    sql = "SELECT id, device_id, peer_name, event_type, at FROM fb_contact_events"
    where = []
    sql_args = []
    if args.device:
        where.append("device_id = ?")
        sql_args.append(args.device)
    if args.since_days > 0:
        where.append("at >= datetime('now', ?)")
        sql_args.append(f"-{int(args.since_days)} days")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC LIMIT ?"
    sql_args.append(args.limit)

    dirty_ids = []
    by_event_type = {}
    by_peer_name = {}
    with _connect() as conn:
        for r in conn.execute(sql, sql_args).fetchall():
            pn = r["peer_name"] or ""
            if valid(pn):
                continue
            dirty_ids.append(r["id"])
            by_event_type[r["event_type"]] = by_event_type.get(
                r["event_type"], 0) + 1
            by_peer_name[pn] = by_peer_name.get(pn, 0) + 1

    print(f"# Phase 15 cleanup — DB={os.environ.get('DB_PATH', '(default)')}")
    print(f"# device_filter={args.device or '(全部)'}")
    print(f"# since_days={args.since_days or '(全历史)'}")
    print(f"# 扫描 limit={args.limit}, 找到脏行 {len(dirty_ids)} 条")
    print()
    print("by event_type:")
    for k, v in sorted(by_event_type.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    print()
    print("top 20 dirty peer_name (按出现次数):")
    for pn, n in sorted(by_peer_name.items(), key=lambda x: -x[1])[:20]:
        print(f"  {n:5d}  {pn[:40]}")

    if not dirty_ids:
        print("\n✓ 没有脏行需要清理.")
        return 0

    if not args.delete:
        print(f"\n[dry-run] 默认不改 DB. 加 --delete 实际删除 {len(dirty_ids)} 行.")
        return 0

    print(f"\n[delete] 删除 {len(dirty_ids)} 行...")
    with _connect() as conn:
        # 分批删除避免 SQLite param 限制
        BATCH = 500
        deleted = 0
        for i in range(0, len(dirty_ids), BATCH):
            batch = dirty_ids[i:i + BATCH]
            placeholders = ",".join(["?"] * len(batch))
            conn.execute(
                f"DELETE FROM fb_contact_events WHERE id IN ({placeholders})",
                batch,
            )
            deleted += len(batch)
        print(f"✓ 已删除 {deleted} 行.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
