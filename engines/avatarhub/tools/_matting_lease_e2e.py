# -*- coding: utf-8 -*-
"""显存租约 E2E：闲置时 LLM 常驻(~10.7G) → 提交录播任务 → 自动卸载 LLM 腾显存开跑
→ 跑完队列复原（引擎不缺员）。收尾把 LLM 按测试前状态回填。"""
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
OLLAMA = "http://127.0.0.1:11434"
BASE = Path(r"C:\模仿音色")
CLIP = BASE / "logs" / "matting_offline" / "_test_raw.mp4"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(("[OK] " if cond else "[NG] ") + name + (f"  {detail}" if detail else ""))
    ok_all = ok_all and bool(cond)


def ollama_ps():
    try:
        return [m.get("name") for m in requests.get(f"{OLLAMA}/api/ps", timeout=8).json().get("models", [])]
    except Exception:
        return []


def free_mb():
    import subprocess
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=10)
    return int(r.stdout.strip().splitlines()[0])


def status():
    return requests.get(f"{HUB}/api/matting/status", timeout=10).json()


st = status()
if st.get("streaming") or st.get("running"):
    print("SKIP: 推流中或有任务在跑")
    sys.exit(0)
requests.post(f"{HUB}/api/matting/cancel_queue", json={}, timeout=10)

# 0) 预置：确保 LLM 常驻（模拟白天直播后 8h keep_alive 钉住的状态）
before_models = ollama_ps()
if not before_models:
    print("加载 qwen2.5:14b (keep_alive=30m) 模拟常驻…")
    requests.post(f"{OLLAMA}/api/generate", json={"model": "qwen2.5:14b", "keep_alive": "30m"}, timeout=300)
    before_models = ollama_ps()
f0 = free_mb()
check("预置: LLM 常驻", bool(before_models), f"{before_models} 空闲={f0}MB")

# 1) 上传 + 提交。空闲 < 6G ⇒ 应走「入队+租约腾挪」路径
with open(CLIP, "rb") as f:
    up = requests.post(f"{HUB}/api/matting/upload",
                       files={"file": ("租约测试.mp4", f, "video/mp4")}, timeout=120).json()
name = up.get("saved", "")
check("上传", bool(name), name)
r = requests.post(f"{HUB}/api/matting/start", json={"input": name, "bg": "green"}, timeout=30).json()
print(f"  start → {r}")
lease_path = bool(r.get("queued"))
check("显存不足→自动转入队", r.get("ok") and lease_path if f0 < 6000 else r.get("ok"),
      r.get("detail", ""))

# 2) 等任务开跑：期间租约应把 LLM 卸掉
started, lease_seen = False, {}
t0 = time.time()
while time.time() - t0 < 180:
    st = status()
    if st.get("running") and st.get("job", {}).get("input") == name:
        started = True
        lease_seen = st.get("lease", {})
        break
    time.sleep(2)
check("任务自动开跑", started, f"{time.time() - t0:.0f}s")
mid_models = ollama_ps()
if lease_path:
    check("租约: LLM 已卸载", not mid_models and bool(lease_seen.get("llm")),
          f"lease.llm={lease_seen.get('llm')} ollama_ps={mid_models}")
    check("租约: 未动引擎(泊车空)", not lease_seen.get("parked"), str(lease_seen.get("parked")))

# 3) 等任务完成 → 队列空 → 租约复原（本例无泊车,验证账本清零 + 核心服务仍在线）
done = False
t0 = time.time()
while time.time() - t0 < 600:
    st = status()
    if not st.get("running") and st.get("job", {}).get("input") == name \
            and st.get("job", {}).get("state") in ("done", "error"):
        done = st["job"]["state"] == "done"
        break
    time.sleep(3)
check("任务完成", done, st.get("job", {}).get("state", ""))
time.sleep(3)
if lease_path:      # 任务已退出、LLM 未回填 → 此刻的空闲显存=租约净收益
    f2 = free_mb()
    check("租约: 空闲显存净提升", f2 > f0 + 4000, f"{f0}MB → {f2}MB")
st = status()
check("租约账本清零", not st.get("lease", {}).get("llm") and not st.get("lease", {}).get("parked"),
      str(st.get("lease")))
g = requests.get(f"{HUB}/api/gpu/status", timeout=15).json()
svc = g.get("gpu_services", {})
check("核心服务未受扰", svc.get("fish_tts") and svc.get("lipsync") and svc.get("ditto"),
      f"fish_tts={svc.get('fish_tts')} lipsync={svc.get('lipsync')} ditto={svc.get('ditto')}")

# 4) 收尾：LLM 回填到测试前状态（不影响之后的对话首响）
if before_models:
    for m in before_models:
        try:
            requests.post(f"{OLLAMA}/api/generate", json={"model": m, "keep_alive": "30m"}, timeout=300)
        except Exception:
            pass
    check("收尾: LLM 回填", set(ollama_ps()) >= set(before_models), str(ollama_ps()))

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
