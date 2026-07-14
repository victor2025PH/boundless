# -*- coding: utf-8 -*-
"""连续采样显卡空闲显存，观察脉冲形态（阶段13 排障用）。"""
import subprocess
import sys
import time

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
gap = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
for _ in range(n):
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True)
    print(f"{time.strftime('%H:%M:%S')} free={int(r.stdout.strip()) / 1024:.1f}G", flush=True)
    time.sleep(gap)
