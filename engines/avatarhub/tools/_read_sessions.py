# -*- coding: utf-8 -*-
"""一次性：human-readable 打印 swap_sessions.jsonl（PS 内联多行脚本转义太折腾）。"""
import io
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
lines = [l for l in io.open("logs/swap_sessions.jsonl", encoding="utf-8").read().splitlines() if l.strip()]
print("ledger lines:", len(lines))
for l in lines:
    j = json.loads(l)
    f = time.strftime("%H:%M:%S", time.localtime(j["start"]))
    t = time.strftime("%H:%M:%S", time.localtime(j["end"]))
    print(f"{f}-{t} dur={j['dur_s']}s samples={j['samples']} miss={j['miss_samples']} "
          f"latmed={j['latency_ms']['med']} p95={j['latency_ms']['p95']} crop={j['crop']['hit_pct']}% "
          f"fail={j['swap']['fail_pct']}% degr={j['degraded_pct']}% recovered={j.get('recovered')}")
    print("  states:", j["states"], " presets:", j["presets"], " enh:", j["enhance"])
