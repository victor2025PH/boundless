# -*- coding: utf-8 -*-
"""P14 终验取证：稳定性账本全量 + hub RSS + 启动账页 + 守护软警。一跑一收。"""
import urllib.request, json, sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

print("== 1) 稳定性账本（天窗=2, refresh=1 现算+哨兵）==")
d = json.load(urllib.request.urlopen("http://127.0.0.1:9000/api/ops/stability?days=2&refresh=1", timeout=180))
print("ok=", d.get("ok"), " sentinel=", d.get("sentinel"))
print("summary:", json.dumps(d.get("summary") or {}, ensure_ascii=False))
print("baseline:", json.dumps(d.get("baseline") or {}, ensure_ascii=False))
wr = d.get("watchdog_restarts") or []
print("守护拉活 %d 条:" % len(wr))
for r in wr[-10:]:
    ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts", 0)))
    print("  ", ts, json.dumps({k: v for k, v in r.items() if k != "ts"}, ensure_ascii=False)[:200])
cr = d.get("crashes") or []
print("崩溃事件 %d 条:" % len(cr))
for r in cr[-10:]:
    ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts", 0)))
    print("  ", ts, json.dumps({k: v for k, v in r.items() if k != "ts"}, ensure_ascii=False)[:200])
bt = d.get("boots") or []
print("启动记录 %d 条(尾5):" % len(bt))
for r in bt[-5:]:
    ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts", 0)))
    print("  ", ts, json.dumps({k: v for k, v in r.items() if k != "ts"}, ensure_ascii=False)[:180])

print("\n== 2) hub 当前 RSS ==")
try:
    import psutil
    for p in psutil.process_iter(["pid", "cmdline", "memory_info", "create_time"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
            if "avatar_hub.py" in cl:
                up_h = (time.time() - p.info["create_time"]) / 3600
                print("  pid=%s rss=%.2fG uptime=%.1fh" % (p.info["pid"], p.info["memory_info"].rss / 1e9, up_h))
        except Exception:
            pass
except Exception as e:
    print("  psutil 不可用:", e)
