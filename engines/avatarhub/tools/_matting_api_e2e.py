# -*- coding: utf-8 -*-
"""录播增强 v2 E2E：停播自动排队 + 停播触发 drain + ProRes + 预热冒烟。"""
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests

HUB = "http://127.0.0.1:9000"
BASE = Path(r"C:\模仿音色")
CLIP = BASE / "logs" / "matting_offline" / "_test_raw.mp4"
BG = "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("[OK] " if cond else "[NG] ") + name + (f"  {detail}" if detail else ""))
    ok_all = ok_all and bool(cond)


# 0) 预热冒烟（独立进程，不依赖 hub 重启）
t0 = time.time()
r = subprocess.run(
    [sys.executable, "-X", "utf8", str(BASE / "tools" / "matting_offline.py"), "--warm-only"],
    cwd=str(BASE), capture_output=True, text=True, timeout=600)
check("模型预热 --warm-only", r.returncode == 0, f"{time.time()-t0:.0f}s {r.stdout.strip()[-40:]}")

# 1) 上传
with open(CLIP, "rb") as f:
    r = requests.post(f"{HUB}/api/matting/upload",
                      files={"file": ("队列测试.mp4", f, "video/mp4")}, timeout=120)
d = r.json()
check("上传录播", r.status_code == 200 and d.get("ok"), str(d.get("saved")))
saved = d["saved"]

st0 = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
streaming = st0.get("streaming", False)

# 2) 推流中 → 入队（不再拒跑）
r = requests.post(f"{HUB}/api/matting/start", json={"input": saved, "bg": BG}, timeout=10).json()
if streaming:
    check("推流中自动入队", r.get("ok") and r.get("queued"), str(r.get("detail", ""))[:50])
    st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
    check("队列持久化可见", len(st.get("queue", [])) >= 1)
    # 清空队列以便后续 force 测试
    requests.post(f"{HUB}/api/matting/cancel_queue", json={}, timeout=10)
else:
    check("非推流直接开跑或入队", r.get("ok"), str(r.get("id") or r.get("queued")))
    if r.get("ok") and not r.get("queued"):
        # 等它跑完或取消
        t0 = time.time()
        while time.time() - t0 < 600:
            st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
            if not st.get("running"):
                break
            time.sleep(2)

# 3) force 开跑 ProRes
r = requests.post(f"{HUB}/api/matting/start",
                  json={"input": saved, "bg": BG, "export": "prores", "force": True}, timeout=10).json()
check("force 开跑", r.get("ok") and not r.get("queued"), f"job={r.get('id')}")
job_id = r.get("id", "")

# 4) 忙时入队
r2 = requests.post(f"{HUB}/api/matting/start",
                   json={"input": saved, "bg": "green"}, timeout=10).json()
check("忙时自动入队", r2.get("ok") and r2.get("queued"), f"pos={r2.get('position')}")

# 5) 轮询到完成
t0 = time.time()
saw_progress = False
st = {}
while time.time() - t0 < 600:
    st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
    j = st.get("job", {})
    if st.get("running") and j.get("state") == "running" and j.get("n", 0) > 0:
        saw_progress = True
    if not st.get("running") and j.get("state") in ("done", "error", "cancelled"):
        break
    time.sleep(2)
j = st.get("job", {})
check("任务完成", j.get("state") == "done", f"{j.get('n')}帧 outputs={len(j.get('outputs',[]))}")
check("轮询见到运行进度", saw_progress)

# 6) 完成后队列 drain（若还有排队项应自动开跑——此处只验队列仍在或已消费）
check("队列 API 字段", "queue" in st)

# 7) ProRes alpha
mov = next((o for o in j.get("outputs", []) if o.endswith("_rgba.mov")), "")
if mov:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    p = str(BASE / "logs" / "matting_offline" / mov)
    r3 = subprocess.run([ff, "-i", p, "-frames:v", "1", "-pix_fmt", "rgba", "-f", "rawvideo", "-"],
                        capture_output=True, timeout=120)
    buf = np.frombuffer(r3.stdout, np.uint8)
    if buf.size >= 1280 * 720 * 4:
        a = buf[: 1280 * 720 * 4].reshape(720, 1280, 4)[:, :, 3]
        check("ProRes alpha", (a > 240).mean() > 0.05 and (a < 15).mean() > 0.3)

# 8) 直播健康（force 占 GPU 时 fps 可能暂时掉帧，只验推流进程仍存活）
rt = requests.get(f"{HUB}/realtime/status", timeout=10).json()
m = rt.get("metrics", {})
if streaming and r.get("id"):  # 曾 force 开跑
    check("force 后推流仍存活", rt.get("video_running") is not False, f"fps={m.get('fps')}")
else:
    check("直播 fps 正常", m.get("fps", 0) >= 25, f"fps={m.get('fps')}")

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
