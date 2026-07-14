# -*- coding: utf-8 -*-
"""Song-P2 点歌台真跑验收：
①开台+曲库点歌(弹幕入口) ②自动备歌→ready(水印/历史/贴合度同权)
③重复点歌去重+冷却防刷 ④上麦(vcam play_audio)→停止
⑤二次点同曲 → 引擎分离缓存命中(separate_ms=0)
⑥15s MV(副歌选段+口型) — lipsync 在线才跑
用法: python tools/_station_e2e.py [--skip-mv]
"""
import sys
import time
import json
import requests

HUB = "http://127.0.0.1:9000"
SONG_FILE = "圣诞快乐歌.mp3"


def jprint(tag, d):
    print(tag, json.dumps(d, ensure_ascii=False)[:300])


def wait_status(rid, want, timeout=600):
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        s = requests.get(f"{HUB}/api/song/station", timeout=10).json()
        r = next((x for x in s.get("queue", []) if x["id"] == rid), None)
        if r is None:
            print("  ! 请求消失"); return None
        cur = f"{r['status']} {r.get('progress',0)}% {r.get('detail','')}"
        if cur != last:
            print(f"  [{int(time.time()-t0):>3}s] #{rid} {cur}")
            last = cur
        if r["status"] in want:
            return r
        if r["status"] == "failed":
            print("  ! failed:", r.get("error")); return r
        time.sleep(3)
    print("  ! 超时"); return None


def main():
    skip_mv = "--skip-mv" in sys.argv
    tag = str(int(time.time()))[-5:]          # 每次重跑观众名唯一，避开 120s 冷却残留

    # ── ① 开台 ──
    d = requests.post(f"{HUB}/api/song/station/config",
                      json={"enabled": True, "chat_enabled": True,
                            "auto_prepare": True, "auto_play": False,
                            "quality": "standard"}, timeout=10).json()
    jprint("[config]", d)
    assert d.get("enabled") is True

    st = requests.get(f"{HUB}/api/song/station", timeout=10).json()
    lib = [it["file"] for it in st.get("library", [])]
    print("[library]", lib)
    assert SONG_FILE in lib, "曲库缺测试歌"

    # 清掉历史队列（幂等重跑）
    for r in st.get("queue", []):
        if r["status"] in ("queued", "preparing"):
            requests.post(f"{HUB}/api/song/station/{r['id']}/cancel", timeout=10)
    for r in st.get("queue", []):
        if r["status"] in ("done", "failed", "cancelled", "ready"):
            requests.delete(f"{HUB}/api/song/station/{r['id']}", timeout=10)

    # ── ② 弹幕点歌（模糊匹配：故意不写全名） ──
    d = requests.post(f"{HUB}/api/song/station/chat",
                      json={"text": "点歌 圣诞快乐", "name": f"测试观众{tag}"}, timeout=10).json()
    jprint("[chat点歌]", d)
    assert d.get("ok") and d.get("matched"), "弹幕点歌未匹配"
    rid = d["id"]

    # ── ③ 防刷：同人冷却 + 同曲去重 ──
    d2 = requests.post(f"{HUB}/api/song/station/chat",
                       json={"text": "点歌 圣诞快乐歌", "name": f"测试观众{tag}"}, timeout=10).json()
    jprint("[冷却拦截]", d2)
    assert not d2.get("ok") and "点太快" in d2.get("reason", ""), "冷却未生效"
    d3 = requests.post(f"{HUB}/api/song/station/chat",
                       json={"text": "点歌 圣诞快乐歌", "name": f"另一位观众{tag}"}, timeout=10).json()
    jprint("[同曲去重]", d3)
    assert not d3.get("ok") and "已在队列" in d3.get("reason", ""), "同曲去重未生效"
    d4 = requests.post(f"{HUB}/api/song/station/chat",
                       json={"text": "点歌 不存在的歌曲名XYZ", "name": f"第三位观众{tag}"}, timeout=10).json()
    jprint("[无此歌]", d4)
    assert not d4.get("ok") and d4.get("matched"), "未匹配曲库应人话拒绝"
    d5 = requests.post(f"{HUB}/api/song/station/chat",
                       json={"text": "主播唱得真好", "name": f"路人{tag}"}, timeout=10).json()
    assert not d5.get("matched"), "普通弹幕不该被当点歌"
    print("[防刷/解析] 全部通过")

    # ── ④ 自动备歌 → ready ──
    r = wait_status(rid, ("ready",), timeout=900)
    assert r and r["status"] == "ready", "备歌未就绪"
    assert r.get("hist_id"), "缺 hist_id(历史入库)"
    assert r.get("audio_url"), "缺 audio_url"
    print("[备歌] ready hist_id=", r["hist_id"], "similarity=", r.get("similarity"))
    a = requests.get(f"{HUB}{r['audio_url']}", timeout=30)
    print("[成品直链]", a.status_code, a.headers.get("content-type"), len(a.content), "bytes")
    assert a.status_code == 200 and "audio/wav" in a.headers.get("content-type", "")

    # ── ⑤ 上麦 → 播放中 → 停止 ──
    d = requests.post(f"{HUB}/api/song/station/{rid}/play", timeout=60).json()
    jprint("[上麦]", d)
    assert d.get("ok") and d.get("duration_s", 0) > 0
    st = requests.get(f"{HUB}/api/song/station", timeout=10).json()
    assert st.get("playing_id") == rid, "playing_id 未置位"
    cur = next(x for x in st["queue"] if x["id"] == rid)
    assert cur["status"] == "playing"
    print("[播放中] duration_s=", d["duration_s"])
    time.sleep(4)
    d = requests.post(f"{HUB}/api/song/station/stop", timeout=15).json()
    jprint("[停止]", d)
    st = requests.get(f"{HUB}/api/song/station", timeout=10).json()
    cur = next(x for x in st["queue"] if x["id"] == rid)
    assert st.get("playing_id") is None and cur["status"] == "ready", "停止后应回 ready"
    print("[切歌] 停止 → ready 通过")

    # ── ⑥ 二次点同曲 → 分离缓存命中 ──
    # 产品语义：ready/排队中的同曲会被去重挡下（防队列刷屏）；
    # 换音色重备的正规路径是删掉旧条目再点 → 这里照此走，同时正好验证缓存命中。
    dd = requests.delete(f"{HUB}/api/song/station/{rid}", timeout=10).json()
    jprint("[删除旧条目]", dd)
    assert dd.get("ok"), "ready 条目应可删除"
    eng0 = requests.get(f"{HUB}/api/song/health", timeout=10).json()
    print("[引擎]", json.dumps(eng0, ensure_ascii=False)[:200])
    d = requests.post(f"{HUB}/api/song/station/request",
                      json={"file": SONG_FILE, "requester": "缓存验证"}, timeout=10).json()
    jprint("[二次点歌]", d)
    assert d.get("ok"), "删除后同曲应可再点"
    rid2 = d["id"]
    r2 = wait_status(rid2, ("ready",), timeout=900)
    assert r2 and r2["status"] == "ready", "二次备歌未就绪"
    # 直接查引擎任务的 timings
    st2 = requests.get(f"{HUB}/api/song/station", timeout=10).json()
    q2 = next(x for x in st2["queue"] if x["id"] == rid2)
    tid2 = q2.get("task_id", "")
    eng = requests.get(f"http://127.0.0.1:7853/v1/task/{tid2}", timeout=10)
    if eng.status_code == 200:
        res = eng.json().get("result") or {}
        print("[缓存命中]", "sep_cache_hit=", res.get("sep_cache_hit"),
              "separate_ms=", (res.get("timings") or {}).get("separate_ms"))
        assert res.get("sep_cache_hit") is True, "二次翻唱应命中分离缓存"
        assert (res.get("timings") or {}).get("separate_ms", 9e9) == 0
    else:
        print("[缓存命中] 引擎任务已过期，跳过 timings 校验")

    # ── ⑦ 15s MV ──
    if skip_mv:
        print("[MV] --skip-mv 跳过")
    else:
        try:
            ls = requests.get("http://127.0.0.1:8090/health", timeout=5)
            ls_ok = ls.status_code == 200
        except Exception:
            ls_ok = False
        if not ls_ok:
            print("[MV] lipsync 8090 不在线，跳过（不判失败）")
        else:
            t0 = time.time()
            d = requests.post(f"{HUB}/api/song/mv",
                              json={"history_id": r["hist_id"]}, timeout=700).json()
            jprint("[MV]", d)
            assert d.get("ok") and d.get("url"), "MV 生成失败"
            v = requests.get(f"{HUB}{d['url']}", timeout=60)
            print("[MV成片]", v.status_code, v.headers.get("content-type"),
                  f"{len(v.content)//1024}KB", f"{time.time()-t0:.0f}s")
            assert v.status_code == 200 and "video/mp4" in v.headers.get("content-type", "")

    print("\nSTATION E2E: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
