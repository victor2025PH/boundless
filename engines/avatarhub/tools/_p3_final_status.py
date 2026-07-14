# -*- coding: utf-8 -*-
"""P3 收尾巡检：hub/RVC 在线 + 偏好文件含 history + 热切端点应答结构。"""
import json
from pathlib import Path

import requests

hub = "http://127.0.0.1:9000"
d = requests.get(hub + "/rvc/devices", timeout=10).json()
print("rvc/devices ok=%s src=%s in=%s out=%s" % (
    d.get("ok"), d.get("src"),
    len(d.get("inputs") or d.get("input_devices") or []),
    len(d.get("outputs") or d.get("output_devices") or [])))
p = json.loads(Path(r"C:\模仿音色\audio_prefs.json").read_text(encoding="utf-8"))
hist = p.get("history") or []
print("audio_prefs keys=%s history=%d 条" % (sorted(k for k in p if k != "history"), len(hist)))
if hist:
    last = hist[-1]
    print("  最近一条: side=%s src=%s to=%s" % (last.get("side"), last.get("src"), str(last.get("to"))[:40]))
r = requests.get("http://127.0.0.1:6242/inputDevices", timeout=10)
print("RVC /inputDevices %s n=%d" % (r.status_code, len(r.json())))
st = requests.get(hub + "/realtime/status", timeout=5).json()
print("realtime running=%s（收尾应为 False）" % st.get("running"))
