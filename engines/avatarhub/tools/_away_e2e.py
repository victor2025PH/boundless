# -*- coding: utf-8 -*-
"""06u E2E：离席画面自定义——热切(经 hub 代理)→状态回读→真离席出品牌图→style=off 冻结帧。
   隔离布景：synth_cam(8094)→realtime_stream(8080,生产口·仅生产空闲时跑)；hub(9000)须已带 /realtime/swap/away。
   通过判据:
     [1] 代理热切 style=image+text+image 路径回读一致
     [2] /swap/status.params.away_style/text/image 同步
     [3] 断供 >AFTER+5s 后抓帧 ≈ 品牌图(上 60% 区域 MAD<30,避开角标区)
     [4] style=off 后抓帧 ≠ 品牌图(冻结帧)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\模仿音色")
PY = sys.executable
HUB = "http://127.0.0.1:9000"
RT = "http://127.0.0.1:8080"
CAM_PORT = 8094
BRAND = str(BASE / "bg_images" / "_smoke_bg.jpg")

import cv2


def wait_http(url, timeout_s, desc):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                print(f"  {desc} 就绪 ({time.time() - t0:.0f}s)", flush=True)
                return True
        except Exception:
            pass
        time.sleep(1.5)
    print(f"  !! {desc} {timeout_s}s 未就绪", flush=True)
    return False


def grab_frame(timeout=10):
    """从 MJPEG /swapped(=vcam 最终帧,观众同源,含离席画面) 抓一帧 JPEG → BGR ndarray。"""
    r = requests.get(f"{RT}/swapped", stream=True, timeout=timeout)
    buf = b""
    for chunk in r.iter_content(8192):
        buf += chunk
        a, b = buf.find(b"\xff\xd8"), buf.find(b"\xff\xd9")
        if a != -1 and b != -1 and b > a:
            img = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            r.close()
            return img
        if len(buf) > 8 * 1024 * 1024:
            break
    r.close()
    return None


def cover(img, w, h):
    sh, sw = img.shape[:2]
    sc = max(w / sw, h / sh)
    rw, rh = max(2, int(sw * sc)), max(2, int(sh * sc))
    im = cv2.resize(img, (rw, rh))
    x0, y0 = (rw - w) // 2, (rh - h) // 2
    return im[y0:y0 + h, x0:x0 + w]


def mad_top(a, b, frac=0.6):
    h = int(a.shape[0] * frac)
    return float(np.mean(np.abs(a[:h].astype(np.float32) - b[:h].astype(np.float32))))


def main():
    if not Path(BRAND).exists():
        print(f"FATAL: 品牌图缺失 {BRAND}")
        return 2
    try:
        requests.get(f"{RT}/swap/status", timeout=2)
        print("FATAL: 8080 已有实例在跑(生产在播?),拒绝 E2E")
        return 2
    except Exception:
        pass

    procs = []
    verdicts = {}
    try:
        print("[1/5] 拉起 synth_cam(8094) + realtime_stream(8080, AWAY_AFTER=4)…", flush=True)
        procs.append(subprocess.Popen(
            [PY, str(BASE / "tools" / "synth_cam.py"), "--width", "960", "--height", "540",
             "--fps", "15", "--port", str(CAM_PORT)],
            cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
        if not wait_http(f"http://127.0.0.1:{CAM_PORT}/health", 20, "synth_cam"):
            return 1
        env = dict(os.environ)
        env.update({"SWAP_AWAY_AFTER": "4", "SWAP_STATS": "0", "PYTHONIOENCODING": "utf-8"})
        procs.append(subprocess.Popen(
            [PY, str(BASE / "realtime_stream.py"), "--source", f"http://127.0.0.1:{CAM_PORT}/stream",
             "--width", "960", "--height", "540", "--swap-preset", "eco", "--no-preview",
             "--mjpeg-port", "8080"],
            cwd=str(BASE), env=env,
            stdout=open(BASE / "logs" / "rt_away_e2e.log", "w", encoding="utf-8"),
            stderr=subprocess.STDOUT))
        if not wait_http(f"{RT}/swap/status", 60, "realtime_stream"):
            return 1

        print("[2/5] 经 hub 代理热切 style=image + 文案 + 品牌图…", flush=True)
        j = requests.get(f"{HUB}/realtime/swap/away",
                         params={"style": "image", "text": "马上回来 · BRB", "image": BRAND},
                         timeout=6).json()
        verdicts["proxy_roundtrip"] = (j.get("ok") is True and j.get("style") == "image"
                                       and j.get("text") == "马上回来 · BRB" and j.get("image") == BRAND)
        print(f"  代理回读: {j}", flush=True)

        st = requests.get(f"{RT}/swap/status", timeout=5).json().get("params", {})
        verdicts["status_synced"] = (st.get("away_style") == "image"
                                     and st.get("away_text") == "马上回来 · BRB"
                                     and st.get("away_image") == BRAND)
        print(f"  status params: style={st.get('away_style')} text={st.get('away_text')!r}", flush=True)

        print("[3/5] 断供摄像头 → 等 12s 进离席态…", flush=True)
        procs[0].kill()
        time.sleep(12)
        frame = grab_frame()
        brand = cv2.imdecode(np.fromfile(BRAND, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            verdicts["brand_shown"] = False
            print("  !! 抓帧失败", flush=True)
        else:
            ref = cover(brand, frame.shape[1], frame.shape[0])
            mad = mad_top(frame, ref)
            verdicts["brand_shown"] = mad < 30
            print(f"  离席帧 vs 品牌图 上60%区 MAD={mad:.1f} (<30 即品牌图在屏)", flush=True)

        print("[4/5] style=off → 冻结帧(≠品牌图)…", flush=True)
        requests.get(f"{RT}/swap/away", params={"style": "off"}, timeout=5)
        time.sleep(2)
        frame2 = grab_frame()
        if frame2 is None:
            verdicts["off_freezes"] = False
        else:
            ref2 = cover(brand, frame2.shape[1], frame2.shape[0])
            mad2 = mad_top(frame2, ref2)
            verdicts["off_freezes"] = mad2 > 30
            print(f"  off 后帧 vs 品牌图 MAD={mad2:.1f} (>30 即已离开品牌图)", flush=True)

        print("[5/5] 收尾…", flush=True)
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
    print(f"结论: {verdicts}", flush=True)
    return 0 if all(verdicts.values()) and len(verdicts) == 4 else 1


if __name__ == "__main__":
    sys.exit(main())
