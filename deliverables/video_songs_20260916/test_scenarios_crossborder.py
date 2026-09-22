#!/usr/bin/env python3
"""跨境询盘场景闸 C1。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lyric_gate import gate_file  # noqa: E402

FAILS: list[str] = []


def main() -> int:
    sc = json.loads((ROOT / "scenarios_crossborder.json").read_text(encoding="utf-8"))
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    eps = {e["id"]: e for e in cur["episodes"]}
    for item in sc["episodes"]:
        eid = item["id"]
        ep = eps.get(eid)
        if not ep:
            FAILS.append(f"{eid}: curriculum 缺")
            continue
        if ep.get("scenario") != "crossborder":
            FAILS.append(f"{eid}: scenario 应为 crossborder")
        steps = []
        for c in ep.get("footage_actions") or []:
            steps.extend(c.get("steps") or [])
        if not any(st[:2] == ["open_chat", "BOUNDLESS"] for st in steps):
            FAILS.append(f"{eid}: 须 open_chat BOUNDLESS（跨境英文询盘）")
        if not any(st[:2] == ["click_css", "#xlate-toggle-btn"] for st in steps):
            FAILS.append(f"{eid}: 须点翻译")
        if not any(st[:2] == ["point_css", "#ai-reply-btn"] for st in steps):
            FAILS.append(f"{eid}: 须得点 AI回复")
        for key in ("rap", "intro"):
            p = ROOT / item[key]
            fails, _ = gate_file(p, lang="zh", audience="public")
            for f in fails:
                FAILS.append(f"{eid}/{p.name}: {f}")
    print("== test_scenarios_crossborder ==")
    for f in FAILS:
        print("FAIL", f)
    print("RESULT", "FAIL" if FAILS else "PASS", f"({len(FAILS)})" if FAILS else "")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
