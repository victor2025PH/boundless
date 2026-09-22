#!/usr/bin/env python3
"""按 registry / 默认 take 表批量重拼 E1–E12（字幕错峰后必跑）。

  python rebuild_all_episodes.py
  python rebuild_all_episodes.py --only E3,E7,E12
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"

DEFAULT_TAKE = {
    "E1": 426, "E2": 428, "E3": 429, "E4": 430, "E5": 431,
    "E6": 432, "E7": 433, "E8": 434, "E9": 435, "E10": 436, "E11": 437,
    "E12": 452,
}


def picks_from_registry() -> dict[str, int]:
    reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    out = dict(DEFAULT_TAKE)
    for it in reg.get("items") or []:
        eid = it.get("id") or ""
        if eid in out and it.get("picked_take"):
            out[eid] = int(it["picked_take"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    takes = picks_from_registry()
    eps = list(takes)
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        eps = [e for e in eps if e in want]

    fails = []
    for eid in eps:
        take = takes[eid]
        print(f"== rebuild {eid} take={take}")
        r = subprocess.run(
            [sys.executable, "make_episode.py", eid, "--take", str(take)],
            cwd=str(ROOT),
        )
        if r.returncode != 0:
            fails.append(f"{eid}:make_episode")
            continue
        mp4 = OUT / eid / f"{eid}_h{take}_episode.mp4"
        if not mp4.exists():
            fails.append(f"{eid}:missing mp4")
    print("== rebuild_all_episodes ==")
    print("RESULT", "FAIL" if fails else "PASS", fails or "")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
