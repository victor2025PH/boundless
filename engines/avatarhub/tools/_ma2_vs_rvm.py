# -*- coding: utf-8 -*-
"""MatAnyone2 vs RVM vs MediaPipe：同一段录播素材的边缘质量+时域稳定性对比。
RVM 按直播管线逐帧带记忆跑全片；MA2 读已生成的 pha 视频；MediaPipe 单帧参考。"""
import io
import sys
import time
from pathlib import Path

import numpy as np
import cv2

BASE = Path(r"C:\模仿音色")
sys.path.insert(0, str(BASE))
OUT = BASE / "logs" / "matting_offline"
CLIP = str(OUT / "_test_raw.mp4")
PHA_MA2 = str(OUT / "_test_raw_yacht_pha.mp4")
BG = str(BASE / "bg_images" / "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp")
K = 60          # 取证帧号


def read_all(path, gray=False):
    cap = cv2.VideoCapture(path)
    fr = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        fr.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if gray else f)
    cap.release()
    return fr


def flicker(alphas):
    vals = []
    for a0, a1 in zip(alphas, alphas[1:]):
        band = (a1 > 0.02) & (a1 < 0.98)
        if band.any():
            vals.append(float(np.abs(a1 - a0)[band].mean()))
    return float(np.mean(vals))


frames = read_all(CLIP)
print(f"[clip] {len(frames)}帧 {frames[0].shape}")

# ── RVM 逐帧（同直播管线）───────────────────────────────────
import torch
with open(BASE / "models" / "rvm_mobilenetv3_fp16.torchscript", "rb") as f:
    net = torch.jit.load(io.BytesIO(f.read()), map_location="cuda").eval()
ds = torch.tensor([0.375], device="cuda")
rec = [None] * 4
alphas_rvm = []
t0 = time.time()
with torch.inference_mode():
    for f in frames:
        t = torch.from_numpy(np.ascontiguousarray(f)).cuda()
        src = t.permute(2, 0, 1)[None].flip(1).half().div_(255.0)
        _, pha, *rec = net(src, *rec, ds)
        alphas_rvm.append(pha[0, 0].float().cpu().numpy())
ms_rvm = (time.time() - t0) / len(frames) * 1000
del net
torch.cuda.empty_cache()

# ── MA2（读产物）＋ MediaPipe（单帧）────────────────────────
alphas_ma2 = [g.astype(np.float32) / 255.0 for g in read_all(PHA_MA2, gray=True)]
assert len(alphas_ma2) == len(frames), (len(alphas_ma2), len(frames))

import bg_replace
from bg_replace import BackgroundReplacer
bg_replace.SETTINGS_PATH = str(OUT / "_cmp_settings.json")
bg_replace._ENGINE = "mediapipe"
b = BackgroundReplacer()
b.set_config(mode="image", image=Path(BG).name)
for _ in range(3):
    b.process(frames[K].copy())
alpha_mp = b._key_masks(frames[K])[0].copy()
b.close()

# ── 指标 ─────────────────────────────────────────────────────
fl_rvm, fl_ma2 = flicker(alphas_rvm), flicker(alphas_ma2)
print(f"[rvm ] {ms_rvm:.0f}ms/帧  时域抖动={fl_rvm:.4f}")
print(f"[ma2 ] 86ms/帧(实测)      时域抖动={fl_ma2:.4f}")

bg = cv2.imdecode(np.fromfile(BG, np.uint8), cv2.IMREAD_COLOR)
h, w = frames[K].shape[:2]
ih, iw = bg.shape[:2]
s = max(w / iw, h / ih)
bgr = cv2.resize(bg, (int(iw * s), int(ih * s)))
y0, x0 = (bgr.shape[0] - h) // 2, (bgr.shape[1] - w) // 2
bgc = bgr[y0:y0 + h, x0:x0 + w].astype(np.float32)


def compose(alpha):
    a = alpha[..., None].astype(np.float32)
    return (frames[K].astype(np.float32) * a + bgc * (1 - a)).astype(np.uint8)


def stats(alpha, out):
    trans = int(((alpha > 0.05) & (alpha < 0.95)).sum())
    core = (alpha > 0.5).astype(np.uint8) * 255
    x, y, ww, hh = cv2.boundingRect(core)
    x0_, y0_ = max(0, x - 30), max(0, y - 30)
    x1_, y1_ = min(w, x + ww + 30), min(h, y + hh + 30)
    bch, gch, rch = cv2.split(out[y0_:y1_, x0_:x1_])
    leak = int((cv2.subtract(gch, cv2.max(bch, rch)) > 25).sum())
    return trans, leak, (x, y, ww, hh)


outs, rows = {}, []
for name, a in (("MediaPipe", alpha_mp), ("RVM", alphas_rvm[K]), ("MatAnyone2", alphas_ma2[K])):
    o = compose(a)
    tr, lk, bbox = stats(a, o)
    outs[name] = o
    rows.append((name, tr, lk))
    print(f"[{name:10s}] 过渡px={tr:6d} 残绿px={lk}")

x, y, ww, hh = bbox
cx = x + ww // 2
crop = (slice(max(0, y - 15), min(h, y + 265)), slice(max(0, cx - 160), min(w, cx + 160)))
zoom = lambda img: cv2.resize(img[crop], None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
panel = np.hstack([zoom(outs["MediaPipe"]), zoom(outs["RVM"]), zoom(outs["MatAnyone2"])])
seg = panel.shape[1] // 3
for i, (nm, c) in enumerate((("MediaPipe", (0, 200, 255)), ("RVM", (0, 200, 0)), ("MatAnyone2", (255, 120, 0)))):
    cv2.putText(panel, nm, (seg * i + 10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, c, 2)
cv2.imencode(".png", panel)[1].tofile(str(OUT / "ma2_head_cmp.png"))

af = np.hstack([(np.clip(a, 0, 1) * 255).astype(np.uint8)[crop] for a in (alpha_mp, alphas_rvm[K], alphas_ma2[K])])
cv2.imencode(".png", cv2.resize(af, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))[1] \
    .tofile(str(OUT / "ma2_alpha_cmp.png"))
print(f"saved {OUT}\\ma2_head_cmp.png / ma2_alpha_cmp.png")
