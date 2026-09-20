# -*- coding: utf-8 -*-
"""值守一轮巡检（0903 起常驻监控用）——一条命令看完四件事，省得每轮拼四段脚本。

1. 群消息（含 mid 连续性：跳号即入站漏收，见 SOP C1）；
2. bug_events 全 kind 扫（含 rate_capped_report/usage 静默类，见 SOP §J）；
3. 工单表新增/状态；
4. 在途证据台账（duty_evidence list 同源）。

只读，不发消息。用法：python tools/duty_watch_once.py [--since-min 60]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

DATA = Path(r"D:\chengjie-instances\zhiliao\data")
GROUP = "-1004345824259"


def _p(s: str = "") -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-min", type=int, default=60)
    a = ap.parse_args()
    cut = (dt.datetime.now() - dt.timedelta(minutes=a.since_min)).timestamp()
    hhmm = lambda t: dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")  # noqa: E731

    _p(f"== 群消息（近 {a.since_min} 分钟）==")
    inbox = sqlite3.connect(str(DATA / "config" / "inbox.db"))
    rows = inbox.execute(
        "select direction, ts, platform_msg_id, substr(text,1,110), media_ref"
        " from messages where conversation_id like ? and ts>=? order by ts",
        (f"%{GROUP}%", cut)).fetchall()
    prev = None
    for d, ts, mid, txt, mref in rows:
        gap = ""
        try:
            if prev is not None and int(mid) - prev > 1:
                gap = f"   <<< 跳号 {prev}->{mid}（入站漏收？）"
            prev = int(mid)
        except (TypeError, ValueError):
            pass
        _p(f"{d:3} {hhmm(ts)} mid={mid} | "
           f"{str(txt or '').replace(chr(10), ' / ')}{'  [media]' if mref else ''}{gap}")
    _p(f"（{len(rows)} 条）")

    bi = sqlite3.connect(str(DATA / "config" / "bug_intake.db"))
    _p("")
    _p("== bug_events（全 kind，含静默类）==")
    for r in bi.execute(
            "select id,ts,kind,reporter_id,substr(detail,1,90) from bug_events"
            " where ts>=? order by id", (cut,)).fetchall():
        _p(f"{r[0]} {hhmm(r[1])} {r[2]:<22} {r[3]} | "
           f"{str(r[4] or '').replace(chr(10), ' / ')}")

    _p("")
    _p("== 工单（近窗新建 / 全部未闭环）==")
    for r in bi.execute(
            "select id,status,severity,reporter_version,substr(title,1,60),created_ts"
            " from bug_tickets where created_ts>=? or status in"
            " ('new','confirmed','in_progress') order by id", (cut,)).fetchall():
        flag = "NEW " if r[5] >= cut else "    "
        _p(f"{flag}#{r[0]} [{r[1]}/{r[2]}] ver={r[3] or '?':<7} "
           f"{str(r[4] or '').replace(chr(10), ' ')}")

    _p("")
    _p("== 在途证据 ==")
    led = DATA / "logs" / "duty_evidence.jsonl"
    state = {}
    if led.is_file():
        for line in led.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = int(e.get("ticket") or 0)
            if e.get("event") == "request":
                state[t] = e
            elif e.get("event") == "received":
                state.pop(t, None)
    for t, e in sorted(state.items()):
        age = (dt.datetime.now().timestamp() - float(e.get("ts") or 0)) / 60
        _p(f"#{t} 等 {age:.0f} 分钟：{str(e.get('what') or '')[:90]}")
    if not state:
        _p("（无）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
