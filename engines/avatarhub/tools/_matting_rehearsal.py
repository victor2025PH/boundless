# -*- coding: utf-8 -*-
"""真实规模彩排 + 孤儿收养验收：
3 分钟 1080p 视频走完整 API 链路（上传→租约腾挪→ProRes 全量导出），
跑到一半硬杀 hub（模拟崩溃）→ 重启 → 断言任务被收养续管、不双跑、正常归档。
收尾回填 LLM。"""
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

HUB = "http://127.0.0.1:9000"
BASE = Path(r"C:\模仿音色")
CLIP = BASE / "logs" / "matting_offline" / "_rehearsal_1080p_3m.mp4"
PY = r"C:\Users\user\miniconda3\envs\facefusion\python.exe"
BG = "u=3803441414,2373243379&fm=253&fmt=auto&app=138&f=JPEG.webp"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("[OK] " if cond else "[NG] ") + name + (f"  {detail}" if detail else ""), flush=True)
    ok_all = ok_all and bool(cond)


def status():
    return requests.get(f"{HUB}/api/matting/status", timeout=10).json()


def hub_pids():
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        if "avatar_hub.py" in cl and "python" in cl.lower():
            out.append(p.info["pid"])
    return out


def matting_pids():
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        if "matting_offline.py" in cl:
            out.append(p.info["pid"])
    return out


st = status()
if st.get("streaming") or st.get("running"):
    print("SKIP: 推流中或有任务在跑")
    sys.exit(0)
requests.post(f"{HUB}/api/matting/cancel_queue", json={}, timeout=10)

# 1) 上传 3 分钟 1080p 彩排片（~70MB）
t_up = time.time()
with open(CLIP, "rb") as f:
    up = requests.post(f"{HUB}/api/matting/upload",
                       files={"file": ("彩排1080p.mp4", f, "video/mp4")}, timeout=600).json()
name = up.get("saved", "")
check("上传 70MB", bool(name), f"{name} 耗时{time.time() - t_up:.0f}s")

# 2) 提交（图片背景 + ProRes 全量导出 = 最重路径）
r = requests.post(f"{HUB}/api/matting/start",
                  json={"input": name, "bg": BG, "export": "prores"}, timeout=30).json()
check("提交", r.get("ok"), "queued(租约)" if r.get("queued") else "直跑")

# 3) 等开跑并推进到 ≥80 帧
job_id = ""
t0 = time.time()
while time.time() - t0 < 300:
    st = status()
    j = st.get("job", {})
    if st.get("running") and j.get("input") == name and j.get("n", 0) >= 80:
        job_id = j.get("id", "")
        check("开跑且推进", True,
              f"id={job_id} n={j.get('n')}/{j.get('total')} {j.get('ms')}ms/帧")
        break
    time.sleep(3)
if not job_id:
    check("开跑且推进", False, "300s 未达标")
    sys.exit(1)

# 4) 硬杀 hub（模拟崩溃）→ 子进程应继续跑
for pid in hub_pids():
    psutil.Process(pid).kill()
time.sleep(4)
alive = matting_pids()
check("hub 已死而任务犹在", not hub_pids() and len(alive) == 1, f"matting pid={alive}")

# 5) 重启 hub → 应收养孤儿，且不双跑
subprocess.Popen([PY, "-X", "utf8", str(BASE / "avatar_hub.py")], cwd=str(BASE),
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 creationflags=0x00000008 | 0x00000200 | 0x08000000)  # DETACHED|NEW_GROUP|NO_WINDOW
t0 = time.time()
adopted = {}
while time.time() - t0 < 90:
    try:
        st = status()
        j = st.get("job", {})
        if st.get("running") and j.get("id") == job_id:
            adopted = j
            break
    except Exception:
        pass
    time.sleep(3)
check("重启后收养同一任务", adopted.get("id") == job_id,
      f"adopted={adopted.get('adopted')} n={adopted.get('n')}")
time.sleep(12)      # 给 drain/对账器留出误判双跑的窗口
check("未双跑(仅1个子进程)", len(matting_pids()) == 1, str(matting_pids()))
n1 = status().get("job", {}).get("n", 0)
time.sleep(10)
n2 = status().get("job", {}).get("n", 0)
check("收养后进度仍在推进", n2 > n1, f"{n1}→{n2}")

# 6) 等跑完（1080p ProRes 全量，给足时间），断言归档与产物
t0 = time.time()
final = {}
while time.time() - t0 < 2400:
    st = status()
    j = st.get("job", {})
    if not st.get("running") and j.get("id") == job_id and j.get("state") in ("done", "error"):
        final = j
        break
    n = j.get("n", 0)
    if n and int(time.time() - t0) % 60 < 3:
        print(f"  … {n}/{j.get('total')} {j.get('ms')}ms/帧 eta~{j.get('eta_s')}s", flush=True)
    time.sleep(3)
check("任务完成", final.get("state") == "done",
      f"{final.get('n')}帧 {final.get('ms')}ms/帧")
outs = final.get("outputs") or []
check("产物齐全(com+pha+rgba)", len(outs) == 3, str(outs))
for o in outs:
    p = BASE / "logs" / "matting_offline" / o
    print(f"  [out] {o}  {p.stat().st_size / 1048576:.0f}MB", flush=True)
hist = status().get("history", [])
check("已归档进历史", any(h.get("id") == job_id for h in hist))

# 7) 收尾：LLM 回填（租约测试卸掉了它）
try:
    requests.post("http://127.0.0.1:11434/api/generate",
                  json={"model": "qwen2.5:14b", "keep_alive": "8h"}, timeout=300)
    print("[teardown] LLM 已回填", flush=True)
except Exception as e:
    print(f"[teardown] LLM 回填失败: {e}", flush=True)

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
