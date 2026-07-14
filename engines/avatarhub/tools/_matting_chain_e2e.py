# -*- coding: utf-8 -*-
"""录播增强 v3 E2E：批量提交→任务完成后队列自动接力→全部跑完；上传触发预热限频。
前提：未推流（推流场景由 _matting_api_e2e.py 覆盖）。"""
import sys
import time
from pathlib import Path

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


def status():
    return requests.get(f"{HUB}/api/matting/status", timeout=10).json()


st = status()
if st.get("streaming"):
    print("SKIP: 正在推流，链式接力测试需要停播后跑")
    sys.exit(0)
if st.get("running"):
    print("等待在跑任务结束…")
    t0 = time.time()
    while status().get("running") and time.time() - t0 < 600:
        time.sleep(3)
requests.post(f"{HUB}/api/matting/cancel_queue", json={}, timeout=10)

# 1) 上传两个测试片
names = []
for i in range(2):
    with open(CLIP, "rb") as f:
        r = requests.post(f"{HUB}/api/matting/upload",
                          files={"file": (f"链式测试{i+1}.mp4", f, "video/mp4")}, timeout=120)
    d = r.json()
    check(f"上传第{i+1}个", r.status_code == 200 and d.get("ok"), str(d.get("saved")))
    names.append(d["saved"])

# 2) 批量提交：空闲时第一个立即开跑（显存不足则先入队+租约腾挪后自动开跑），第二个排队
r1 = requests.post(f"{HUB}/api/matting/start", json={"input": names[0], "bg": BG}, timeout=15).json()
check("任务A 提交成功", r1.get("ok"), "queued(租约腾挪中)" if r1.get("queued") else f"直跑 id={r1.get('id')}")
r2 = requests.post(f"{HUB}/api/matting/start", json={"input": names[1], "bg": "green"}, timeout=15).json()
check("任务B 排队", r2.get("ok") and r2.get("queued"), f"pos={r2.get('position')}")

id_a = r1.get("id", "")
if r1.get("queued"):     # 租约路径：等 A 被自动拉起拿到 id
    t0 = time.time()
    while time.time() - t0 < 120:
        st = status()
        j = st.get("job", {})
        if st.get("running") and j.get("input") == names[0]:
            id_a = j.get("id", "")
            check("任务A 租约后自动开跑", True, f"{time.time() - t0:.0f}s 后开跑 id={id_a}")
            break
        time.sleep(2)
    if not id_a:
        check("任务A 租约后自动开跑", False, "120s 未开跑")

# 3) 等 A 完成 → B 应在 ~15s 内被 drain 自动接力
t0 = time.time()
a_done_ts = 0.0
b_started = False
b_state = {}
while time.time() - t0 < 900:
    st = status()
    j = st.get("job", {})
    if not a_done_ts and j.get("id") == id_a and j.get("state") in ("done", "error") \
            and not st.get("running"):
        a_done_ts = time.time()
        check("任务A 完成", j.get("state") == "done", f"{j.get('n')}帧")
    if a_done_ts and j.get("id") != id_a and j.get("input") == names[1]:
        b_started = True
        wait_s = time.time() - a_done_ts
        check("任务B 自动接力", True, f"A完成后 {wait_s:.0f}s 自动开跑")
        # 等 B 也完成
        while time.time() - t0 < 900:
            st = status()
            j2 = st.get("job", {})
            if not st.get("running") and j2.get("input") == names[1] \
                    and j2.get("state") in ("done", "error", "cancelled"):
                b_state = j2
                break
            time.sleep(2)
        break
    time.sleep(2)
check("任务B 完成", b_state.get("state") == "done", f"{b_state.get('n')}帧")
if not b_started:
    check("任务B 自动接力", False, "900s 内未接力")

# 4) 收尾状态
st = status()
check("队列已清空", len(st.get("queue", [])) == 0)
hist_inputs = [h.get("input") for h in st.get("history", [])]
check("历史含 A+B", names[0] in hist_inputs and names[1] in hist_inputs)

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
