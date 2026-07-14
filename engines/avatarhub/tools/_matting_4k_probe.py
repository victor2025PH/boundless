# -*- coding: utf-8 -*-
"""4K 录播验证：64 秒 3840×2160 走 API 全链路（内部 720 处理），
断言正常闸门放行、任务完成、产物为原生 4K、速度可用。"""
import sys
import time
from pathlib import Path

import cv2
import requests

HUB = "http://127.0.0.1:9000"
BASE = Path(r"C:\模仿音色")
CLIP = BASE / "logs" / "matting_offline" / "_rehearsal_4k.mp4"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("[OK] " if cond else "[NG] ") + name + (f"  {detail}" if detail else ""), flush=True)
    ok_all = ok_all and bool(cond)


def status():
    return requests.get(f"{HUB}/api/matting/status", timeout=10).json()


st = status()
if st.get("streaming") or st.get("running"):
    print("SKIP: 推流中或有任务在跑")
    sys.exit(0)
requests.post(f"{HUB}/api/matting/cancel_queue", json={}, timeout=10)

with open(CLIP, "rb") as f:
    up = requests.post(f"{HUB}/api/matting/upload",
                       files={"file": ("彩排4K.mp4", f, "video/mp4")}, timeout=600).json()
name = up.get("saved", "")
check("上传 4K 素材", bool(name), name)

r = requests.post(f"{HUB}/api/matting/start", json={"input": name, "bg": "green"}, timeout=30).json()
check("正常闸门放行", r.get("ok"), "queued(租约)" if r.get("queued") else "直跑")

job, t0 = {}, time.time()
while time.time() - t0 < 900:
    st = status()
    j = st.get("job", {})
    if j.get("input") == name and not st.get("running") and j.get("state") in ("done", "error"):
        job = j
        break
    time.sleep(3)
check("4K 任务完成", job.get("state") == "done",
      f"{job.get('n')}帧 {job.get('ms')}ms/帧" + (f" err={job.get('error')}" if job.get("error") else ""))

for o in job.get("outputs") or []:
    p = BASE / "logs" / "matting_offline" / o
    cap = cv2.VideoCapture(str(p))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    check(f"产物 {o.split('_')[-1]} 为原生4K", (w, h) == (3840, 2160),
          f"{w}x{h} {n}帧 {p.stat().st_size / 1048576:.0f}MB")

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
