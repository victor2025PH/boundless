# -*- coding: utf-8 -*-
"""/order 页演示视频观看漏斗:从服务器拉 events.jsonl,按 key/lang 汇总
play → 25/50/75 → done,算完播率。用法:
  python demo_record/showcase_stats.py [--days 7]
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = r"%USERPROFILE%\.ssh\hualing_deploy"
HOST = "ubuntu@165.154.233.121"
REMOTE = "/home/ubuntu/hualing-analytics/events.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    import os
    key = os.path.expandvars(KEY)
    r = subprocess.run(["ssh", "-i", key, HOST, f"cat {REMOTE}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("拉取失败:", r.stderr[:200]); return

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    # funnel[key][lang] = {play, p25, p50, p75, done}; 会话去重按 (sid,key)
    seen = set()
    funnel: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for ln in r.stdout.splitlines():
        try:
            e = json.loads(ln)
        except Exception:
            continue
        ev = e.get("event", "")
        if not ev.startswith("showcase_"):
            continue
        p = e.get("props") or {}
        k, lang = p.get("key", "?"), p.get("lang", "?")
        if k == "_smoke":
            continue
        try:
            if datetime.fromisoformat(e["t"].replace("Z", "+00:00")) < since:
                continue
        except Exception:
            pass
        sid = e.get("sid", "")
        if ev == "showcase_play":
            dk = (sid, k, "play")
            if dk in seen:
                continue
            seen.add(dk)
            funnel[k][lang]["play"] += 1
        elif ev == "showcase_progress":
            funnel[k][lang][f"p{p.get('pct')}"] += 1
        elif ev == "showcase_done":
            funnel[k][lang]["done"] += 1

    if not funnel:
        print(f"近 {args.days} 天暂无 showcase 播放事件(埋点 {datetime.now():%m-%d} 刚上线,等自然流量)")
        return
    print(f"近 {args.days} 天 /order 演示视频漏斗(会话去重):")
    print(f"{'视频':12}{'语言':6}{'播放':6}{'25%':6}{'50%':6}{'75%':6}{'完播':6}{'完播率':8}")
    for k in sorted(funnel):
        for lang in sorted(funnel[k]):
            d = funnel[k][lang]
            play = d.get("play", 0)
            rate = f"{d.get('done', 0) / play * 100:.0f}%" if play else "-"
            print(f"{k:12}{lang:6}{play:<6}{d.get('p25', 0):<6}{d.get('p50', 0):<6}"
                  f"{d.get('p75', 0):<6}{d.get('done', 0):<6}{rate:8}")


if __name__ == "__main__":
    main()
