# -*- coding: utf-8 -*-
"""P14 取证②：在播态 + 当前 hub 身份 + 02:04 死亡现场（hub_console 尾部）。"""
import urllib.request, json, sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

print("== 1) 在播态 /realtime/status ==")
try:
    d = json.load(urllib.request.urlopen("http://127.0.0.1:9000/realtime/status", timeout=10))
    keep = {k: d.get(k) for k in ("running", "streaming", "orphan", "rvc_conv", "health", "mode") if k in d}
    print(json.dumps(keep, ensure_ascii=False))
except Exception as e:
    print("  失败:", e)

print("\n== 2) 当前 hub 进程 ==")
import psutil
for p in psutil.process_iter(["pid", "cmdline", "memory_info", "create_time"]):
    try:
        cl = " ".join(p.info["cmdline"] or [])
        if "avatar_hub.py" in cl:
            print("  pid=%s rss=%.2fG 启动于 %s" % (
                p.info["pid"], p.info["memory_info"].rss / 1e9,
                time.strftime("%H:%M:%S", time.localtime(p.info["create_time"]))))
    except Exception:
        pass

print("\n== 3) boots 账页新尾 ==")
with open(r"logs\hub_boots.jsonl", encoding="utf-8") as f:
    for ln in f.readlines()[-4:]:
        d = json.loads(ln)
        print("  ", time.strftime("%m-%d %H:%M:%S", time.localtime(d["ts"])), "pid=", d["pid"], d["parent"][:90])

print("\n== 4) hub_console.log 在 02:03:30~02:05:00 的行（死亡现场）==")
import io
with open(r"logs\hub_console.log", "rb") as f:
    f.seek(max(0, f.seek(0, 2) - 3_000_000))
    f.readline()
    raw = f.read()
txt = raw.decode("utf-8", errors="replace")
hits = 0
for ln in txt.splitlines():
    if any(k in ln for k in ("02:03:5", "02:04:0", "02:04:1", "02:04:2", "02:04:3")):
        print("  ", ln.strip()[:200]); hits += 1
        if hits > 40:
            break
if not hits:
    # 没有直接命中就打最后 25 行看结尾停在哪
    print("  （02:04 窗口无行——打日志结尾 25 行）")
    for ln in txt.splitlines()[-25:]:
        print("  ", ln.strip()[:200])
