# -*- coding: utf-8 -*-
"""定位刘德华 cos 掉线是「合成变了」还是「打分变了」：
用固定文本合成两遍(确定性 seed)看输出是否自稳定，再对同一份音频重复打分看打分稳定性。"""
import json
import urllib.request

HUB = "http://127.0.0.1:9000"


def post(path, payload, timeout=120):
    req = urllib.request.Request(HUB + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


TEXT = "你好，我是刘德华，很高兴在这里和大家见面聊天。"
r1 = post("/api/tts_only", {"profile": "刘德华", "text": TEXT, "language": "zh-cn"})
r2 = post("/api/tts_only", {"profile": "刘德华", "text": TEXT, "language": "zh-cn"})
a1, a2 = r1.get("audio_base64", ""), r2.get("audio_base64", "")
print("len1=", len(a1), "len2=", len(a2), "identical=", a1 == a2)
for i, a in enumerate((a1, a2), 1):
    s1 = post("/api/clone_score", {"profile": "刘德华", "audio_base64": a})
    s2 = post("/api/clone_score", {"profile": "刘德华", "audio_base64": a})
    print(f"take{i}: score1={s1.get('cosine')} score2={s2.get('cosine')} nat={s1.get('naturalness')}")
