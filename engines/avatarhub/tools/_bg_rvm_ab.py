# -*- coding: utf-8 -*-
"""RVM vs MediaPipe 抠像 A/B 基准（ADR-12-02 第①阶段验收）：
抓当前 /raw 帧 → 两引擎各跑 20 帧 → 量化耗时/过渡带/残绿/显存，出对比图与发丝特写。
验收线：RVM ≤8ms/帧(不含合成)、全链 ≤20ms、残绿≤MediaPipe、显存增量 <2GB、坏模型自动回退。"""
import os
import subprocess
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

bg_replace.SETTINGS_PATH = str(OUT / "_rvm_ab_settings.json")   # 隔离持久化

BG_IMG = "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp"


def gpu_used_mb():
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    return int(r.stdout.strip().splitlines()[0])


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
assert raw is not None, "抓不到 /raw（realtime_stream 未运行？）"
if raw.shape[1] > 1280:
    raw = cv2.resize(raw, (1280, 720))
print(f"[raw] {raw.shape}")

results = {}
ok = True


def metrics(b, out):
    km = b._key_masks(raw)[0]
    trans = int(((km > 0.05) & (km < 0.95)).sum())
    core = cv2.compare(km, 0.5, cv2.CMP_GT)
    x, y, w, h = cv2.boundingRect(core)
    x0, y0 = max(0, x - 30), max(0, y - 30)
    x1, y1 = min(out.shape[1], x + w + 30), min(out.shape[0], y + h + 30)
    bch, gch, rch = cv2.split(out[y0:y1, x0:x1])
    leak = int((cv2.subtract(gch, cv2.max(bch, rch)) > 25).sum())
    return trans, leak, (x, y, w, h)


def run_engine(pref, wait_rvm):
    bg_replace._ENGINE = pref
    b = BackgroundReplacer()
    r = b.set_config(mode="image", image=BG_IMG)
    assert r.get("ok"), r
    if wait_rvm:
        t0 = time.time()
        while b._rvm is None and not b._rvm_failed and time.time() - t0 < 60:
            time.sleep(0.5)
        if b._rvm_failed:
            return b, None, b._rvm_failed
    out = None
    for _ in range(5):                     # 稳态（RVM rec 记忆/mp EMA）
        out = b.process(raw.copy())
    t0 = time.time()
    for _ in range(20):
        out = b.process(raw.copy())
    ms = (time.time() - t0) / 20 * 1000
    return b, out, ms


# ── B: MediaPipe 基线 ─────────────────────────────────────────
mem0 = gpu_used_mb()
b_mp, out_mp, ms_mp = run_engine("mediapipe", False)
tr_mp, lk_mp, bbox = metrics(b_mp, out_mp)
print(f"[mediapipe] 全链={ms_mp:.1f}ms/帧  过渡px={tr_mp}  残绿px={lk_mp}  engine={b_mp._engine_live}")
b_mp.close()

# ── C: RVM ────────────────────────────────────────────────────
b_rv, out_rv, ms_rv = run_engine("rvm", True)
if out_rv is None:
    print(f"FATAL: RVM 预热失败: {ms_rv}")
    sys.exit(1)
mem1 = gpu_used_mb()
tr_rv, lk_rv, _ = metrics(b_rv, out_rv)
st = b_rv.status()
print(f"[rvm      ] 全链={ms_rv:.1f}ms/帧  过渡px={tr_rv}  残绿px={lk_rv}  "
      f"engine={b_rv._engine_live}  显存增量={mem1 - mem0}MB")
if not (b_rv._engine_live == "rvm" and st["rvm"]["ready"]):
    ok = False

# 纯推理口径（不含背景合成）
t0 = time.time()
for _ in range(20):
    b_rv._rvm_mask(raw)
ms_infer = (time.time() - t0) / 20 * 1000
print(f"[rvm      ] 纯抠像(预处理+推理+α回传)={ms_infer:.1f}ms/帧")

# ── 验收判定 ──────────────────────────────────────────────────
checks = {
    "RVM 纯抠像 ≤8ms": ms_infer <= 8.0,
    "RVM 全链 ≤20ms": ms_rv <= 20.0,
    "残绿不劣于基线": lk_rv <= max(lk_mp, 50),
    "显存增量 <2GB": (mem1 - mem0) < 2048,
}
for k, v in checks.items():
    print(("[OK] " if v else "[NG] ") + k)
    ok = ok and v

# ── 回退验证：坏模型路径 → mediapipe 顶上,不崩 ────────────────
bg_replace._ENGINE = "auto"
bg_replace._RVM_MODEL_PATH = str(OUT / "不存在的模型.onnx")
bg_replace._RVM_URLS = ("http://127.0.0.1:1/nope.onnx",)      # 断下载,逼失败
b_fb = BackgroundReplacer()
b_fb.set_config(mode="image", image=BG_IMG)
t0 = time.time()
while not b_fb._rvm_failed and time.time() - t0 < 30:
    b_fb.process(raw.copy())
    time.sleep(0.05)
o = b_fb.process(raw.copy())
fb_ok = b_fb._engine_live == "mediapipe" and o is not None and b_fb._rvm_failed
print(("[OK] " if fb_ok else "[NG] ") + f"坏模型自动回退 mediapipe（error={b_fb._rvm_failed[:40]}）")
ok = ok and bool(fb_ok)
b_fb.close()

# ── 取证图：整帧对比 + 头部发丝特写(2×放大) ───────────────────
x, y, w, h = bbox
cx = x + w // 2
crop = (slice(max(0, y - 20), min(720, y + 280)), slice(max(0, cx - 150), min(1280, cx + 150)))
zoom = lambda img: cv2.resize(img[crop], None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
panel_full = np.vstack([out_mp, out_rv])
cv2.putText(panel_full, "B MediaPipe+chroma (CPU)", (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 255), 2)
cv2.putText(panel_full, "C RVM neural matting (5090)", (12, 720 + 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
panel_head = np.hstack([zoom(out_mp), zoom(out_rv)])
cv2.putText(panel_head, "MediaPipe", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 255), 2)
cv2.putText(panel_head, "RVM", (panel_head.shape[1] // 2 + 12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 0), 2)
cv2.imencode(".png", panel_full)[1].tofile(str(OUT / "rvm_ab_full.png"))
cv2.imencode(".png", panel_head)[1].tofile(str(OUT / "rvm_ab_head.png"))
b_rv.close()

print(f"\n对比图: {OUT}\\rvm_ab_full.png / rvm_ab_head.png")
print("RESULT: " + ("ALL PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
