# -*- coding: utf-8 -*-
"""阶段15 冒烟：videotryon 服务全链（提交→进度→结果），
输入与阶段14 PoC 完全相同 → 顺带完成 AutoMasker vs FitDiT 遮罩对比。
腾显存必须 stop?suspend=1：七测实锤，裸 stop 会被 self-heal 20s 拉回，
lipsync(+5G) 在解码中途回场 → WDDM 换页 → 解码 30s 拖成 340s。"""
import base64
import sys
import time
from pathlib import Path

import requests


sys.path.insert(0, r"c:\模仿音色")
import service_auth  # noqa: E402


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
H = {"X-Service-Token": service_auth._token_direct()}
API = "http://127.0.0.1:8006"
HUB = "http://127.0.0.1:9000"

for _svc in ("lipsync", "ditto"):
    try:
        rr = requests.post(f"{HUB}/api/engine/stop?name={_svc}&suspend=1",
                           headers=H, timeout=40)
        print(f"park {_svc}:", rr.json().get("ok"))
    except Exception as e:
        print(f"park {_svc} fail:", str(e)[:60])
try:
    requests.post("http://127.0.0.1:8001/unload", headers=H, timeout=20)
except Exception:
    pass
time.sleep(3)

cloth_b64 = base64.b64encode(
    Path(r"C:\datasets\viton_hd_test\cloth\00069_00.jpg").read_bytes()).decode()
r = requests.post(f"{API}/video_tryon", headers=H, timeout=15, json={
    "person_video_path": r"c:\模仿音色\logs\catv2ton_poc\person_motion.mp4",
    "cloth_image": cloth_b64,
    "cloth_type": "upper",
    "max_frames": 48,
})
print("submit:", r.status_code, r.text[:200])
if r.status_code != 200:
    sys.exit(1)
jid = r.json()["job_id"]

last = ""
t0 = time.time()
fails = 0
while True:
    time.sleep(5)
    try:
        j = requests.get(f"{API}/job/{jid}", headers=H, timeout=8).json()
        fails = 0
    except Exception as e:                       # GPU 重载阶段服务可能短暂无响应
        fails += 1
        print(f"  [{time.time() - t0:5.0f}s] poll失败#{fails}: {str(e)[:60]}", flush=True)
        if fails >= 24:                          # 连续 2 分钟无响应 → 判死
            print("SERVICE UNRESPONSIVE")
            sys.exit(1)
        continue
    line = f"{j['state']} {j.get('progress', 0)}% {j.get('detail', '')}"
    if line != last:
        print(f"  [{time.time() - t0:5.0f}s] {line}", flush=True)
        last = line
    if j["state"] in ("done", "error"):
        break
    if time.time() - t0 > 900:
        print("TIMEOUT")
        sys.exit(1)

for _svc in ("lipsync", "ditto"):                # 解泊：engine/start 会清挂起标记
    try:
        requests.post(f"{HUB}/api/engine/start?name={_svc}", headers=H, timeout=60)
    except Exception:
        pass

if j["state"] == "done":
    print("meta:", j["meta"])
    out = Path(r"c:\模仿音色\logs\catv2ton_poc\svc_result.mp4")
    out.write_bytes(requests.get(f"{API}/job/{jid}/result", headers=H, timeout=30).content)
    Path(r"c:\模仿音色\logs\catv2ton_poc\svc_preview.jpg").write_bytes(
        requests.get(f"{API}/job/{jid}/preview", headers=H, timeout=30).content)
    print("saved svc_result.mp4 / svc_preview.jpg")
else:
    print("FAILED:", j.get("detail"))
    sys.exit(1)
