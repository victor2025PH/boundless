# -*- coding: utf-8 -*-
"""看当前是否在播(实时换脸) + vcam/OBS 冲突风险评估。"""
import json
import urllib.request


def get(url):
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


try:
    h = get("http://127.0.0.1:9000/health")
    print("video_running:", h.get("video_running"))
    print("pressure:", h.get("pressure"))
    svc = h.get("services") or {}
    print("services:", {k: v for k, v in svc.items() if k in ("vcam", "lipsync", "fish_tts", "stt", "faceswap", "rvc")})
    print("broadcast:", json.dumps(h.get("broadcast"), ensure_ascii=False)[:300])
except Exception as e:
    print("health err:", e)

try:
    s = get("http://127.0.0.1:9000/realtime/status")
    print("realtime:", json.dumps(s, ensure_ascii=False)[:300])
except Exception as e:
    print("realtime err:", e)
