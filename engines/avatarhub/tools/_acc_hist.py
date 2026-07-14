# -*- coding: utf-8 -*-
"""验收历史速览：最近 N 轮各项 PASS/FAIL 矩阵（判断失败是否 P2 之前就存在）。"""
import json
import time
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "logs" / "acceptance_history.jsonl"
rows = []
for ln in p.read_text(encoding="utf-8").splitlines():
    try:
        rows.append(json.loads(ln))
    except Exception:
        pass
for r in rows[-8:]:
    ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
    marks = []
    for it in r.get("results", []):
        s = "S" if it.get("skipped") else ("P" if it.get("ok") else "F")
        marks.append(f"{it.get('key')}:{s}")
    print(ts, f"{r.get('pass')}/{r.get('total')}", " ".join(marks))
