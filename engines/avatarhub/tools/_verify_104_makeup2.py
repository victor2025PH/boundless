# -*- coding: utf-8 -*-
"""妆容层部署终验：不靠 openapi（.104 生成挂 500），直接打一发带 makeup 的换脸请求，
看响应里 makeup_ms 是否非空——运行中的进程真有新代码才会回这个字段。"""
import base64
import sys

import cv2
import numpy as np
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 本地 openapi 对照（同一份代码：本地也 500 说明是 schema 生成问题，与部署无关）
try:
    r = requests.get("http://127.0.0.1:8000/openapi.json", timeout=8)
    print("local  openapi:", r.status_code)
except Exception as e:
    print("local  openapi: err", str(e)[:60])
r = requests.get("http://192.168.0.104:8000/openapi.json", timeout=8)
print("remote openapi:", r.status_code)

# 功能级验证：合成一张带"脸"的图不现实，取项目里现成人脸图
from pathlib import Path
cand = sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))
img = cv2.imdecode(np.fromfile(str(cand[0]), np.uint8), cv2.IMREAD_COLOR)
ok, buf = cv2.imencode(".jpg", img)
b64 = base64.b64encode(buf).decode()

body = {"target_image": b64, "source_image": b64, "enhance": "none",
        "makeup": {"lip_color": [90, 60, 180], "lip": 0.5}}
rr = requests.post("http://192.168.0.104:8000/faceswap", json=body, timeout=60)
j = rr.json()
print("remote swap:", rr.status_code, "| detail:", str(j.get("detail"))[:100])
print("makeup_ms in response:", "makeup_ms" in j, "| value:", j.get("makeup_ms"),
      "| faces_used:", j.get("faces_used"))
