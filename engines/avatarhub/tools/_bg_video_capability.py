# -*- coding: utf-8 -*-
"""实证：本机 OpenCV 对视频背景的能力面——
1) VideoWriter 能写哪种编码(mp4v?) 2) VideoCapture 能否解码 GIF
3) 中文路径下 VideoCapture 直开是否成功(决定要不要 ASCII 临时副本兜底)"""
import os
import numpy as np
import cv2
from PIL import Image

TMP_ASCII = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "bg_cap_test")
TMP_CN = r"C:\模仿音色\logs\_bg_probe\能力测试"
os.makedirs(TMP_ASCII, exist_ok=True)
os.makedirs(TMP_CN, exist_ok=True)


def mk_frames(n=12, w=320, h=180):
    fs = []
    for i in range(n):
        f = np.zeros((h, w, 3), np.uint8)
        f[:, :, 0] = int(255 * i / n)
        cv2.circle(f, (20 + i * 20, h // 2), 15, (0, 200, 255), -1)
        fs.append(f)
    return fs


frames = mk_frames()

# 1) VideoWriter 编码可用性
for cc in ("mp4v", "XVID", "MJPG"):
    p = os.path.join(TMP_ASCII, f"t_{cc}.mp4" if cc == "mp4v" else f"t_{cc}.avi")
    vw = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*cc), 12, (320, 180))
    ok = vw.isOpened()
    if ok:
        for f in frames:
            vw.write(f)
    vw.release()
    sz = os.path.getsize(p) if os.path.exists(p) else 0
    print(f"[writer] {cc}: opened={ok} bytes={sz}")

# 2) GIF 解码（PIL 生成 → VideoCapture 读）
gif_p = os.path.join(TMP_ASCII, "t.gif")
Image.new("RGB", (320, 180))
ims = [Image.fromarray(f[:, :, ::-1]) for f in frames]
ims[0].save(gif_p, save_all=True, append_images=ims[1:], duration=80, loop=0)
cap = cv2.VideoCapture(gif_p)
n = 0
while True:
    r, f = cap.read()
    if not r:
        break
    n += 1
print(f"[gif] VideoCapture opened={cap.isOpened()} frames_read={n} "
      f"fps={cap.get(cv2.CAP_PROP_FPS):.1f}")
cap.release()

# 3) 中文路径 mp4 / gif 直开
import shutil
mp4_cn = os.path.join(TMP_CN, "测试视频.mp4")
gif_cn = os.path.join(TMP_CN, "测试动图.gif")
shutil.copy(os.path.join(TMP_ASCII, "t_mp4v.mp4"), mp4_cn)
shutil.copy(gif_p, gif_cn)
for p in (mp4_cn, gif_cn):
    cap = cv2.VideoCapture(p)
    r, f = cap.read()
    print(f"[中文路径] {os.path.basename(p)}: opened={cap.isOpened()} first_frame={r and f is not None}")
    cap.release()
