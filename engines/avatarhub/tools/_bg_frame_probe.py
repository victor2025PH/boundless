# -*- coding: utf-8 -*-
"""虚拟背景现场取证：抓 /swapped 一帧 + 解码当前背景图 + 直接跑一遍 BackgroundReplacer，
比对角落均色判断背景是否真的替换了。产物存 logs/_bg_probe/ 供人眼复核。"""
import io
import os
import sys
import time
from pathlib import Path

import numpy as np
import cv2
import requests

BASE = Path(r"C:\模仿音色")
sys.path.insert(0, str(BASE))
OUT = BASE / "logs" / "_bg_probe"
OUT.mkdir(parents=True, exist_ok=True)

RT = "http://127.0.0.1:8080"
CAM = RT          # realtime_stream 内置 MJPEG 与控制端点同为 8080（/raw /swapped）

st = requests.get(f"{RT}/bg/status", timeout=5).json()
print(f"[status] mode={st.get('mode')} image={st.get('image')!r} ms={st.get('ms')} "
      f"engine={st.get('engine')!r} error={st.get('error')!r}")

img_path = BASE / "bg_images" / st.get("image", "")
bg = cv2.imdecode(np.fromfile(str(img_path), np.uint8), cv2.IMREAD_COLOR) if img_path.exists() else None
print(f"[bgfile] exists={img_path.exists()} decoded={'None' if bg is None else bg.shape}")
if bg is not None:
    cv2.imencode(".png", cv2.resize(bg, (480, int(bg.shape[0] * 480 / bg.shape[1]))))[1] \
        .tofile(str(OUT / "bg_image.png"))


def grab(path, fn):
    """从 mjpeg 流里抠一帧 jpeg。"""
    r = requests.get(f"{CAM}{path}", stream=True, timeout=8)
    buf = b""
    t0 = time.time()
    for chunk in r.iter_content(8192):
        buf += chunk
        a = buf.find(b"\xff\xd8")
        b_ = buf.find(b"\xff\xd9", a + 2)
        if a >= 0 and b_ > a:
            jpg = buf[a:b_ + 2]
            fr = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if fr is not None:
                cv2.imencode(".png", fr)[1].tofile(str(OUT / fn))
                r.close()
                return fr
            buf = buf[b_ + 2:]
        if time.time() - t0 > 8:
            break
    r.close()
    return None


sw = grab("/swapped", "frame_swapped.png")
rw = grab("/raw", "frame_raw.png")
print(f"[frames] swapped={'None' if sw is None else sw.shape} raw={'None' if rw is None else rw.shape}")


def corners(f):
    h, w = f.shape[:2]
    s = 40
    return {k: f[y:y + s, x:x + s].reshape(-1, 3).mean(0).round(0).tolist()
            for k, (y, x) in {"左上": (8, 8), "右上": (8, w - s - 8),
                              "左下": (h - s - 8, 8), "右下": (h - s - 8, w - s - 8)}.items()}


if sw is not None and rw is not None:
    cs, cr = corners(sw), corners(rw)
    for k in cs:
        d = float(np.abs(np.array(cs[k]) - np.array(cr[k])).mean())
        print(f"[corner] {k}: swapped={cs[k]} raw={cr[k]} 平均差={d:.0f}")

# 离线复演：同一个类直接吃 raw 帧，验证 bg_replace 本身工作与否（与直播进程隔离）
if rw is not None and bg is not None:
    import bg_replace
    from bg_replace import BackgroundReplacer
    bg_replace.SETTINGS_PATH = str(OUT / "_probe_settings.json")   # 隔离持久化,不污染直播配置
    b2 = BackgroundReplacer()
    b2.set_config(mode="image", image=st.get("image"))
    t0 = time.time()
    out = b2.process(rw.copy())
    for _ in range(4):
        out = b2.process(rw.copy())
    print(f"[offline] process ok, {((time.time()-t0)/5)*1000:.1f}ms/帧, "
          f"status={ {k: b2.status()[k] for k in ('engine','error')} }")
    cv2.imencode(".png", out)[1].tofile(str(OUT / "frame_offline_replaced.png"))
    d = float(np.abs(out.astype(np.int16) - rw.astype(np.int16)).mean())
    print(f"[offline] 离线替换后与 raw 平均像素差={d:.1f}（>5 说明替换在生效）")

print(f"产物目录: {OUT}")
