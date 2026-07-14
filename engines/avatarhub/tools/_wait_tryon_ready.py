# -*- coding: utf-8 -*-
"""轮询 8002 /health 直到 model_loaded 或超时；打印最终 backend。"""
import sys, time
import requests

deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 300)
last = ""
while time.time() < deadline:
    try:
        j = requests.get("http://127.0.0.1:8002/health", timeout=3).json()
        last = f"backend={j.get('backend')} loaded={j.get('model_loaded')}"
        if j.get("model_loaded"):
            print("READY", last)
            sys.exit(0)
    except Exception as e:
        last = f"connecting... {str(e)[:60]}"
    time.sleep(5)
print("TIMEOUT", last)
sys.exit(1)
