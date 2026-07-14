# -*- coding: utf-8 -*-
"""虚边优化 A/B/C 取证：抓当前 /raw 一帧（主播身后有实体绿幕），比较
  A=旧原版(宽软边)  B=收紧边缘  C=收紧+实体绿幕色度精修/去溢色(本轮)
量化「半透明过渡像素」与「输出残余绿晕像素」，出对比图（logs/_bg_probe/）。
另做回归：无绿幕画面上 chroma 精修必须零作用。"""
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

bg_replace._ENGINE = "mediapipe"      # 本脚本校准的是 mediapipe+chroma 管线,固定引擎保证可复现

# 测试实例的 set_config 会持久化设置——重定向到临时文件，绝不能污染直播真配置
# (2026-07-07 事故：A/B 脚本把 mode=green 写进 bg_settings.json，重启后直播全绿)。
bg_replace.SETTINGS_PATH = str(OUT / "_ab_settings.json")


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
if raw.shape[1] > 1280:                       # 对齐生产口径：bg.process 吃的是 720p 输出帧
    raw = cv2.resize(raw, (1280, 720))
print(f"[raw] {raw.shape}")

BG_IMG = "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp"


def run(mode, tight, chroma):
    bg_replace._EDGE_TIGHT = tight
    bg_replace._CHROMA_REFINE = chroma
    b = BackgroundReplacer()
    b.set_config(mode=mode, image=BG_IMG)
    out = None
    for _ in range(6):                     # 多跑几帧让 EMA/缓存进稳态
        out = b.process(raw.copy())
    km = b._key_masks(raw)[0]
    trans = int(((km > 0.05) & (km < 0.95)).sum())          # 半透明过渡像素
    # 残绿晕：人像外扩 30px 的矩形内数输出绿像素（能抓到模型误判进人像的整条绿幕，
    # 不只是掩码过渡带；游艇背景图本身几乎无纯绿，本底≈0）
    core = cv2.compare(km, 0.5, cv2.CMP_GT)
    x, y, w, h = cv2.boundingRect(core)
    x0, y0 = max(0, x - 30), max(0, y - 30)
    x1, y1 = min(out.shape[1], x + w + 30), min(out.shape[0], y + h + 30)
    bch, gch, rch = cv2.split(out[y0:y1, x0:x1])
    leak = int((cv2.subtract(gch, cv2.max(bch, rch)) > 25).sum())
    t0 = time.time()
    for _ in range(10):
        b.process(raw.copy())
    ms = (time.time() - t0) / 10 * 1000
    return out, trans, leak, ms


print()
ok = True
for mode in ("image", "green"):
    a_out, a_tr, a_lk, a_ms = run(mode, False, False)
    b_out, b_tr, b_lk, b_ms = run(mode, True, False)
    c_out, c_tr, c_lk, c_ms = run(mode, True, True)
    print(f"[{mode:5s}] 过渡像素 A旧={a_tr:6d}  B收紧={b_tr:6d}  C精修={c_tr:6d}")
    print(f"[{mode:5s}] 残绿像素 A旧={a_lk:6d}  B收紧={b_lk:6d}  C精修={c_lk:6d}")
    print(f"[{mode:5s}] 耗时     A旧={a_ms:5.1f}  B收紧={b_ms:5.1f}  C精修={c_ms:5.1f} ms/帧")
    canvas = np.vstack([a_out, b_out, c_out])
    hh = a_out.shape[0]
    for i, lab in enumerate(("A OLD soft edge", "B tight edge", "C tight + chroma refine")):
        cv2.putText(canvas, lab, (12, hh * i + 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (0, 0, 255) if i == 0 else ((0, 200, 255) if i == 1 else (0, 200, 0)), 2)
    cv2.imencode(".png", canvas)[1].tofile(str(OUT / f"edge_abc_{mode}.png"))
    if mode == "image":                       # 残绿晕指标仅对图片模式有意义(绿幕模式背景本来就是绿)
        if not (c_lk < max(1, a_lk) * 0.25 and c_ms < 18):
            ok = False
    else:
        if not (c_tr < a_tr * 0.25 and c_ms < 18):
            ok = False

# 回归：无绿幕画面（人为压掉绿通道）→ chroma 精修零作用
bg_replace._EDGE_TIGHT = True
bg_replace._CHROMA_REFINE = True
nog = raw.copy()
bch, gch, rch = cv2.split(nog)
nog[:, :, 1] = cv2.min(gch, cv2.max(bch, rch))          # greenness≡0 的"普通房间"
b = BackgroundReplacer()
b.set_config(mode="image", image=BG_IMG)
for _ in range(3):
    b.process(nog.copy())
m_ema = b._mask.copy()
m_ref = b._chroma_refine(nog, m_ema)
diff = float(np.abs(m_ref - m_ema).max())
print(f"\n[回归] 无绿幕帧 chroma 精修掩码最大改动={diff:.4f}（应≈0）")
if diff > 1e-3:
    ok = False

print("\nRESULT: " + ("ALL PASS" if ok else "FAIL"))
print(f"对比图: {OUT}\\edge_abc_image.png / edge_abc_green.png")
sys.exit(0 if ok else 1)
