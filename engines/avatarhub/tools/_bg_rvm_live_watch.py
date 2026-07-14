# -*- coding: utf-8 -*-
"""RVM 上线后 60s 稳定性观测：引擎位/抠像耗时/推流fps/显存峰值。"""
import statistics
import subprocess
import time

import requests

mss, fpss, vrams = [], [], []
for i in range(12):
    try:
        st = requests.get("http://127.0.0.1:8080/bg/status", timeout=3).json()
        rt = requests.get("http://127.0.0.1:9000/realtime/status", timeout=3).json()
        m = rt.get("metrics", {})
        v = int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True).stdout.strip())
        mss.append(st["ms"])
        fpss.append(m.get("fps", 0))
        vrams.append(v)
        print(f'{i*5:3d}s engine={st["engine"]} bg_ms={st["ms"]:5.1f} '
              f'fps={m.get("fps", 0):5.1f} swap_fail_ps={m.get("swap_fail_ps", 0)} vram={v}MiB')
    except Exception as e:
        print(i, "ERR", str(e)[:60])
    time.sleep(5)
print(f"--- bg_ms avg={statistics.mean(mss):.1f} max={max(mss):.1f} | "
      f"fps min={min(fpss):.1f} | vram max={max(vrams)}MiB ({max(vrams)/1024:.1f}G)")
