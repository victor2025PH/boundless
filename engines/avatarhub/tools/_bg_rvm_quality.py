# -*- coding: utf-8 -*-
"""用带人像的存档帧(frame_raw.png, 18:32 抓取)做 RVM vs MediaPipe 画质对比。"""
import sys
import time
from pathlib import Path

import numpy as np
import cv2

BASE = Path(r"C:\模仿音色")
sys.path.insert(0, str(BASE))
OUT = BASE / "logs" / "_bg_probe"

import bg_replace
from bg_replace import BackgroundReplacer

bg_replace.SETTINGS_PATH = str(OUT / "_rvm_ab_settings.json")
BG_IMG = "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp"

raw = cv2.imdecode(np.fromfile(str(OUT / "frame_raw.png"), np.uint8), cv2.IMREAD_COLOR)
raw = cv2.resize(raw, (1280, 720))
print("[raw]", raw.shape)


def run(pref):
    bg_replace._ENGINE = pref
    b = BackgroundReplacer()
    b.set_config(mode="image", image=BG_IMG)
    if pref == "rvm":
        t0 = time.time()
        while b._rvm is None and not b._rvm_failed and time.time() - t0 < 60:
            time.sleep(0.5)
        assert not b._rvm_failed, b._rvm_failed
    out = None
    for _ in range(8):
        out = b.process(raw.copy())
    km = b._key_masks(raw)[0]
    trans = int(((km > 0.05) & (km < 0.95)).sum())
    core = cv2.compare(km, 0.5, cv2.CMP_GT)
    x, y, w, h = cv2.boundingRect(core)
    x0, y0 = max(0, x - 30), max(0, y - 30)
    x1, y1 = min(1280, x + w + 30), min(720, y + h + 30)
    bch, gch, rch = cv2.split(out[y0:y1, x0:x1])
    leak = int((cv2.subtract(gch, cv2.max(bch, rch)) > 25).sum())
    print(f"[{pref:9s}] engine={b._engine_live} 过渡px={trans} 残绿px={leak} bbox={(x, y, w, h)}")
    b.close()
    return out, (x, y, w, h)


out_mp, bbox = run("mediapipe")
out_rv, _ = run("rvm")

x, y, w, h = bbox
cx = x + w // 2
crop = (slice(max(0, y - 15), min(720, y + 265)), slice(max(0, cx - 160), min(1280, cx + 160)))
zoom = lambda img: cv2.resize(img[crop], None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
head = np.hstack([zoom(out_mp), zoom(out_rv)])
cv2.putText(head, "MediaPipe", (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 2)
cv2.putText(head, "RVM", (head.shape[1] // 2 + 12, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
full = np.vstack([out_mp, out_rv])
cv2.putText(full, "B MediaPipe+chroma (CPU)", (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 2)
cv2.putText(full, "C RVM neural matting (5090)", (12, 754), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
cv2.imencode(".png", head)[1].tofile(str(OUT / "rvm_person_head.png"))
cv2.imencode(".png", full)[1].tofile(str(OUT / "rvm_person_full.png"))
print("saved rvm_person_head.png / rvm_person_full.png")
