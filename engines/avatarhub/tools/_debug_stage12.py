# -*- coding: utf-8 -*-
"""④ 存照回退失败复现：最小序列 建角色→传照试衣→无照试衣，打印完整错误。"""
import base64
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HUB = "http://127.0.0.1:9000"
NAME = "_dbg_存照"

face_b64 = base64.b64encode(
    sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))[5].read_bytes()).decode()
person_b64 = base64.b64encode(
    sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[10].read_bytes()).decode()

try:
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
except Exception:
    pass
r = requests.post(f"{HUB}/profiles", json={"name": NAME, "face_b64": face_b64}, timeout=15)
print("create:", r.status_code)

try:
    r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                      json={"person_image_b64": person_b64, "cloth_name": "演示上衣001",
                            "animate": "off"}, timeout=300)
    print("tryon with photo:", r.status_code, str(r.json())[:150])
    bp = Path(r"c:\模仿音色\data\body_photo") / f"{NAME}.jpg"
    print("stored file exists:", bp.exists(), bp.stat().st_size if bp.exists() else 0)

    r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                      json={"cloth_name": "演示上衣002", "animate": "off"}, timeout=300)
    print("tryon no photo:", r.status_code, str(r.json())[:300])
finally:
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
