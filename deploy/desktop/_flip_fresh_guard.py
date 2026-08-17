"""Flip the fresh_guard stopgap back on in a seat's config.local.yaml (v1.024).

Context (SEAT_LOG_MONITOR_LOG.md 2026-08-13): v1.023's store.upsert_draft pinned
draft created_at -> fresh_guard deadlocked autosend delivery; seats got a
stopgap `fresh_guard.enabled: false`. v1.024 ships the store fix, so the guard
goes back on -- but ONLY on seats actually running v1.024 (tingxie stays 1.021
and must keep the stopgap).

Usage: python _flip_fresh_guard.py <local_yaml_path>
Edits in place. Exits: 0 flipped / 4 already true / 2 pattern not found
(refuses to guess -- the file shape changed, flip by hand).

Deliberately a dumb single-line text edit (not a YAML round-trip): the seat
file is hand-maintained with comments; rewriting the whole document risks
eating them (the 2026-08-01 overlay comment-loss lesson).
"""
import io
import re
import sys


def main() -> int:
    path = sys.argv[1]
    text = io.open(path, encoding="utf-8").read()
    if re.search(r"fresh_guard:\s*\n\s+enabled:\s*true", text):
        print("already true (nothing to do)")
        return 4
    pat = re.compile(
        r"(fresh_guard:\s*\n(\s+)enabled:)\s*false[^\n]*")
    m = pat.search(text)
    if not m:
        print("fresh_guard enabled:false block not found -- flip by hand")
        return 2
    new = (m.group(1) + " true  # 2026-08-13 v1.024 shipped the upsert "
           "created_at fix; stopgap lifted (SEAT_LOG_MONITOR_LOG.md)")
    text = text[:m.start()] + new + text[m.end():]
    io.open(path, "w", encoding="utf-8", newline="\n").write(text)
    print("flipped to true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
