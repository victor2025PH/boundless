# -*- coding: utf-8 -*-
"""量化实体绿幕的绿色度分布（校准 BG_CHROMA_T0/T1 用）：取最近一帧 raw，
人工框出绿幕区(画面中上部)与皮肤区(人脸)，对比 g-max(r,b) 分布。"""
import numpy as np
import cv2

raw = cv2.imdecode(np.fromfile(r"C:\模仿音色\logs\_bg_probe\frame_raw.png", np.uint8),
                   cv2.IMREAD_COLOR)
h, w = raw.shape[:2]
print(f"raw {raw.shape}")


def stats(label, roi):
    b, g, r = cv2.split(roi)
    gn = cv2.subtract(g, cv2.max(b, r)).astype(np.float32)
    q = np.percentile(gn, [5, 25, 50, 75, 95])
    print(f"{label:12s} 绿色度 p5={q[0]:5.1f} p25={q[1]:5.1f} p50={q[2]:5.1f} "
          f"p75={q[3]:5.1f} p95={q[4]:5.1f}  >12占比={(gn > 12).mean() * 100:4.1f}%  "
          f">25占比={(gn > 25).mean() * 100:4.1f}%")


# 绿幕采样区：上部左右两条（避开人物中央），及人物左侧那条出问题的窄带
stats("绿幕-左上", raw[40:200, int(w * 0.28):int(w * 0.42)])
stats("绿幕-右上", raw[40:200, int(w * 0.60):int(w * 0.80)])
stats("绿幕-左中", raw[int(h * 0.35):int(h * 0.6), int(w * 0.30):int(w * 0.38)])
stats("皮肤-脸",   raw[int(h * 0.32):int(h * 0.5), int(w * 0.44):int(w * 0.56)])
