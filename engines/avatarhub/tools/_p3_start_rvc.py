# -*- coding: utf-8 -*-
"""拉起 RVC API(6242) 并等它就绪。"""
import time

import requests

print("start_rvc_api →", requests.post("http://127.0.0.1:9000/realtime/start_rvc_api", timeout=20).json())
for i in range(40):
    time.sleep(3)
    try:
        r = requests.get("http://127.0.0.1:6242/inputDevices", timeout=6)
        if r.status_code == 200:
            print(f"RVC_UP after ~{(i+1)*3}s, n={len(r.json())}")
            break
    except Exception:
        pass
else:
    print("RVC_TIMEOUT")
