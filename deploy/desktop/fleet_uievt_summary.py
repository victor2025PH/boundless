#!/usr/bin/env python3
"""Summarize a seat machine's ui_event_trend.db snapshot (fleet telemetry pull).

Called by chatx_fleet_status.ps1 -Telemetry after it scp'd the seat's daily
trend DB to a local temp path. Read-only (mode=ro URI on the copied snapshot,
never the live seat DB).

Usage:  python fleet_uievt_summary.py <db_path> <days> [prefix]
        (prefix optional = all actions. PS 5.1 DROPS empty-string args when
        invoking native executables, so the caller omits it instead of "".)
Output: one line per action, pipe-separated:  action|total_last_N_days|today
Exit codes: 0 = ok (possibly zero rows), 3 = trend table absent (store never
wrote on that seat -- distinct from "no events in window", report it honestly).

Day keys in the DB are UTC %Y-%m-%d (written by src/web/ui_event_trend.py's
_day_str via time.gmtime) -- "today" here uses the same UTC convention.

WAL note (bit us live on the first pull): the seat store runs journal_mode=WAL,
so a fresh boot may hold schema+rows entirely in the -wal sidecar. The fleet
script copies .db/.db-wal/.db-shm together, and we deliberately open the local
snapshot WITHOUT mode=ro so sqlite recovers the WAL into our private copy.
"""
from __future__ import annotations

import sqlite3
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fleet_uievt_summary.py <db_path> <days> [prefix]",
              file=sys.stderr)
        return 2
    db, days = sys.argv[1], int(sys.argv[2])
    prefix = sys.argv[3] if len(sys.argv) >= 4 else ""
    day0 = time.strftime("%Y-%m-%d",
                         time.gmtime(time.time() - (max(1, days) - 1) * 86400))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    con = sqlite3.connect(db)
    try:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ui_event_trend_daily'").fetchone()
        if not has:
            return 3
        rows = con.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE ?",
            (day0, prefix + "%")).fetchall()
    finally:
        con.close()
    agg: dict[str, list[int]] = {}
    for day, action, n in rows:
        slot = agg.setdefault(str(action), [0, 0])
        slot[0] += int(n)
        if str(day) == today:
            slot[1] += int(n)
    for action in sorted(agg):
        total, tod = agg[action]
        print("%s|%d|%d" % (action, total, tod))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
