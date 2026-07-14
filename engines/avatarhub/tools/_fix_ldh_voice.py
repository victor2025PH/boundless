# -*- coding: utf-8 -*-
"""刘德华声纹回归自动修复：走声音包预切段 holdout 优选（apply_if_better=只有更好才落库），
轮询任务直到完成并打印结论。"""
import json
import time
import urllib.request
import urllib.parse

HUB = "http://127.0.0.1:9000"
NAME = urllib.parse.quote("刘德华")


def post(path):
    req = urllib.request.Request(HUB + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(HUB + path, timeout=15) as r:
        return json.loads(r.read().decode())


r = post(f"/api/optimize_references/segments/{NAME}?max_refs=1")
print("job:", r)
jid = r.get("job_id")
deadline = time.time() + 15 * 60
last = ""
while time.time() < deadline:
    s = get(f"/api/optimize_references/status?job_id={jid}")
    prog = s.get("progress") or ""
    if prog != last:
        print("  ...", s.get("status"), prog)
        last = prog
    if s.get("status") in ("done", "error"):
        res = s.get("result") or {}
        res.pop("candidates", None)
        print("RESULT:", json.dumps({k: v for k, v in res.items() if k != "probe"},
                                    ensure_ascii=False)[:600])
        if res.get("probe"):
            print("PROBE:", json.dumps(res["probe"], ensure_ascii=False)[:400])
        if s.get("error"):
            print("ERROR:", s["error"])
        break
    time.sleep(6)
else:
    print("TIMEOUT waiting job")
