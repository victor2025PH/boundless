# -*- coding: utf-8 -*-
"""激活收尾验证：.104 换脸机 openapi 是否含 makeup 字段（直播妆容层上线判据）。"""
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

r = requests.get("http://192.168.0.104:8000/openapi.json", timeout=10)
print(".104 openapi:", r.status_code, "| makeup field:", "makeup" in r.text)
h = requests.get("http://192.168.0.104:8000/health", timeout=6)
print(".104 health:", h.status_code, h.text[:120])
