# -*- coding: utf-8 -*-
"""一次性：用 Cursor 直播监控的 TICK 采样重建 2026-07-06 13:46–14:12 场次的在播断点。
背景：停播时 hub 被整树关闭，内存半场聚合蒸发、账本缺档(P7 三期的暴露现场)。
本脚本把监控 5s 采样(313 点)换算回 hub 4s 口径写成 data/swap_sess_active.json，
随后重启 hub，由新增的 _swap_sess_reconcile() 走真代码路径转正入账(recovered 标记)。
"""
import json, re, time, sys, os

TERM = r"C:\Users\user\.cursor\projects\c\terminals\219317.txt"
OUT  = r"c:\模仿音色\data\swap_sess_active.json"
DAY  = "2026-07-06"
T0, T1 = "13:46:39", "14:12:53"          # SESSION_START → 首个 idle tick
WATCH_SEC = 4.0                           # hub 健康守护采样周期

def ep(hms):
    return time.mktime(time.strptime(f"{DAY} {hms}", "%Y-%m-%d %H:%M:%S"))

pat = re.compile(
    r"^TICK (\d\d:\d\d:\d\d) hub=(\w+)/\w+ fps=([\d.]+) lat=(\d+)ms eff=(\w+) tgt=(\w+) "
    r"crop=(\d+)/(\d+) ok=(\d+) fail=(\d+) enh=(\w+) sfps=\d+")

rows = []
with open(TERM, encoding="utf-8", errors="replace") as f:
    for ln in f:
        m = pat.match(ln.strip())
        if not m:
            continue
        t = m.group(1)
        if T0 <= t <= T1:
            rows.append(dict(t=t, state=m.group(2), fps=float(m.group(3)), lat=float(m.group(4)),
                             eff=m.group(5), tgt=m.group(6), hits=int(m.group(7)), miss=int(m.group(8)),
                             ok=int(m.group(9)), fail=int(m.group(10)), enh=m.group(11)))

if len(rows) < 10:
    print(f"FATAL: 只解析到 {len(rows)} 个采样点"); sys.exit(1)

start, end = ep(T0), ep(T1)
dur = end - start
n_hub = int(round(dur / WATCH_SEC))              # hub 口径应有样本数(4s/tick)
scale = n_hub / float(len(rows))

lat  = [r["lat"] for r in rows if r["lat"] > 0]
fps  = [r["fps"] for r in rows if r["fps"] > 0]
last = rows[-1]

def hist(key):
    h = {}
    for r in rows:
        h[r[key]] = h.get(r[key], 0) + 1
    return {k: max(1, int(round(v * scale))) for k, v in h.items()}

states  = hist("state")
presets = hist("eff")
enh_map = hist("enh")
enhance = {("无" if k == "none" else k): v for k, v in enh_map.items()}
deg = sum(1 for r in rows if r["eff"] != r["tgt"])
degraded_ticks = max(deg, int(round(deg * scale)))

agg = {
    "active": True, "start": round(start, 1),
    "samples": n_hub, "miss_samples": 0,
    "fps": fps, "lat": lat, "lat_max": max(lat) if lat else 0.0,
    "cum": {"ok":   {"carry": 0, "last": last["ok"]},
            "fail": {"carry": 0, "last": last["fail"]},
            "hits": {"carry": 0, "last": last["hits"]},
            "miss": {"carry": 0, "last": last["miss"]}},
    "presets": presets, "enhance": enhance,
    "bg": {"none": n_hub},
    "degraded_ticks": degraded_ticks,
    "reasons": {"时延779ms超标→hd降beauty再降natural(双脸全帧过载),40s后自动爬回hd(监控重建)": degraded_ticks},
    "states": states,
    "note": "cursor监控重建(宿主停播时被整树关闭,断点机制上线前的补录)",
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(agg, f, ensure_ascii=False)
print(f"ticks={len(rows)} → hub样本={n_hub} dur={int(dur)}s")
print(f"lat n={len(lat)} med≈{sorted(lat)[len(lat)//2]:.0f} max={max(lat):.0f}")
print(f"ok={last['ok']} fail={last['fail']} crop={last['hits']}/{last['miss']}")
print(f"states={states} presets={presets} enhance={enhance} degraded={degraded_ticks}")
print("checkpoint written:", OUT)
