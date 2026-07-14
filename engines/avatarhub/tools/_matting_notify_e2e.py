# -*- coding: utf-8 -*-
"""批量完成通知 E2E：本地 HTTP 接收器扮演 webhook → 提交 2 个录播任务 →
队列跑空后应收到「录播增强完成 2/2 成功」汇总卡片。收尾还原 webhook 配置。"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
BASE = Path(r"C:\模仿音色")
CLIP = BASE / "logs" / "matting_offline" / "_test_raw.mp4"
HOOK_FILE = BASE / "secrets" / "alert_webhooks.txt"
HOOK_PORT = 9777
ok_all = True
received = []


def check(name, cond, detail=""):
    global ok_all
    print(("[OK] " if cond else "[NG] ") + name + (f"  {detail}" if detail else ""))
    ok_all = ok_all and bool(cond)


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            received.append(json.loads(body.decode("utf-8")))
        except Exception:
            received.append({"raw": body[:200].decode("utf-8", "replace")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", HOOK_PORT), Hook)
threading.Thread(target=srv.serve_forever, daemon=True).start()

st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
if st.get("streaming") or st.get("running"):
    print("SKIP: 推流中或有任务在跑")
    sys.exit(0)
requests.post(f"{HUB}/api/matting/cancel_queue", json={}, timeout=10)

orig = HOOK_FILE.read_text(encoding="utf-8") if HOOK_FILE.exists() else ""
HOOK_FILE.parent.mkdir(exist_ok=True)
HOOK_FILE.write_text(orig.rstrip("\n") + f"\nhttp://127.0.0.1:{HOOK_PORT}/hook\n", encoding="utf-8")
print(f"[setup] 临时 webhook → 127.0.0.1:{HOOK_PORT}")

try:
    names = []
    for i in range(2):
        with open(CLIP, "rb") as f:
            d = requests.post(f"{HUB}/api/matting/upload",
                              files={"file": (f"通知测试{i+1}.mp4", f, "video/mp4")}, timeout=120).json()
        names.append(d["saved"])
    for n in names:
        r = requests.post(f"{HUB}/api/matting/start", json={"input": n, "bg": "green"}, timeout=30).json()
        check(f"提交 {n}", r.get("ok"), "queued" if r.get("queued") else "直跑")

    # 等两个任务全部跑完 + 通知送达
    t0 = time.time()
    while time.time() - t0 < 900:
        st = requests.get(f"{HUB}/api/matting/status", timeout=10).json()
        if not st.get("running") and not st.get("queue") and received:
            break
        time.sleep(3)
    hist = {h.get("input"): h.get("state") for h in st.get("history", [])}
    check("两个任务均完成", all(hist.get(n) == "done" for n in names),
          str({n: hist.get(n) for n in names}))
    check("收到 webhook 通知", bool(received), f"{len(received)}条")
    if received:
        text = json.dumps(received[-1], ensure_ascii=False)
        check("通知含标题", "录播增强完成" in text, "")
        check("通知含成功计数", "2/2" in text, "")
        check("通知含控制台链接", "/ui" in text, "")
        print("  payload 摘要:", (received[-1].get("text") or "")[:180].replace("\n", " ⏎ "))
finally:
    HOOK_FILE.write_text(orig, encoding="utf-8")
    srv.shutdown()
    print("[teardown] webhook 配置已还原")

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
