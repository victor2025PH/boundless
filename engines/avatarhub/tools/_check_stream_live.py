# -*- coding: utf-8 -*-
import json, time, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
d = json.load(open(r"c:\模仿音色\realtime_status.json", encoding="utf-8"))
age = time.time() - d["ts"]
print(f"age={age:.0f}s swap_ok={d['swap_ok']} recent={d['swap_recent']}")
print("LIVE" if age < 30 else "IDLE")
