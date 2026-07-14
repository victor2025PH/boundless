# -*- coding: utf-8 -*-
"""用 voice_clones/_shared/andy_ref.wav 作为候选参考跑一次 holdout 优选（只有更好才落库）。"""
import base64
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

HUB = "http://127.0.0.1:9000"
SRC = Path(r"C:\模仿音色\voice_clones\_shared\andy_ref.wav")


def post_json(path, payload, timeout=120):
    req = urllib.request.Request(HUB + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(HUB + path, timeout=15) as r:
        return json.loads(r.read().decode())


b64 = base64.b64encode(SRC.read_bytes()).decode()
print("source bytes:", SRC.stat().st_size)
r = post_json("/api/optimize_references",
              {"profile": "刘德华", "source_b64": b64, "max_refs": 2,
               "apply_if_better": True, "use_speech_corpus": True})
print("job:", r)
jid = r.get("job_id")
deadline = time.time() + 20 * 60
last = ""
while time.time() < deadline:
    s = get(f"/api/optimize_references/status?job_id={jid}")
    prog = s.get("progress") or ""
    if prog != last:
        print("  ...", s.get("status"), prog, flush=True)
        last = prog
    if s.get("status") in ("done", "error"):
        res = s.get("result") or {}
        print("RESULT:", json.dumps({k: v for k, v in res.items() if k not in ("probe", "chosen_refs")},
                                    ensure_ascii=False)[:700])
        if s.get("error"):
            print("ERROR:", s["error"])
        break
    time.sleep(8)
else:
    print("TIMEOUT")
