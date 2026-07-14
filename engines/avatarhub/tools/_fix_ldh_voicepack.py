# -*- coding: utf-8 -*-
"""刘德华参考音修复第二路：声音包长录音重切段优选（apply_if_better）。"""
import json
import time
import urllib.request
import urllib.parse

HUB = "http://127.0.0.1:9000"
NAME = urllib.parse.quote("刘德华")


def post(path, timeout=60):
    req = urllib.request.Request(HUB + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(HUB + path, timeout=15) as r:
        return json.loads(r.read().decode())


r = post(f"/api/optimize_references/voice_pack/{NAME}?max_refs=1", timeout=120)
print("job:", r)
jid = r.get("job_id")
deadline = time.time() + 25 * 60
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
        if res.get("probe"):
            print("PROBE:", json.dumps(res["probe"], ensure_ascii=False)[:400])
        if s.get("error"):
            print("ERROR:", s["error"])
        break
    time.sleep(8)
else:
    print("TIMEOUT waiting job")
