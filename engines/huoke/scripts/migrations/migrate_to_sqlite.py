#!/usr/bin/env python3
"""
One-time migration: tiktok_state.json + stats.json → DeviceStateStore (SQLite).

Safe to run multiple times — uses upsert logic (won't overwrite newer SQLite data).

Usage:
    python migrate_to_sqlite.py           # dry-run (preview)
    python migrate_to_sqlite.py --apply   # actually migrate
"""

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.host.database import init_db
from src.host.device_state import get_device_state_store

STATE_FILE = project_root / "data" / "tiktok_state.json"
STATS_FILE = project_root / "data" / "stats.json"


def migrate(dry_run: bool = True):
    init_db()
    ds = get_device_state_store("tiktok")
    migrated = 0
    skipped = 0

    if STATE_FILE.exists():
        print(f"\n=== Migrating {STATE_FILE} ===")
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        for device_id, dev_state in state.items():
            existing_start = ds.get(device_id, "start_date")
            if existing_start:
                print(f"  [{device_id[:12]}] Already in SQLite (start={existing_start}), skipping state")
                skipped += 1
                continue

            fields = {
                "start_date": dev_state.get("start_date", ""),
                "phase": dev_state.get("phase", "cold_start"),
                "can_follow": dev_state.get("can_follow", False),
                "follow_test_failures": dev_state.get("follow_test_failures", 0),
                "follow_tested_days": dev_state.get("follow_tested_days", []),
                "follow_unlocked_date": dev_state.get("follow_unlocked_date", ""),
                "active_days_following": dev_state.get("active_days_following", 0),
            }

            print(f"  [{device_id[:12]}] phase={fields['phase']}, "
                  f"can_follow={fields['can_follow']}, "
                  f"start={fields['start_date'][:10]}")

            if not dry_run:
                for k, v in fields.items():
                    ds.set(device_id, k, v)
            migrated += 1

    if STATS_FILE.exists():
        print(f"\n=== Migrating {STATS_FILE} ===")
        stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))

        for device_id, dev_stats in stats.get("devices", {}).items():
            existing_watched = ds.get_int(device_id, "total_watched")
            json_watched = dev_stats.get("total_watched", 0)

            if existing_watched >= json_watched and existing_watched > 0:
                print(f"  [{device_id[:12]}] SQLite has {existing_watched} watched >= JSON {json_watched}, skipping stats")
                skipped += 1
                continue

            stat_fields = {
                "total_watched": dev_stats.get("total_watched", 0),
                "total_liked": dev_stats.get("total_liked", 0),
                "total_followed": dev_stats.get("total_followed", 0),
                "total_follow_backs": dev_stats.get("total_follow_backs", 0),
                "total_dms_sent": dev_stats.get("total_dms_sent", 0),
                "total_comments": dev_stats.get("total_comments", 0),
            }

            print(f"  [{device_id[:12]}] watched={stat_fields['total_watched']}, "
                  f"liked={stat_fields['total_liked']}, "
                  f"followed={stat_fields['total_followed']}")

            if not dry_run:
                for k, v in stat_fields.items():
                    ds.set(device_id, k, v)

            for day, day_stats in dev_stats.get("daily", {}).items():
                for metric, val in day_stats.items():
                    if val > 0:
                        key = f"daily:{day}:{metric}"
                        if not dry_run:
                            ds.set(device_id, key, val)
                        migrated += 1

    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}Migration complete: {migrated} records migrated, {skipped} skipped")

    if dry_run:
        print("\nRun with --apply to actually migrate:")
        print("  python migrate_to_sqlite.py --apply")

    if not dry_run:
        for f, name in [(STATE_FILE, "tiktok_state.json"), (STATS_FILE, "stats.json")]:
            if f.exists():
                backup = f.with_suffix(".json.bak")
                f.rename(backup)
                print(f"  Backed up {name} → {backup.name}")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    migrate(dry_run=not apply)
