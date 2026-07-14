# -*- coding: utf-8 -*-
"""收尾小工具：清点歌台残留测试条目（保留 ready 成品曲），打印最终状态。"""
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"

s = requests.get(f"{HUB}/api/song/station", timeout=5).json()
for it in s["queue"]:
    if it["status"] not in ("ready",):
        rid = it["id"]
        requests.post(f"{HUB}/api/song/station/{rid}/cancel", timeout=5)
        requests.delete(f"{HUB}/api/song/station/{rid}", timeout=5)
s2 = requests.get(f"{HUB}/api/song/station", timeout=5).json()
print("队列剩余:", [(x["id"], x["status"], x["song_name"]) for x in s2["queue"]])
print("yield:", s2["yield"])
print("announce:", s2["announce"], "| enabled:", s2["enabled"])
