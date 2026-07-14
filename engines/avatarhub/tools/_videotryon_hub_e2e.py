# -*- coding: utf-8 -*-
"""阶段15 收尾 E2E：走 Hub 产品链（/api/videotryon/submit → job → apply）。
与 _videotryon_smoke.py 的区别：不直连 8006、不手工泊车——腾挪/挂起/解泊
全部由 Hub 编排，验证的就是用户点按钮时的真实链路。"""
import sys
import time

import requests


sys.path.insert(0, r"c:\模仿音色")
import service_auth  # noqa: E402


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
H = {"X-Service-Token": service_auth._token_direct()}
HUB = "http://127.0.0.1:9000"
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "Inside"
CLOTH = sys.argv[2] if len(sys.argv) > 2 else "演示上衣001"

r = requests.post(f"{HUB}/api/videotryon/submit", headers=H, timeout=60, json={
    "profile": PROFILE, "cloth_name": CLOTH, "cloth_type": "upper",
    "field": "idle_video"})
print("submit:", r.status_code, r.text[:220])
if r.status_code != 200:
    sys.exit(1)
jid = r.json()["job_id"]

last, t0, fails = "", time.time(), 0
while True:
    time.sleep(5)
    try:
        j = requests.get(f"{HUB}/api/videotryon/job/{jid}", headers=H, timeout=8).json()
        fails = 0
    except Exception as e:
        fails += 1
        if fails >= 24:
            print("HUB UNRESPONSIVE")
            sys.exit(1)
        continue
    line = f"{j.get('state')} {j.get('progress', 0)}% {j.get('detail', '')}"
    if line != last:
        print(f"  [{time.time() - t0:5.0f}s] {line}", flush=True)
        last = line
    if j.get("state") in ("done", "error"):
        break
    if time.time() - t0 > 900:
        print("TIMEOUT")
        sys.exit(1)

if j.get("state") != "done":
    print("FAILED:", j.get("detail"))
    sys.exit(1)
print("meta:", j.get("meta"))

pv = requests.get(f"{HUB}/api/videotryon/job/{jid}/preview", headers=H, timeout=15)
print("preview proxy:", pv.status_code, len(pv.content), "bytes")
vd = requests.get(f"{HUB}/api/videotryon/job/{jid}/video", headers=H, timeout=60)
print("video proxy:", vd.status_code, len(vd.content), "bytes")

a = requests.post(f"{HUB}/api/videotryon/job/{jid}/apply", headers=H, timeout=120,
                  json={})
print("apply:", a.status_code, a.text[:220])
if a.status_code != 200:
    sys.exit(1)

d = requests.get(f"{HUB}/profiles/{PROFILE}", headers=H, timeout=8).json()
print("profile idle_video →", d.get("idle_video"))
hh = requests.get(f"{HUB}/api/profiles/{PROFILE}/look_history", headers=H,
                  timeout=8).json()
items = hh.get("items") or hh.get("history") or []
print("look_history 最新:", (items[0] if items else {}))
print("E2E DONE")
