# -*- coding: utf-8 -*-
"""验收历史失败项明细：最近 6 轮，每轮列出失败 key 与摘要前 60 字。"""
import json
import time
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "logs" / "acceptance_history.jsonl"
rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
for r in rows[-6:]:
    ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
    print(f"== {ts}  {r.get('pass')}/{r.get('total')}  keys={sorted(r.keys())}")
    for it in (r.get("results") or r.get("items") or []):
        if isinstance(it, str):
            print("   item:", it[:90])
        elif not it.get("ok") and not it.get("skipped"):
            print("   FAIL", it.get("key"), "|", (it.get("summary") or "")[:70])
    if "fails" in r:
        print("   fails:", r["fails"])
