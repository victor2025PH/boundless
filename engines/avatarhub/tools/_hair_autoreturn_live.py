# -*- coding: utf-8 -*-
"""阶段10：发型显存自动归还——线上触发复验（判定函数已单测，这里验完整闭环）。
前置：8001 已以 HAIR_IDLE_UNLOAD_MIN=1、HAIR_KEEP_FREE_GB=99 重启
（99G 恒大于 free → "压卡"条件恒真；1min 空闲即触发，免等 15min）。
流程：触发一次真实 hair_transfer（懒加载→驻留）→ 轮询 /health 看 model_loaded
在 ~2min 内自动翻回 False，同时对照 VRAM free 回升。"""
import base64
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HAIR = "http://127.0.0.1:8001"


def free_gb() -> float:
    import torch
    return torch.cuda.mem_get_info()[0] / 1024**3


h = requests.get(f"{HAIR}/health", timeout=6).json()
print(f"[0] 初态 model_loaded={h['model_loaded']} free={free_gb():.1f}G")

face = sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))[3]
b64 = base64.b64encode(face.read_bytes()).decode()
t0 = time.time()
r = requests.post(f"{HAIR}/hair_transfer", json={"source_image": b64}, timeout=240)
print(f"[1] hair_transfer {r.status_code} {int(time.time() - t0)}s "
      f"detail={str(r.json().get('detail'))[:80] if r.status_code != 200 else '-'}")
if r.status_code != 200:
    sys.exit(1)

h = requests.get(f"{HAIR}/health", timeout=6).json()
f_loaded = free_gb()
print(f"[2] 驻留态 model_loaded={h['model_loaded']} free={f_loaded:.1f}G")
if not h["model_loaded"]:
    print("[NG] 推理后模型未驻留？")
    sys.exit(1)

# 空闲计时从最后一次使用起：1min 阈值 + 60s 轮询周期 → 最坏 ~2min
print("[3] 等待自动归还（阈值 1min，轮询线程 60s/轮，最坏 ~130s）…")
deadline = time.time() + 240
while time.time() < deadline:
    time.sleep(15)
    h = requests.get(f"{HAIR}/health", timeout=6).json()
    ff = free_gb()
    print(f"    t+{int(time.time() - t0)}s model_loaded={h['model_loaded']} free={ff:.1f}G")
    if not h["model_loaded"]:
        print(f"[PASS] 自动归还触发：显存 {f_loaded:.1f}G → {ff:.1f}G（回收 {ff - f_loaded:.1f}G）")
        sys.exit(0)
print("[FAIL] 240s 内未自动归还——查 8001 控制台日志")
sys.exit(1)
