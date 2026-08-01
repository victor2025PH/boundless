# -*- coding: utf-8 -*-
"""挽回效果周报 CLI（RH-P2）——「发现→话术→发送→回来了」的读数出口。

用法（引擎根运行，只读，随时可跑）::

    python -m scripts.winback_report                 # 人读格式
    python -m scripts.winback_report --json          # 机器格式
    python -m scripts.winback_report --out-jsonl logs\\eval\\winback_trend.jsonl

输出：近 30 天挽回漏斗（发送/挽回/成熟/待观察/挽回率）+ 本周 vs 上周环比 +
最近 10 条发送逐条状态。统计口径与页面 KPI 单源（src/contacts/winback_stats.py）。

数据根按 scripts/_data_root 契约解析；contacts.db 以 **mode=ro** 打开（活体
生产库零写事务）；库不存在＝SKIP exit 0（服务没开闸不算红）。
周批注册（读数型任务，与 TranslationEvalWeekly/ProactiveReviewWeekly 同族）::

    schtasks /Create /TN WinbackReviewWeekly /SC WEEKLY /D SAT /ST 07:20 /F ^
      /TR "powershell -ExecutionPolicy Bypass -File D:\\boundless\\engines\\chengjie\\scripts\\winback_report_weekly.ps1"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import ENGINE_ROOT, resolve_data_roots  # noqa: E402
from src.contacts.winback_stats import (  # noqa: E402
    compute_winback,
    readonly_conn,
    recent_reactivations,
)


def collect(root: Path, *, days: int, window: int, now: float) -> dict:
    db = Path(root) / "config" / "contacts.db"
    if not db.is_file():
        return {"root": str(root), "skip": "no_contacts_db"}
    con = readonly_conn(db)
    try:
        overall = compute_winback(con, days=days, reply_window_days=window, now=now)
        week = compute_winback(con, days=7, reply_window_days=window, now=now)
        prev_week = compute_winback(
            con, days=7, reply_window_days=window, until_offset_days=7, now=now)
        recent = recent_reactivations(con, limit=10, reply_window_days=window, now=now)
    finally:
        con.close()
    return {
        "root": str(root),
        "ts": now,
        "date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        "overall": overall,
        "week": week,
        "prev_week": prev_week,
        "recent": recent,
    }


def _fmt_rate(d: dict) -> str:
    return f"{round(d['rate'] * 100)}%" if d.get("rate") is not None else "—"


def _print_human(r: dict) -> None:
    print(f"\n=== 挽回效果周报 {r['date']} · {r['root']} ===")
    o = r["overall"]
    print(f"  近 {o['days']} 天: 发送 {o['sent']} · 挽回 {o['replied']} · "
          f"成熟 {o['matured']} · 待观察 {o['pending']} · 挽回率 {_fmt_rate(o)}")
    w, p = r["week"], r["prev_week"]
    print(f"  本周: 发送 {w['sent']} 挽回 {w['replied']} ({_fmt_rate(w)})  |  "
          f"上周: 发送 {p['sent']} 挽回 {p['replied']} ({_fmt_rate(p)})")
    if r["recent"]:
        print("  最近发送:")
        for it in r["recent"]:
            state = "已挽回" if it["replied"] else ("待观察" if it["pending"] else "未回")
            when = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
            print(f"    {when}  {it['name'] or it['journey_id'][:12]}  [{state}]")
    else:
        print("  尚无挽回发送记录（坐席在预警榜点「标记已发/直接发送」后开始积累）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--window", type=int, default=7, help="回复观察窗（天）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="", help="趋势行追加落盘（UTF-8）")
    args = ap.parse_args()

    now = time.time()
    results = []
    for root in resolve_data_roots(args.data_root):
        root = Path(root)
        if root == ENGINE_ROOT and not args.data_root:
            continue  # 引擎仓库根不是实例数据根
        results.append(collect(root, days=args.days, window=args.window, now=now))

    if not results:
        print("SKIP: 未发现实例数据根（或均无 contacts.db）")
        return 0

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r.get("skip"):
                print(f"SKIP {r['root']}: {r['skip']}")
            else:
                _print_human(r)

    if args.out_jsonl:
        out = Path(args.out_jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            for r in results:
                if r.get("skip"):
                    continue
                line = {
                    "ts": r["ts"], "date": r["date"], "root": r["root"],
                    "days": r["overall"]["days"],
                    "window": r["overall"]["reply_window_days"],
                    "sent": r["overall"]["sent"],
                    "replied": r["overall"]["replied"],
                    "matured": r["overall"]["matured"],
                    "pending": r["overall"]["pending"],
                    "rate": r["overall"]["rate"],
                    "week_sent": r["week"]["sent"],
                    "week_replied": r["week"]["replied"],
                    "prev_week_sent": r["prev_week"]["sent"],
                    "prev_week_replied": r["prev_week"]["replied"],
                }
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
