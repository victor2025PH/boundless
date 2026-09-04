# -*- coding: utf-8 -*-
"""汇总回访记账 CLI（I-6 F1③，2026-09-04）。

场景：一个报障人积压 N 单 ``fixed`` 未回访（notify_ts=0），逐单推 N 条是轰炸
（对方直接静音 bot＝彻底失去反馈渠道）。改为群里发**一条**按人合并的汇总回访
（``duty_reply.py --group neice --ticket <锚单> --text-file ...``），再用本工具把该
报障人全部积压单一次性 ``mark_notified``——notify_ts>0 之后 ``verified`` 自动
闭环链（bug_intake.py 顶部 P3）与 ops 的 pending_notify KPI 才能正常工作。

    python tools/duty_notify_summary.py --reporter 8942577244                  # dry-run：只打印将写哪些行
    python tools/duty_notify_summary.py --reporter 8852939166 --reporter 7997166885 \\
        --msg-id 1234 --apply                                                  # 真写库

默认 dry-run，``--apply`` 才写库。``--reporter`` 可多次给（同一人两个 TG 号）。
``--ids`` 可再收窄到显式工单号（宁窄勿宽：每一行都是「已告知」的承诺）。
记账 note 形如 ``summary:20260904 mid=1234``，与逐单回访的 ``bot`` 口径区分，事后可查。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


def select_rows(rows: List[Dict], reporters: List[str], ids: List[int]) -> List[Dict]:
    """纯函数：按 reporter_id 白名单 + 可选工单号白名单收窄积压行。

    reporters 为空＝不按人过滤（配合 --ids 用）；两者都空＝返回空（拒绝「全冲」
    ——汇总回访是按人发的，记账也必须按人来，不给误把别人的单一起盖掉的机会）。
    """
    reps = {str(r).strip() for r in reporters if str(r).strip()}
    idset = {int(i) for i in ids}
    if not reps and not idset:
        return []
    out = []
    for r in rows:
        if reps and str(r.get("reporter_id") or "") not in reps:
            continue
        if idset and int(r.get("id") or 0) not in idset:
            continue
        out.append(r)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="汇总回访记账（默认 dry-run）")
    ap.add_argument("--reporter", action="append", default=[],
                    help="报障人 reporter_id（可多次）")
    ap.add_argument("--ids", default="", help="逗号分隔工单号，进一步收窄")
    ap.add_argument("--msg-id", type=int, default=0,
                    help="汇总消息的 TG message id（bot 链可得；0=不记锚）")
    ap.add_argument("--note", default="",
                    help="记账备注，缺省 summary:<日期> mid=<msg-id>")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--apply", action="store_true", help="真写库（缺省只打印）")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    os.environ.setdefault("AITR_DATA_DIR", str(data_root))
    from src.ops.bug_intake import list_pending_notify, mark_notified

    ids = [int(x) for x in str(args.ids).split(",") if x.strip().isdigit()]
    rows = select_rows(list_pending_notify(limit=100), args.reporter, ids)
    if not rows:
        _out("[ok] 该筛选下无积压（或未给 --reporter/--ids）")
        return 0
    note = args.note or (f"summary:{time.strftime('%Y%m%d')} mid={args.msg_id}")
    _out(f"[plan] 数据根 {data_root}")
    _out(f"[plan] 将对 {len(rows)} 单执行 mark_notified(ok=True, note={note!r}, msg_id={args.msg_id})")
    _out("       → UPDATE bug_tickets SET notify_ts=now, notify_note=note"
         + (", notify_msg_id=msg_id" if args.msg_id > 0 else "")
         + ", updated_ts=now WHERE id=?")
    for r in rows:
        _out(f"  #{r.get('id')} [{r.get('severity')}] {r.get('reporter_name')} "
             f"| {str(r.get('title') or '').splitlines()[0][:56]}")
    if not args.apply:
        _out("[dry-run] 未写库。确认后加 --apply。")
        return 0
    done = 0
    for r in rows:
        mark_notified(int(r["id"]), True, note, msg_id=args.msg_id)
        done += 1
    left = select_rows(list_pending_notify(limit=100), args.reporter, ids)
    _out(f"[apply] 已记账 {done} 单；该筛选下剩余积压 {len(left)} 单")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
