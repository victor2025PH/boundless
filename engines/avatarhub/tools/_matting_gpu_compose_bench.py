# -*- coding: utf-8 -*-
"""GPU 合成改造后复跑 4K/1080p，与改造前 ms/帧 对照。"""
import time

import requests

HUB = "http://127.0.0.1:9000"
CLIPS = ("彩排4K.mp4", "彩排1080p_3.mp4")

_st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
if _st.get("streaming") or _st.get("running"):
    print("SKIP: 推流中或有任务在跑，测速数字无意义且会积压队列")
    raise SystemExit(0)

for name in CLIPS:
    r = requests.post(f"{HUB}/api/matting/start", json={"input": name, "bg": "green"}, timeout=30).json()
    print("submit", name, "->", "queued" if r.get("queued") else "run", flush=True)

t0 = time.time()
done = {}
while time.time() - t0 < 1500 and len(done) < len(CLIPS):
    st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
    if not st.get("running") and not st.get("queue"):
        for j in st.get("history", [])[:4]:
            if j.get("input") in CLIPS and j["id"] not in done and j.get("ts", 0) > t0 - 5:
                done[j["id"]] = j
                print("done %s: %s %s帧 %sms/帧" % (j.get("input"), j.get("state"), j.get("n"), j.get("ms")), flush=True)
        if len(done) >= len(CLIPS):
            break
    time.sleep(5)
print("RESULT:", "ALL DONE" if len(done) == len(CLIPS) else "TIMEOUT")
