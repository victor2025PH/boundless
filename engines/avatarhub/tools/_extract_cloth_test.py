# -*- coding: utf-8 -*-
"""截图抠衣双路径实测：
A 穿着照(人体解析)：FitDiT 示例模特照 → 应走 parsing;
B 平铺图杂色背景(背景差分)：白底服装合成到渐变背景 → 应走 bgdiff;
C 端到端：A 抠出的服装直接拿去试穿，证明产物可用。"""
import base64
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(r"c:\模仿音色")
T = "http://127.0.0.1:8002"


def b64f(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def save_b64(b64s: str, p: Path):
    p.write_bytes(base64.b64decode(b64s))


def extract(img64: str, tag: str, save_name: str = ""):
    t0 = time.time()
    r = requests.post(f"{T}/clothes/extract",
                      json={"image": img64, "save_name": save_name}, timeout=120)
    el = time.time() - t0
    j = r.json()
    if r.status_code != 200:
        print(f"[{tag}] FAIL {r.status_code} {str(j)[:200]}")
        return None
    out = BASE / "logs" / f"_extract_{tag}.jpg"
    save_b64(j["garment_image"], out)
    print(f"[{tag}] OK method={j['method']} coverage={j['coverage']} "
          f"{el:.1f}s saved={j.get('saved') or '-'} -> {out.name}")
    return j


def main():
    # A: 穿着照 → 人体解析
    worn = b64f(Path(r"C:\FitDiT\examples\model\0279.jpg"))
    ja = extract(worn, "worn_parsing", save_name="")

    # B: 白底服装贴到花背景 → 背景差分
    g = cv2.imdecode(np.frombuffer(Path(r"C:\FitDiT\examples\garment\0047.jpg").read_bytes(),
                                   np.uint8), cv2.IMREAD_COLOR)
    h, w = g.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.stack([(yy / h * 120 + 60), (xx / w * 90 + 80), np.full((h, w), 150)],
                  axis=2).astype(np.uint8)
    white = (g > 240).all(axis=2)                       # 白底区域 → 换成花背景
    comp = g.copy()
    comp[white] = bg[white]
    ok, buf = cv2.imencode(".jpg", comp, [cv2.IMWRITE_JPEG_QUALITY, 92])
    (BASE / "logs" / "_extract_busy_input.jpg").write_bytes(buf.tobytes())
    jb = extract(base64.b64encode(buf.tobytes()).decode(), "flat_bgdiff")

    # C: 端到端——A 的产物直接试穿
    if ja:
        person = b64f(Path(r"C:\FitDiT\examples\model\0083.jpg"))
        t0 = time.time()
        r = requests.post(f"{T}/tryon",
                          json={"person_image": person,
                                "cloth_image": ja["garment_image"]}, timeout=300)
        if r.status_code == 200:
            save_b64(r.json()["result_image"], BASE / "logs" / "_extract_tryon_e2e.jpg")
            print(f"[e2e] OK {time.time()-t0:.1f}s -> _extract_tryon_e2e.jpg")
        else:
            print(f"[e2e] FAIL {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    main()
