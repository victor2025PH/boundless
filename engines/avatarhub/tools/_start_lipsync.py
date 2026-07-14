# -*- coding: utf-8 -*-
"""经 hub /api/engine/start 拉起本机 lipsync，并轮询 8090 健康。"""
import json
import time
import urllib.request


def post(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


try:
    print("start:", post("http://127.0.0.1:9000/api/engine/start?name=lipsync"))
except Exception as e:
    print("start err:", e)

deadline = time.time() + 240
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=4) as r:
            print("8090 health:", r.read().decode()[:200])
            break
    except Exception:
        time.sleep(5)
else:
    print("8090 TIMEOUT")
