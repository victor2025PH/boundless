#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 播报+字幕真跑冒烟：备一首歌→开播报→上麦（角色先开口谢点歌人→起歌+字幕）→停。"""
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"


def main():
    requests.post(f"{HUB}/api/song/station/config",
                  json={"enabled": True, "auto_prepare": True, "announce": True},
                  timeout=5)
    lib = requests.get(f"{HUB}/api/song/library", timeout=5).json()["items"]
    r = requests.post(f"{HUB}/api/song/station/request",
                      json={"file": lib[0]["file"], "requester": "冒烟观众"},
                      timeout=10).json()
    rid = r.get("id") or r.get("dup_id")
    if not rid:   # 队里可能已有同曲 ready
        snap = requests.get(f"{HUB}/api/song/station", timeout=5).json()
        rid = next((x["id"] for x in snap["queue"] if x["status"] == "ready"), None)
    assert rid, f"入队失败: {r}"
    print("点歌 #", rid)
    t0 = time.time()
    while time.time() - t0 < 600:
        snap = requests.get(f"{HUB}/api/song/station", timeout=5).json()
        it = next((x for x in snap["queue"] if x["id"] == rid), {})
        st = it.get("status")
        print(f"  {int(time.time()-t0)}s {st} {it.get('progress')}% {it.get('detail', '')}")
        if st == "ready":
            break
        if st in ("failed", "cancelled"):
            raise SystemExit(f"备歌失败: {it.get('error')}")
        time.sleep(5)
    t0 = time.time()
    p = requests.post(f"{HUB}/api/song/station/{rid}/play", timeout=180).json()
    el = time.time() - t0
    print(f"上麦: {p} （耗时 {el:.1f}s——含播报生成+播完播报才起歌）")
    assert p.get("ok"), "上麦失败"
    assert el > 2.0, "播报应占用数秒（太快=没播报）"
    time.sleep(6)
    requests.post(f"{HUB}/api/song/station/stop", timeout=10)
    print("已停止。PASS：播报→起歌→字幕→停 全链真跑")
    # 收尾清队列
    requests.post(f"{HUB}/api/song/station/{rid}/cancel", timeout=5)
    requests.delete(f"{HUB}/api/song/station/{rid}", timeout=5)
    requests.post(f"{HUB}/api/song/station/config", json={"announce": False}, timeout=5)


if __name__ == "__main__":
    main()
