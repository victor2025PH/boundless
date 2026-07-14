# -*- coding: utf-8 -*-
"""诊断：变声转换「在跑」时，设备枚举各环节谁挂了。
对照四路：RVC /inputDevices 直连 · hub /rvc/devices · hub /rvc/auto_devices · 时序(刚启 vs 稳定后)。
结束必停转换、还原 config.json。"""
import json
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
RVC = "http://127.0.0.1:6242"
CFG = Path(r"C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\configs\config.json")


def probe(tag):
    t0 = time.time()
    try:
        r = requests.get(RVC + "/inputDevices", timeout=6)
        n = len(r.json()) if r.status_code == 200 else -1
        print(f"  [{tag}] RVC直连 /inputDevices → {r.status_code} n={n} ({time.time()-t0:.2f}s)")
    except Exception as e:
        print(f"  [{tag}] RVC直连 /inputDevices → EXC {str(e)[:120]} ({time.time()-t0:.2f}s)")
    t0 = time.time()
    try:
        d = requests.get(HUB + "/rvc/devices", timeout=25).json()
        print(f"  [{tag}] hub /rvc/devices → ok={d.get('ok')} src={d.get('source')} "
              f"in={len(d.get('input_devices') or [])} out={len(d.get('output_devices') or [])} "
              f"note={str(d.get('rvc_note'))[:80]} detail={str(d.get('detail'))[:120]} ({time.time()-t0:.2f}s)")
    except Exception as e:
        print(f"  [{tag}] hub /rvc/devices → EXC {str(e)[:120]} ({time.time()-t0:.2f}s)")
    t0 = time.time()
    try:
        d = requests.get(HUB + "/rvc/auto_devices", timeout=25).json()
        print(f"  [{tag}] hub /rvc/auto_devices → ok={d.get('ok')} in={str(d.get('input'))[:40]} "
              f"out={str(d.get('output'))[:40]} ({time.time()-t0:.2f}s)")
    except Exception as e:
        print(f"  [{tag}] hub /rvc/auto_devices → EXC {str(e)[:120]} ({time.time()-t0:.2f}s)")


cfg_orig = CFG.read_bytes()
try:
    print("== 基线（未在跑）==")
    probe("idle")
    pick = requests.get(HUB + "/rvc/auto_devices", timeout=25).json()
    cfg = json.loads(cfg_orig.decode("utf-8"))
    cfg["sg_input_device"] = pick["input"]
    cfg["sg_output_device"] = pick["output"]
    cfg.pop("use_jit", None)
    print("config →", requests.post(RVC + "/config", json=cfg, timeout=20).status_code,
          "| start →", requests.post(RVC + "/start", timeout=90).status_code)
    print("== 刚启动 0.5s ==")
    time.sleep(0.5)
    probe("t+0.5s")
    print("== 稳定 5s 后 ==")
    time.sleep(5)
    probe("t+5s")
finally:
    try:
        print("stop →", requests.post(RVC + "/stop", timeout=15).status_code)
    except Exception as e:
        print("stop EXC", e)
    CFG.write_bytes(cfg_orig)
    print("config.json restored")
