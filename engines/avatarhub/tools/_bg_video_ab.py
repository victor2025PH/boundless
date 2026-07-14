# -*- coding: utf-8 -*-
"""视频背景离线验证：抓 /raw 一帧当"人像"，用生成的测试视频当背景跑 BackgroundReplacer——
1) 背景随时间变化(动起来)  2) 人像仍在(未被整帧替换)  3) 残余绿晕=0  4) 耗时达标
5) 播完回卷循环  6) 素材文件坏/缺 → 退化绿幕不崩"""
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

import bg_replace
from bg_replace import BackgroundReplacer

bg_replace._ENGINE = "mediapipe"      # 视频背景断言与耗时线按 mediapipe 校准,固定引擎保证可复现

bg_replace.SETTINGS_PATH = str(OUT / "_vid_settings.json")   # 隔离持久化,不污染直播配置


def grab_raw():
    r = requests.get("http://127.0.0.1:8080/raw", stream=True, timeout=8)
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


raw = grab_raw()
assert raw is not None, "抓不到 /raw 帧（realtime_stream 未运行？）"
if raw.shape[1] > 1280:
    raw = cv2.resize(raw, (1280, 720))
print(f"[raw] {raw.shape}")

# ── 生成测试视频：25fps×2s，蓝→红渐变 + 移动色条（帧间差异大,便于断言"动了"）──
vid_p = str(OUT / "测试动态背景.mp4")
vw = cv2.VideoWriter(vid_p, cv2.VideoWriter_fourcc(*"mp4v"), 25, (1280, 720))
N = 50
for i in range(N):
    f = np.zeros((720, 1280, 3), np.uint8)
    f[:, :, 0] = 255 - int(255 * i / N)
    f[:, :, 2] = int(255 * i / N)
    x = int(1280 * i / N)
    cv2.rectangle(f, (x - 60, 0), (x + 60, 720), (0, 255, 255), -1)
    vw.write(f)
vw.release()
print(f"[vid] 生成 {vid_p} ({os.path.getsize(vid_p)} bytes)")

ok = True
b = BackgroundReplacer()
r = b.set_config(mode="image", image=vid_p)          # 绝对路径直接喂
assert r.get("ok"), f"set_config 拒绝: {r}"
print(f"[status] image_kind={b.status()['image_kind']}")

# 1+2) 背景动起来 & 人像还在
out1 = None
for _ in range(3):
    out1 = b.process(raw.copy())
time.sleep(0.35)                                     # 视频 25fps → 0.35s 应推进 ~8 帧
out2 = b.process(raw.copy())
corner_diff = float(np.abs(out1[:60, :200].astype(int) - out2[:60, :200].astype(int)).mean())
km = b._key_masks(raw)[0]
person_px = int((km > 0.9).sum())
print(f"[动态] 背景角块两次采样平均差={corner_diff:.1f}（>10=在动）  人像像素={person_px}")
if corner_diff < 10 or person_px < 10000:
    ok = False

# 3) 残余绿晕（人像外扩 30px 内数绿像素；测试视频无纯绿,本底≈0）
core = cv2.compare(km, 0.5, cv2.CMP_GT)
x, y, w, h = cv2.boundingRect(core)
x0, y0 = max(0, x - 30), max(0, y - 30)
x1, y1 = min(out2.shape[1], x + w + 30), min(out2.shape[0], y + h + 30)
bch, gch, rch = cv2.split(out2[y0:y1, x0:x1])
leak = int((cv2.subtract(gch, cv2.max(bch, rch)) > 25).sum())
print(f"[绿晕] 残余绿像素={leak}（应=0）")
if leak > 50:
    ok = False

# 4) 耗时
t0 = time.time()
for _ in range(20):
    b.process(raw.copy())
ms = (time.time() - t0) / 20 * 1000
print(f"[耗时] {ms:.1f} ms/帧（应<20）")
if ms >= 20:
    ok = False

# 5) 回卷循环：硬灌超过一轮的帧数
for _ in range(3):
    for _ in range(N + 10):
        b._vid["next_t"] = 0.0                      # 强制每次调用都推进一帧
        b.process(raw.copy())
print(f"[循环] 灌{3 * (N + 10)}帧无异常，当前背景帧存在={b._vid['frame'] is not None}")

# 6) 坏文件退化
bad = str(OUT / "坏视频.mp4")
with open(bad, "wb") as f:
    f.write(b"not a video at all" * 100)
b2 = BackgroundReplacer()
b2.set_config(mode="image", image=bad)
o = None
for _ in range(3):
    o = b2.process(raw.copy())
green_ratio = float((np.all(np.abs(o.astype(int) - [0, 255, 0]) < 30, axis=2)).mean())
print(f"[坏文件] 不崩,退化绿幕占比={green_ratio:.2f}（>0.2=可察觉的显式失败）")
if green_ratio < 0.2:
    ok = False
b.close()
b2.close()

cv2.imencode(".png", np.hstack([out1, out2]))[1].tofile(str(OUT / "video_bg_t0_t1.png"))
print("\nRESULT: " + ("ALL PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
