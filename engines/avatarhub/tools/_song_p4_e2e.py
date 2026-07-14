# -*- coding: utf-8 -*-
"""Song-P4 真跑验收：原创歌全链路（Hub 编排）。

覆盖：
  ①能力探测：/api/song/health.create 如实上报
  ②纯生成：POST /api/song/create（30s turbo）→ 轮询 → done（水印+历史入库+可播）
  ③让路联动：hold 后提交 → 任务 detail 显示让路 → 放行 → 完成且 yield_ms>0
  ④换声编排：svc_swap=true → gen(0~50%) → swap(52~100%) → done（贴合度+[原创]前缀）
  ⑤歌词辅写：POST /api/song/lyrics_assist（没配 LLM 时 503 人话=PASS 降级分支）
  ⑥MV 60s 窗：seconds=45 提交不被砍到 30（返回 seconds=45）

用法： ymsvc python tools/_song_p4_e2e.py
"""
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"
ENG = "http://127.0.0.1:7859"

PASS = []
FAIL = []


def _req(method, url, *, retries=6, backoff=5.0, **kw):
    """带瞬断重试的请求：Hub 被 mem_watchdog 拉起需要 ~20s，期间连接拒绝
    不该把验收脚本砸死（2026-07-07 真跑实锤：hub 崩溃重启导致轮询裸崩）。"""
    last = None
    for i in range(retries):
        try:
            return requests.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            if i < retries - 1:
                print(f"    ..连接失败({type(e).__name__})，{backoff:.0f}s 后重试 {i+1}/{retries-1}")
                time.sleep(backoff)
    raise last


def ok(label, extra=""):
    PASS.append(label)
    print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))


def ng(label, extra=""):
    FAIL.append(label)
    print(f"  [FAIL] {label}" + (f"  {extra}" if extra else ""))


LYRICS = """[verse]
清晨的光洒在窗台
咖啡冒着热气
新的一天刚刚醒来
你哼着小曲

[chorus]
唱吧唱吧 大声地唱
这是我们自己的歌
"""
STYLE = "pop, mandarin, female vocal, warm, acoustic guitar, 90 bpm, uplifting"


def wait_create(tid, timeout=900, want_stage=None):
    """轮询 /api/song/create/{tid} 到终态；返回最后一份状态。"""
    t0 = time.time()
    last = {}
    seen_yield_detail = False
    while time.time() - t0 < timeout:
        r = _req("GET", f"{HUB}/api/song/create/{tid}", timeout=30)
        if r.status_code != 200:
            return {"status": "http_error", "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
        last = r.json()
        if "直播让路中" in (last.get("detail") or ""):
            seen_yield_detail = True
        if last.get("status") in ("done", "error", "cancelled"):
            last["_seen_yield_detail"] = seen_yield_detail
            return last
        time.sleep(2.0)
    last["_timeout"] = True
    return last


def main():
    # ① 能力探测
    print("== ① 能力探测 ==")
    h = _req("GET", f"{HUB}/api/song/health", timeout=10).json()
    c = h.get("create") or {}
    if c.get("online") and (c.get("capabilities") or {}).get("create"):
        ok("health.create 在线且能力开", f"engine={c.get('engine')}")
    else:
        ng("health.create 在线且能力开", str(c))
        print("引擎不在线，后续无法验收")
        return

    # 确保干净：让路 hold 复位
    _req("POST", f"{HUB}/api/song/yield/hold", params={"on": False}, timeout=10)

    # ② 纯生成（30s turbo）
    print("== ② 纯生成 30s turbo ==")
    r = _req("POST", f"{HUB}/api/song/create", json={
        "style": STYLE, "lyrics": LYRICS, "duration_s": 30,
        "quality": "turbo", "svc_swap": False, "song_name": "P4E2E纯生成",
    }, timeout=30)
    if r.status_code != 200:
        ng("纯生成提交", f"HTTP {r.status_code}: {r.text[:200]}")
        return
    tid = r.json()["task_id"]
    st = wait_create(tid, timeout=600)
    if st.get("status") == "done":
        okflags = (st.get("history_id") and st.get("audio_url")
                   and st.get("engine_used") == "ace_step")
        (ok if okflags else ng)(
            "纯生成完成(历史+audio_url+引擎标注)",
            f"hist={st.get('history_id')} rtf={st.get('rtf')} "
            f"timings={st.get('timings')}")
        # 成品可下载且非空
        a = _req("GET", f"{HUB}{st['audio_url']}", timeout=60)
        (ok if a.status_code == 200 and len(a.content) > 200_000 else ng)(
            "成品音频可播", f"{len(a.content)//1024}KB")
        hist_pure = st.get("history_id")
    else:
        ng("纯生成完成", str(st)[:300])
        hist_pure = None

    # ③ 让路联动：hold → 提交 → 观察挂起 → 放行
    print("== ③ 让路联动 ==")
    _req("POST", f"{HUB}/api/song/yield/hold",
                  params={"on": True, "reason": "P4 e2e 演练"}, timeout=10)
    r = _req("POST", f"{HUB}/api/song/create", json={
        "style": STYLE, "lyrics": "", "duration_s": 15,
        "quality": "turbo", "svc_swap": False, "song_name": "P4E2E让路",
    }, timeout=30)
    tid2 = r.json().get("task_id", "")
    time.sleep(8)                     # 引擎轮询 5s 一次，等它看到 hold
    st_mid = _req("GET", f"{HUB}/api/song/create/{tid2}", timeout=10).json()
    yielding = "直播让路中" in (st_mid.get("detail") or "")
    (ok if yielding else ng)("hold 后任务挂起(人话 detail)", st_mid.get("detail", ""))
    _req("POST", f"{HUB}/api/song/yield/hold", params={"on": False}, timeout=10)
    st2 = wait_create(tid2, timeout=600)
    if st2.get("status") == "done":
        eng_t = _req("GET", f"{ENG}/v1/task/{tid2}", timeout=10).json()
        ym = ((eng_t.get("result") or {}).get("timings") or {}).get("yield_ms", 0)
        (ok if ym and ym > 3000 else ng)("放行后完成且 yield_ms 可观测", f"yield_ms={ym}")
    else:
        ng("让路任务放行后完成", str(st2)[:300])

    # ④ 换声编排（gen→swap 两阶段）
    print("== ④ 换声编排(svc_swap) ==")
    r = _req("POST", f"{HUB}/api/song/create", json={
        "style": STYLE, "lyrics": LYRICS, "duration_s": 30,
        "quality": "turbo", "svc_swap": True, "song_name": "P4E2E换声",
    }, timeout=30)
    if r.status_code != 200:
        d = ""
        try:
            d = r.json().get("detail", "")
        except Exception:
            pass
        # 没克隆音的角色会人话拒——也算合法分支，但完整验收要求角色有克隆音
        ng("换声提交", f"HTTP {r.status_code} {d[:120]}")
    else:
        tid3 = r.json()["task_id"]
        saw_swap_stage = False
        t0 = time.time()
        st3 = {}
        while time.time() - t0 < 1200:
            st3 = _req("GET", f"{HUB}/api/song/create/{tid3}", timeout=30).json()
            if st3.get("stage") == "swap" and st3.get("status") not in ("done", "error"):
                saw_swap_stage = True
            if st3.get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(2.0)
        if st3.get("status") == "done":
            ok("换声完成", f"hist={st3.get('history_id')} sim={st3.get('similarity')} "
                          f"engine={st3.get('engine_used')}")
            (ok if saw_swap_stage or st3.get("stage") == "swap" else ng)(
                "两阶段可观测(swap 段出现过)")
            (ok if st3.get("engine_used") == "ace_step+yingmusic_svc" else ng)(
                "引擎如实标注 ace_step+yingmusic_svc", st3.get("engine_used", ""))
            (ok if st3.get("similarity") else ng)(
                "换声版有贴合度评分", str(st3.get("similarity")))
            (ok if st3.get("gen_timings") else ng)(
                "gen 阶段耗时保留", str(st3.get("gen_timings")))
        else:
            ng("换声完成", str(st3)[:300])

    # ⑤ 歌词辅写（没配 LLM = 503 人话，也算 PASS 的降级分支）
    print("== ⑤ 歌词辅写 ==")
    r = _req("POST", f"{HUB}/api/song/lyrics_assist", json={
        "topic": "写给凌晨还在写代码的人", "style": STYLE, "duration_s": 60,
    }, timeout=120)
    if r.status_code == 200:
        ly = r.json().get("lyrics", "")
        (ok if "[" in ly and len(ly) > 30 else ng)(
            "LLM 写词(结构标记齐)", ly[:60].replace("\n", " / "))
    elif r.status_code == 503:
        ok("没配 LLM → 503 人话降级(不硬依赖)", r.json().get("detail", "")[:60])
    else:
        ng("歌词辅写", f"HTTP {r.status_code}: {r.text[:200]}")

    # ⑥ MV 60s 窗（用纯生成的历史成品，45s 请求不被砍到 30）
    print("== ⑥ MV 60s 窗 ==")
    if hist_pure:
        r = _req("POST", f"{HUB}/api/song/mv", json={
            "history_id": hist_pure, "seconds": 45,
        }, timeout=900)
        if r.status_code == 200:
            d = r.json()
            (ok if abs(d.get("seconds", 0) - 45) < 1 else ng)(
                "45s 请求如实执行(上限已放宽到 60)",
                f"seconds={d.get('seconds')} 用时{d.get('elapsed_ms', 0)/1000:.0f}s")
        elif r.status_code == 409:
            ok("MV 让路 409(直播中人话拒)", r.json().get("detail", "")[:60])
        else:
            ng("MV 45s", f"HTTP {r.status_code}: {r.text[:200]}")
    else:
        ng("MV 45s(无纯生成历史可用)")

    print(f"\n结果: PASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print("  FAIL -", f)
    print("RESULT:", "OK" if not FAIL else "FAIL")


if __name__ == "__main__":
    main()
