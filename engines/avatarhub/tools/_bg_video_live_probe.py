# -*- coding: utf-8 -*-
"""线上视频背景取证：热切到测试视频 → 隔 1.2s 抓两帧 /swapped → 断言背景在动且人像在
→ 恢复原背景。产物 video_live_t0.png / video_live_t1.png / video_live_pair.png"""
import sys
import time
from pathlib import Path

import numpy as np
import cv2
import requests

OUT = Path(r"C:\模仿音色\logs\_bg_probe")
RT = "http://127.0.0.1:8080"
VID = str(OUT / "测试动态背景.mp4")
ORIG = "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp"


def grab(path="/swapped"):
    r = requests.get(RT + path, stream=True, timeout=8)
    buf = b""
    t0 = time.time()
    for chunk in r.iter_content(8192):
        buf += chunk
        a = buf.find(b"\xff\xd8")
        b_ = buf.find(b"\xff\xd9", a + 2)
        if a >= 0 and b_ > a:
            fr = cv2.imdecode(np.frombuffer(buf[a:b_ + 2], np.uint8), cv2.IMREAD_COLOR)
            if fr is not None:
                r.close()
                return fr
            buf = buf[b_ + 2:]
        if time.time() - t0 > 8:
            break
    r.close()
    return None


def set_bg(image):
    r = requests.get(RT + "/bg/set", params={"mode": "image", "image": image}, timeout=8)
    return r.json()


st = set_bg(VID)
print(f"[set] ok={st.get('ok')} kind={st.get('image_kind')} image={st.get('image', '')[-24:]}")
assert st.get("ok"), st
time.sleep(1.0)                      # 视频背景首帧就位
f0 = grab()
time.sleep(1.2)
f1 = grab()
try:
    assert f0 is not None and f1 is not None, "抓帧失败"
    diff = float(np.abs(f0.astype(int) - f1.astype(int)).mean())
    # 上角背景区（人像在中央,角落必是背景）两帧差异
    c0 = np.abs(f0[:80, :240].astype(int) - f1[:80, :240].astype(int)).mean()
    c1 = np.abs(f0[:80, -240:].astype(int) - f1[:80, -240:].astype(int)).mean()
    print(f"[live] 全帧平均差={diff:.1f} 左上角差={c0:.1f} 右上角差={c1:.1f}（角差>10=背景在动）")
    cv2.imencode(".png", f0)[1].tofile(str(OUT / "video_live_t0.png"))
    cv2.imencode(".png", f1)[1].tofile(str(OUT / "video_live_t1.png"))
    cv2.imencode(".png", np.hstack([f0, f1]))[1].tofile(str(OUT / "video_live_pair.png"))
    ok = c0 > 10 and c1 > 10
finally:
    back = set_bg(ORIG)
    print(f"[restore] ok={back.get('ok')} image={back.get('image', '')[-24:]} kind={back.get('image_kind')}")
print("RESULT: " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
