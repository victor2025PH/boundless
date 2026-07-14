# -*- coding: utf-8 -*-
"""重启后 hub 在线验证：直播存活(收养) + 真传一张 PNG 走 /api/bg_images/upload 全链路。"""
import io
import sys

import requests
from PIL import Image

HUB = "http://127.0.0.1:9000"

j = requests.get(f"{HUB}/realtime/status", timeout=5).json()
print(f"[1] 直播存活: video_running={j.get('video_running')} "
      f"fps={(j.get('metrics') or {}).get('fps')} adopted={bool(j.get('orphan_adopted'))}")

b = io.BytesIO()
Image.new("RGB", (320, 180), (40, 120, 200)).save(b, "PNG")
r = requests.post(f"{HUB}/api/bg_images/upload",
                  files={"file": ("_upload_check.png", b.getvalue(), "image/png")}, timeout=10)
j2 = r.json()
print(f"[2] PNG 上传: HTTP {r.status_code} saved={j2.get('saved')}")

imgs = requests.get(f"{HUB}/api/bg_images", timeout=5).json().get("images", [])
print(f"[3] 清单: {imgs}")

ok = (j.get("video_running") is True and r.status_code == 200
      and j2.get("saved") == "_upload_check.png" and "_upload_check.png" in imgs)
print("RESULT: " + ("ALL PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
