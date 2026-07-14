# -*- coding: utf-8 -*-
"""FitDiT 清晰度档位透传验证：768x1024 vs 1152x1536 各跑一张，记录时延。"""
import base64
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(r"c:\模仿音色")
person = base64.b64encode(Path(r"C:\FitDiT\examples\model\0083.jpg").read_bytes()).decode()
cloth = base64.b64encode(Path(r"C:\FitDiT\examples\garment\0012.jpg").read_bytes()).decode()

for res in ("768x1024", "1152x1536"):
    t0 = time.time()
    r = requests.post("http://127.0.0.1:8002/tryon",
                      json={"person_image": person, "cloth_image": cloth,
                            "resolution": res}, timeout=600)
    el = time.time() - t0
    j = r.json()
    if r.status_code == 200 and j.get("result_image"):
        img = base64.b64decode(j["result_image"])
        tag = res.replace("x", "_")
        (BASE / "logs" / f"_tryon_res_{tag}.jpg").write_bytes(img)
        print(f"{res}: OK {el:.1f}s ({len(img)//1024}KB)")
    else:
        print(f"{res}: FAIL {r.status_code} {str(j)[:200]}")
