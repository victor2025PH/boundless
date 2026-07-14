# -*- coding: utf-8 -*-
"""06x E2E：收养后离席配置对齐实弹。
   剧本：裸起孤儿流(默认 away 环境) → 存标记离席配置 → 重启 hub → 首个守护 tick 收养
        → 孤儿的 /swap/status.params.away_* 应被热推成标记值(收养对齐生效)
        → 停播树杀 → 复原配置/清账本。
   前置：生产空闲(9000 在跑、8080 无人)。"""
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(r"C:\模仿音色")
HUB = "http://127.0.0.1:9000"
RT = "http://127.0.0.1:8080"
CAM_PORT = 8094
FF_PY = str(Path.home() / "Miniconda3" / "envs" / "facefusion" / "python.exe")
MARK_TEXT = "收养对齐-去去就回"

v = {}
procs = []
t0 = time.time()


def bail(msg, code=2):
    print(f"FATAL: {msg}")
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    sys.exit(code)


if requests.get(f"{HUB}/realtime/status", timeout=5).json().get("video"):
    bail("生产在播,拒绝 E2E")
try:
    requests.get(f"{RT}/swap/status", timeout=2)
    bail("8080 已有实例")
except requests.exceptions.RequestException:
    pass

print("[1/7] 存标记离席配置(blur + 标记文案)…", flush=True)
orig = (requests.get(f"{HUB}/api/effect_cfg", timeout=5).json().get("cfg") or {})
keep = {k: orig.get(k) for k in ("awayStyle", "awayText", "awayImage") if k in orig}
j = requests.post(f"{HUB}/api/effect_cfg", json={
    "awayStyle": "blur", "awayText": MARK_TEXT, "awayImage": ""}, timeout=5).json()
if not j.get("ok"):
    bail(f"配置保存失败 {j}")

print("[2/7] 裸起孤儿流(默认 away 环境,不经 hub)…", flush=True)
procs.append(subprocess.Popen(
    [FF_PY, str(BASE / "tools" / "synth_cam.py"), "--width", "960", "--height", "540",
     "--fps", "15", "--port", str(CAM_PORT)],
    cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
deadline = time.time() + 20
while time.time() < deadline:
    try:
        if requests.get(f"http://127.0.0.1:{CAM_PORT}/health", timeout=2).status_code == 200:
            break
    except Exception:
        time.sleep(1)
else:
    bail("synth_cam 未就绪")
import os
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
env.pop("SWAP_AWAY_TEXT", None)          # 确保孤儿带的是默认文案(与已存配置不同)
rt_orphan = subprocess.Popen(
    [FF_PY, str(BASE / "realtime_stream.py"), "--source", f"http://127.0.0.1:{CAM_PORT}/stream",
     "--width", "960", "--height", "540", "--swap-preset", "eco", "--no-preview",
     "--mjpeg-port", "8080"],
    cwd=str(BASE), env=env,
    stdout=open(BASE / "logs" / "rt_adopt_align_e2e.log", "w", encoding="utf-8"),
    stderr=subprocess.STDOUT)
procs.append(rt_orphan)
deadline = time.time() + 60
while time.time() < deadline:
    try:
        if requests.get(f"{RT}/swap/status", timeout=3).status_code == 200:
            break
    except Exception:
        pass
    time.sleep(2)
else:
    bail("孤儿流未就绪")
pre = requests.get(f"{RT}/swap/status", timeout=3).json().get("params") or {}
v["orphan_default_text"] = pre.get("away_text") != MARK_TEXT
print(f"  孤儿出生文案: {pre.get('away_text')!r} (应≠标记)", flush=True)

print("[3/7] 重启 hub(制造孤儿+触发首 tick 收养)…", flush=True)
import psutil
hub_pid = None
for c in psutil.net_connections(kind="tcp"):
    if c.laddr and c.laddr.port == 9000 and c.status == "LISTEN":
        hub_pid = c.pid
        break
if not hub_pid:
    bail("找不到 hub 进程")
psutil.Process(hub_pid).kill()
time.sleep(2)
subprocess.Popen(["cmd", "/c", "start", "", "/min", str(BASE / "_launch_hub_detached.bat")],
                 cwd=str(BASE), shell=False)
deadline = time.time() + 90
while time.time() < deadline:
    try:
        if requests.get(f"{HUB}/health", timeout=3).status_code == 200:
            break
    except Exception:
        pass
    time.sleep(3)
else:
    bail("hub 重启未就绪")
print("  hub 已回来", flush=True)

print("[4/7] 等首个守护 tick 收养…", flush=True)
adopted = False
deadline = time.time() + 120
while time.time() < deadline:
    try:
        s = requests.get(f"{HUB}/realtime/status", timeout=5).json()
        if s.get("orphan_adopted"):
            adopted = True
            print(f"  已收养: {s.get('orphan_adopted')}", flush=True)
            break
    except Exception:
        pass
    time.sleep(5)
v["adopted"] = adopted

print("[5/7] 校验离席配置已对齐…", flush=True)
time.sleep(3)          # 对齐热推在收养之后紧接发生,给 1 个来回余量
post = {}
try:
    post = requests.get(f"{RT}/swap/status", timeout=5).json().get("params") or {}
except Exception:
    pass
v["align_pushed"] = post.get("away_text") == MARK_TEXT and post.get("away_style") == "blur"
print(f"  收养后文案: {post.get('away_text')!r} (应=标记)", flush=True)

print("[6/7] 停播(树杀孤儿) + 复原…", flush=True)
try:
    requests.post(f"{HUB}/realtime/stop", timeout=15)
except Exception:
    pass
time.sleep(3)
try:
    requests.get(f"{RT}/swap/status", timeout=2)
    v["stopped"] = False
except requests.exceptions.RequestException:
    v["stopped"] = True
restore = {"awayStyle": keep.get("awayStyle", "blur"),
           "awayText": keep.get("awayText", "稍等片刻 · Be right back"),
           "awayImage": keep.get("awayImage", "")}
r2 = requests.post(f"{HUB}/api/effect_cfg", json=restore, timeout=5).json()
v["cfg_restored"] = r2.get("ok") is True

print("[7/7] 清理合成场次账本…", flush=True)
for p in procs:
    try:
        p.kill()
    except Exception:
        pass
time.sleep(6)
led = BASE / "logs" / "swap_sessions.jsonl"
removed = 0
if led.exists():
    import json as _json
    lines = led.read_text(encoding="utf-8").splitlines()
    kept_lines = []
    for ln in lines:
        try:
            e = _json.loads(ln)
            if float(e.get("start", 0)) >= t0 - 5 and int(e.get("dur_s", 9999)) < 600:
                removed += 1
                continue
        except Exception:
            pass
        kept_lines.append(ln)
    if removed:
        led.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
print(f"  账本清理: 移除 {removed} 条", flush=True)

print(f"结论: {v}", flush=True)
sys.exit(0 if all(v.values()) and len(v) == 5 else 1)
