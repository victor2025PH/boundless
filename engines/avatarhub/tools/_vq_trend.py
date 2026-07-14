# -*- coding: utf-8 -*-
"""voice_quality.jsonl 中某角色的 cos 历史趋势（判断今天的回归是漂移还是骤降）。"""
import json
import sys
import time
from pathlib import Path

name = sys.argv[1] if len(sys.argv) > 1 else "刘德华"
p = Path(__file__).resolve().parent.parent / "logs" / "voice_quality.jsonl"
lines = p.read_text(encoding="utf-8").splitlines()
print("total lines:", len(lines))
if lines:
    print("sample keys:", sorted(json.loads(lines[-1]).keys()))
for ln in lines:
    try:
        d = json.loads(ln)
    except Exception:
        continue
    ts = time.strftime("%m-%d %H:%M", time.localtime(d.get("ts", 0)))
    profs = d.get("profiles")
    if isinstance(profs, dict) and name in profs:
        print(ts, json.dumps(profs[name], ensure_ascii=False)[:160])
    elif isinstance(profs, list):
        for r in profs:
            if isinstance(r, dict) and (r.get("profile") == name or r.get("name") == name):
                print(ts, json.dumps(r, ensure_ascii=False)[:160])
