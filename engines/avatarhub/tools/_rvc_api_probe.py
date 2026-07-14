# -*- coding: utf-8 -*-
"""探 RVC(6242) 有哪些端点可用：status/state/health 等，以及 config 当前值可否回读。"""
import json
import urllib.request

BASE = "http://127.0.0.1:6242"
for path in ("/status", "/state", "/health", "/getConfig", "/config", "/api/status",
             "/inputDevices"):
    try:
        with urllib.request.urlopen(BASE + path, timeout=4) as r:
            body = r.read().decode()[:200]
            print(f"GET {path} -> {r.status}: {body}")
    except Exception as e:
        print(f"GET {path} -> ERR {e}")
