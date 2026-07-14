#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Song-P3 真跑验收：
①O6 直播让路——手动挂起→翻唱任务挂起(人话 detail)→MV 409→放行→任务完成(yield_ms>0)
②O2 完整版——精细档走 Mel-Band 分离(sep_model_used=mel)
③礼物插队——gift 带歌名点歌+插队、gift 为已排队曲目插队
④播报开关——config 往返 + (服务在线时)真播报冒烟
跑法: facefusion python tools/_song_p3_e2e.py
"""
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"
ENG = "http://127.0.0.1:7853"
PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  [OK] {m}")


def ng(m):
    FAIL.append(m)
    print(f"  [NG] {m}")


def wait_engine(timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            j = requests.get(f"{ENG}/health", timeout=3).json()
            if j.get("service") == "song_studio":
                return j
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit("song_studio 未上线")


def submit_cover(quality="standard"):
    import base64
    lib = requests.get(f"{HUB}/api/song/library", timeout=5).json()["items"]
    assert lib, "曲库为空"
    fname = lib[0]["file"]
    song = requests.get(f"{HUB}/api/song/library", timeout=5)  # noqa: F841 (占位)
    import os
    p = os.path.join(r"c:\模仿音色\songs", fname)
    with open(p, "rb") as f:
        song_bytes = f.read()
    r = requests.post(f"{HUB}/api/song/cover",
                      files={"song": (fname, song_bytes, "audio/mpeg")},
                      data={"quality": quality}, timeout=120)
    assert r.status_code == 200, f"提交失败 {r.status_code}: {r.text[:200]}"
    return r.json()["task_id"]


def poll(tid, until=("done", "error"), timeout=600, want_detail=None):
    """轮询任务；want_detail 给定时，命中即返回当时状态。"""
    t0 = time.time()
    seen = None
    while time.time() - t0 < timeout:
        d = requests.get(f"{HUB}/api/song/task/{tid}", timeout=10).json()
        seen = d
        if want_detail and want_detail in (d.get("detail") or ""):
            return d
        if d.get("status") in until:
            return d
        time.sleep(2)
    return seen


def eng_task(tid):
    """timings/sep_model_used 等引擎侧字段在引擎任务上（hub 完成态返回自己的收尾视图）。"""
    try:
        return requests.get(f"{ENG}/v1/task/{tid}", timeout=10).json()
    except Exception:
        return {}


def clear_station():
    """清残留队列（上轮 e2e/演示留下的 ready 条目会干扰去重/礼物断言）。"""
    try:
        snap = requests.get(f"{HUB}/api/song/station", timeout=5).json()
        for it in snap.get("queue", []):
            rid = it.get("id")
            if it.get("status") in ("preparing", "playing", "queued"):
                requests.post(f"{HUB}/api/song/station/{rid}/cancel", timeout=5)
            requests.delete(f"{HUB}/api/song/station/{rid}", timeout=5)
    except Exception as e:
        print("  [warn] 清队列失败:", e)


def main():
    eng = wait_engine()
    print("引擎能力:", eng.get("capabilities"))

    # ── ① O6: 手动挂起 → 让路 ──
    r = requests.post(f"{HUB}/api/song/yield/hold",
                      params={"on": "true", "reason": "e2e 演练"}, timeout=5).json()
    (ok if r.get("yield") else ng)("hold 后 /api/song/yield=true")
    snap = requests.get(f"{HUB}/api/song/station", timeout=5).json()
    (ok if (snap.get("yield") or {}).get("yield") else ng)("点歌台快照带让路状态")

    tid = submit_cover("standard")
    print("  任务:", tid)
    d = poll(tid, want_detail="直播让路", timeout=60)
    (ok if "直播让路" in (d.get("detail") or "") else ng)(
        f"任务挂起+人话 detail（现在: {d.get('status')}/{d.get('detail')!r}）")

    # 放行 → 任务完成且 yield_ms>0（引擎侧计时）
    requests.post(f"{HUB}/api/song/yield/hold", params={"on": "false"}, timeout=5)
    d = poll(tid, timeout=600)
    st = d.get("status")
    hid = d.get("history_id")
    ym = ((eng_task(tid).get("result") or {}).get("timings") or {}).get("yield_ms", 0)
    (ok if st == "done" else ng)(f"放行后任务完成（{st}）")
    (ok if ym and ym > 1000 else ng)(f"yield_ms 记录让路耗时（{ym}ms）")

    # MV 直播让路中 409（用刚出炉的翻唱历史，确定性）
    if hid:
        requests.post(f"{HUB}/api/song/yield/hold",
                      params={"on": "true", "reason": "e2e MV 档"}, timeout=5)
        r = requests.post(f"{HUB}/api/song/mv", json={"history_id": hid}, timeout=30)
        (ok if r.status_code == 409 else ng)(f"MV 直播中默认拒(409)，实际 {r.status_code}")
        try:
            (ok if "直播让路" in r.json().get("detail", "") else ng)("MV 409 人话原因")
        except Exception:
            ng("MV 409 人话原因")
        requests.post(f"{HUB}/api/song/yield/hold", params={"on": "false"}, timeout=5)
    else:
        ng("翻唱完成应带 history_id（MV 409 验证依赖它）")

    # ── ② O2: 精细档 → Mel-Band ──
    tid2 = submit_cover("fine")
    print("  精细档任务:", tid2)
    d2 = poll(tid2, timeout=900)
    res2 = (eng_task(tid2).get("result") or {})
    (ok if d2.get("status") == "done" else ng)(f"精细档完成（{d2.get('status')}/{d2.get('detail')}）")
    (ok if res2.get("sep_model_used") == "mel" else ng)(
        f"精细档用 Mel-Band 分离（实际: {res2.get('sep_model_used')}）")
    print(f"  精细档耗时 {res2.get('elapsed_ms', 0)/1000:.0f}s "
          f"separate={(res2.get('timings') or {}).get('separate_ms')}ms "
          f"cache_hit={res2.get('sep_cache_hit')}")

    # ── ③ 礼物插队 ──
    requests.post(f"{HUB}/api/song/station/config",
                  json={"enabled": True, "auto_prepare": False}, timeout=5)
    clear_station()
    lib = requests.get(f"{HUB}/api/song/library", timeout=5).json()["items"]
    song_name = lib[0]["name"]
    tag = str(int(time.time()))[-5:]
    g1 = requests.post(f"{HUB}/api/song/station/gift",
                       json={"name": f"礼物哥{tag}", "gift": "火箭", "value": 500,
                             "song": song_name}, timeout=10).json()
    (ok if g1.get("ok") and g1.get("id") else ng)(f"礼物带歌名→入队+插队（{g1.get('message')}）")
    rid = g1.get("id")
    g2 = requests.post(f"{HUB}/api/song/station/gift",
                       json={"name": f"礼物哥{tag}", "gift": "小心心", "value": 1},
                       timeout=10).json()
    (ok if g2.get("ok") and g2.get("id") == rid else ng)(
        f"礼物不带歌名→为已排队曲目插队（{g2.get('message')}）")
    g3 = requests.post(f"{HUB}/api/song/station/gift",
                       json={"name": f"路人{tag}", "gift": "灯牌"}, timeout=10).json()
    (ok if not g3.get("ok") and "没有排队中的点歌" in (g3.get("reason") or "") else ng)(
        f"无排队曲目的礼物→人话拒（{g3.get('reason')}）")
    if rid:
        requests.post(f"{HUB}/api/song/station/{rid}/cancel", timeout=5)
        requests.delete(f"{HUB}/api/song/station/{rid}", timeout=5)

    # ── ④ 播报开关往返 ──
    r = requests.post(f"{HUB}/api/song/station/config",
                      json={"announce": True}, timeout=5).json()
    (ok if r.get("announce") is True else ng)("announce 开关下发")
    snap = requests.get(f"{HUB}/api/song/station", timeout=5).json()
    (ok if snap.get("announce") is True else ng)("announce 快照回读")
    requests.post(f"{HUB}/api/song/station/config",
                  json={"announce": False, "auto_prepare": True}, timeout=5)

    print(f"\n{'='*46}\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print(" [NG]", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
