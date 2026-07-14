# -*- coding: utf-8 -*-
"""等 hub(9000) 起来：轮询 /rvc/devices 至多 N 秒，起来即退出 0。"""
import sys, time
import requests

deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 150)
i = 0
while time.time() < deadline:
    i += 1
    try:
        r = requests.get("http://127.0.0.1:9000/rvc/devices", timeout=4)
        if r.status_code == 200 and r.json().get("ok"):
            print(f"HUB_UP after ~{i} polls; source={r.json().get('source')}")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(3)
print("HUB_TIMEOUT")
sys.exit(1)
