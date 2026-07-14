# -*- coding: utf-8 -*-
"""探测各 MSMF 摄像头: 分辨率 + 平均亮度, 用于确认 BestCam(1920x1080) 非黑屏。"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import cv2

for idx in range(6):
    cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
    try:
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 2500)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2500)
    except Exception:
        pass
    if not cap.isOpened():
        print(f"idx {idx}: (打不开)")
        cap.release()
        continue
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    ok, frame = cap.read()
    if ok and frame is not None:
        mean = float(frame.mean())
        print(f"idx {idx}: {w}x{h}  read=OK  mean_brightness={mean:.1f}")
    else:
        print(f"idx {idx}: {w}x{h}  read=FAIL")
    cap.release()
print("done")
