# -*- coding: utf-8 -*-
"""RVM ONNX 离线烟雾测试：CUDA EP 在 RTX 5090(Blackwell) 上的可用性与真实耗时。
量三种口径：naive(状态回传CPU) / IO-Binding(状态驻显存) / IO-Binding+预处理全含。"""
import time
import sys

import numpy as np
import cv2
import onnxruntime as ort

MODEL = r"C:\模仿音色\models\rvm_mobilenetv3_fp16.onnx"
W, H = 1280, 720
DS = 0.375

print("providers avail:", ort.get_available_providers())
ort.preload_dlls()        # 借 torch/lib 的 cuBLAS/cuDNN(cu128)——env 里 ORT 自身不带 CUDA DLL
so = ort.SessionOptions()
so.log_severity_level = 3
t0 = time.time()
sess = ort.InferenceSession(MODEL, so, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
print(f"session created in {time.time()-t0:.1f}s, active providers: {sess.get_providers()}")
if "CUDAExecutionProvider" not in sess.get_providers():
    print("FATAL: CUDA EP 未生效")
    sys.exit(1)

# 合成 720p 测试帧（随机噪声+方块,含时间变化）
frames = []
rng = np.random.default_rng(7)
for i in range(30):
    f = rng.integers(0, 255, (H, W, 3), np.uint8)
    cv2.rectangle(f, (400 + i * 5, 200), (800 + i * 5, 650), (180, 140, 120), -1)
    frames.append(f)


def preprocess(bgr):
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    x = (rgb.astype(np.float32) * (1.0 / 255.0)).transpose(2, 0, 1)[None]
    return x.astype(np.float16)


# ── IO-Binding：循环状态常驻显存 ─────────────────────────────
rec = [ort.OrtValue.ortvalue_from_numpy(np.zeros([1, 1, 1, 1], np.float16), "cuda", 0)] * 4
dsr = ort.OrtValue.ortvalue_from_numpy(np.asarray([DS], np.float32), "cuda", 0)
io = sess.io_binding()

def run_iob(x):
    global rec
    io.clear_binding_inputs()
    io.clear_binding_outputs()
    io.bind_cpu_input("src", x)
    for k, r in zip(("r1i", "r2i", "r3i", "r4i"), rec):
        io.bind_ortvalue_input(k, r)
    io.bind_ortvalue_input("downsample_ratio", dsr)
    io.bind_output("fgr", "cuda", 0)          # 前景不取回,省一次大拷贝
    io.bind_output("pha")                     # 只有 alpha 回 CPU
    for k in ("r1o", "r2o", "r3o", "r4o"):
        io.bind_output(k, "cuda", 0)
    sess.run_with_iobinding(io)
    outs = io.get_outputs()
    rec = list(outs[2:6])
    return outs[1].numpy()                    # pha [1,1,H,W]


# 预热(编译/显存分配)
for f in frames[:5]:
    pha = run_iob(preprocess(f))
print(f"pha shape={pha.shape} dtype={pha.dtype} range=[{float(pha.min()):.2f},{float(pha.max()):.2f}]")

t0 = time.time()
for f in frames:
    run_iob(preprocess(f))
full = (time.time() - t0) / len(frames) * 1000

xs = [preprocess(f) for f in frames]
t0 = time.time()
for x in xs:
    run_iob(x)
infer_only = (time.time() - t0) / len(xs) * 1000

t0 = time.time()
for f in frames:
    preprocess(f)
pre = (time.time() - t0) / len(frames) * 1000

print(f"[iob ] 预处理={pre:.1f}ms  推理(含src上传+pha回传)={infer_only:.1f}ms  全链={full:.1f}ms/帧")

# 显存占用
import subprocess
r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
                   capture_output=True, text=True)
print("gpu mem used:", r.stdout.strip())
print("RESULT: PASS" if full < 12 else f"RESULT: SLOW ({full:.1f}ms)")
