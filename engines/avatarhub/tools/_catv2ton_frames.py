# -*- coding: utf-8 -*-
"""从 PoC 结果视频抽帧（imencode+write_bytes 绕中文路径 cv2.imwrite 静默失败坑）。
并排：源视频帧 | 试衣结果帧，首/中/尾三组。"""
import sys
from pathlib import Path

import cv2
import numpy as np


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OUT = Path(r"c:\模仿音色\logs\catv2ton_poc")


def read_frames(p):
    cap = cv2.VideoCapture(str(p))
    fr = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        fr.append(f)
    cap.release()
    return fr


src = read_frames(OUT / "person_motion.mp4")
res = read_frames(OUT / "tryon_result.mp4")
n = min(len(src), len(res))
print(f"src {len(src)} frames, result {len(res)} frames")
for tag, i in (("first", 0), ("mid", n // 2), ("last", n - 1)):
    s = cv2.resize(src[i], (res[i].shape[1], res[i].shape[0]))
    pair = np.hstack([s, res[i]])
    ok, buf = cv2.imencode(".jpg", pair, [cv2.IMWRITE_JPEG_QUALITY, 92])
    (OUT / f"pair_{tag}.jpg").write_bytes(buf.tobytes())
    print("saved", f"pair_{tag}.jpg")
# 衣区放大对比条：结果的连续 6 帧同一裁剪，看时序闪烁
h, w = res[0].shape[:2]
crop = [r[int(h * 0.25):int(h * 0.7), int(w * 0.2):int(w * 0.8)] for r in res[n // 2:n // 2 + 6]]
strip = np.hstack(crop)
ok, buf = cv2.imencode(".jpg", strip, [cv2.IMWRITE_JPEG_QUALITY, 92])
(OUT / "strip_6frames.jpg").write_bytes(buf.tobytes())
print("saved strip_6frames.jpg")
