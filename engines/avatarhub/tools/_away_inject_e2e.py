# -*- coding: utf-8 -*-
"""06v 最后一环 E2E：hub 生产开播路径的离席配置注入实测。
   链路：effect_cfg 存标记值 → POST /realtime/start(合成源,真生产 spawn,_video_env 注入)
        → /swap/status.params.away_* 应带标记 → /realtime/stop → 复原配置。
   前置：生产必须空闲(video=False 且 8080 无人)，否则拒跑。
   收尾：清掉本次合成场次的账本行(与 06t E2E 同口径)。"""
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
MARK_TEXT = "注入验证-马上回来"
MARK_IMG = "_smoke_bg.jpg"

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


s = requests.get(f"{HUB}/realtime/status", timeout=5).json()
if s.get("video"):
    bail("生产在播,拒绝 E2E")
try:
    requests.get(f"{RT}/swap/status", timeout=2)
    bail("8080 已有实例")
except requests.exceptions.RequestException:
    pass

print("[1/6] 存离席标记配置(style=image + 标记文案 + 品牌图)…", flush=True)
orig = (requests.get(f"{HUB}/api/effect_cfg", timeout=5).json().get("cfg") or {})
keep = {k: orig.get(k) for k in ("awayStyle", "awayText", "awayImage") if k in orig}
j = requests.post(f"{HUB}/api/effect_cfg", json={
    "awayStyle": "image", "awayText": MARK_TEXT, "awayImage": MARK_IMG}, timeout=5).json()
if not j.get("ok"):
    bail(f"配置保存失败 {j}")

print("[2/6] 拉起合成摄像头(8094)…", flush=True)
FF_PY = str(Path.home() / "Miniconda3" / "envs" / "facefusion" / "python.exe")
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

print("[3/6] POST /realtime/start(真生产 spawn 路径)…", flush=True)
j = requests.post(f"{HUB}/realtime/start", json={
    "source": f"http://127.0.0.1:{CAM_PORT}/stream",
    "width": 960, "height": 540, "swap_preset": "eco"}, timeout=30).json()
print(f"  start: {j}", flush=True)
v["start_ok"] = j.get("ok") is True

print("[4/6] 等 /swap/status 就绪并校验注入…", flush=True)
params = {}
deadline = time.time() + 60
while time.time() < deadline:
    try:
        params = requests.get(f"{RT}/swap/status", timeout=3).json().get("params") or {}
        if params:
            break
    except Exception:
        pass
    time.sleep(2)
v["away_text_injected"] = params.get("away_text") == MARK_TEXT
v["away_style_injected"] = params.get("away_style") == "image"
v["away_image_injected"] = str(params.get("away_image", "")).endswith(MARK_IMG)
print(f"  params: style={params.get('away_style')} text={params.get('away_text')!r} "
      f"image={params.get('away_image')!r}", flush=True)

print("[5/6] 停播 + 复原配置…", flush=True)
try:
    requests.post(f"{HUB}/realtime/stop", timeout=10)
except Exception:
    pass
restore = {"awayStyle": keep.get("awayStyle", "blur"),
           "awayText": keep.get("awayText", "稍等片刻 · Be right back"),
           "awayImage": keep.get("awayImage", "")}
r2 = requests.post(f"{HUB}/api/effect_cfg", json=restore, timeout=5).json()
v["cfg_restored"] = r2.get("ok") is True and (r2.get("cfg") or {}).get("awayText") == restore["awayText"]

print("[6/6] 清理本次合成场次账本(如已入账)…", flush=True)
for p in procs:
    try:
        p.kill()
    except Exception:
        pass
time.sleep(8)                       # 给 hub 收场聚合留时间
led = BASE / "logs" / "swap_sessions.jsonl"
removed = 0
if led.exists():
    import json as _json
    lines = led.read_text(encoding="utf-8").splitlines()
    kept = []
    for ln in lines:
        try:
            e = _json.loads(ln)
            if float(e.get("start", 0)) >= t0 and int(e.get("dur_s", 9999)) < 300:
                removed += 1
                continue
        except Exception:
            pass
        kept.append(ln)
    if removed:
        led.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
print(f"  账本清理: 移除 {removed} 条合成场次", flush=True)

print(f"结论: {v}", flush=True)
sys.exit(0 if all(v.values()) and len(v) == 5 else 1)
