# -*- coding: utf-8 -*-
"""显存死人开关（阶段15 冒烟护航）：物理空闲 < 阈值持续 2 拍 → 杀掉目标端口进程。
上次解码期显存爆穿把整机拖死重启，这次宁可杀作业也不赌。用完即弃。"""
import subprocess
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8006
LIMIT_MB = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
DURATION_S = int(sys.argv[3]) if len(sys.argv) > 3 else 900

low = 0
t0 = time.time()
while time.time() - t0 < DURATION_S:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8)
        free = int(out.stdout.strip().splitlines()[0])
    except Exception:
        time.sleep(2)
        continue
    if free < LIMIT_MB:
        low += 1
        print(f"[deadman] 低水位 {free}MB ({low}/2)", flush=True)
        if low >= 2:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen | "
                 f"Select-Object -First 1).OwningProcess"],
                capture_output=True, text=True, timeout=15)
            pid = (r.stdout or "").strip()
            if pid.isdigit():
                subprocess.run(["taskkill", "/PID", pid, "/F", "/T"],
                               capture_output=True, timeout=15)
                print(f"[deadman] 已击杀 :{PORT} pid={pid}（free={free}MB）", flush=True)
            else:
                print(f"[deadman] :{PORT} 无监听进程", flush=True)
            sys.exit(2)
    else:
        low = 0
    time.sleep(2)
print("[deadman] 结束（未触发）", flush=True)
