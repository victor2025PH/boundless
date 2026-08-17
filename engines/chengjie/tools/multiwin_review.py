# -*- coding: utf-8 -*-
"""多窗口治理验收周读 CLI（只读）——「还会不会出现两个坐席工作台」用数字回答。

背景：2026-08-11 上线「返回工作台」去重（_win_unique.html::__wsGoHome，P1-③/P2），
修复生效的硬判据＝坐席端多窗遥测归零：
  mw.takeover_auto    前台开出重复坐席、直接接管（重复窗口诞生的直接证据）
  mw.takeover_reclaim 坐席在两个窗口间来回切（重复窗口存续的证据）
  mw.boot_standby     后台恢复出的重复窗口静默待机（会话恢复类，预期恒 ≈0）
  mw.promoted         主控崩溃被待机窗接管（正常自愈，非重复窗口信号）

数据源＝ui_event_trend.db（beacon 按日落库，重启不清零；zhiliao 已开
ops.ui_event_trend）。本工具只读（sqlite ro URI，绝不建库）；多实例机按
scripts/_data_root 契约逐根审读。

用法::

    python tools/multiwin_review.py                 # 全部活跃实例，近 14 天
    python tools/multiwin_review.py --days 30
    python tools/multiwin_review.py --data-root D:/chengjie-instances/zhiliao/data
    python tools/multiwin_review.py --json          # 机器可读（进脚本/周批）

判词口径：修复日（--fix-date，默认 2026-08-11）当天新旧行为混杂不计入；
修复日之后 takeover_auto+reclaim 归零＝坐实，仍有量＝存在未收口入口
（Ctrl+点击新开 / 跨 profile / 壳+浏览器并存属声明边界，见 AGENTS.md 多开治理④）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

DEFAULT_FIX_DAY = "2026-08-11"
MW_ACTIONS = ("mw.takeover_auto", "mw.takeover_reclaim", "mw.boot_standby", "mw.promoted")
# 「重复坐席窗口」的直接信号（判词只看这两项；boot_standby/promoted 是语境不是病）
DUP_ACTIONS = ("mw.takeover_auto", "mw.takeover_reclaim")


def summarize_mw(rows: Sequence[Tuple[str, str, int]], fix_day: str) -> Dict[str, Any]:
    """按修复日切段聚合 + 出判词。纯函数（门禁 tests/test_multiwin_review.py）。

    rows: (day 'YYYY-MM-DD', action, n)；未知 action 忽略；day==fix_day 不入两段
    （当天新旧行为混杂，既不给修复报喜也不给旧账定罪）。
    """
    days: Dict[str, Dict[str, int]] = {}
    pre: Dict[str, int] = {a: 0 for a in MW_ACTIONS}
    post: Dict[str, int] = {a: 0 for a in MW_ACTIONS}
    post_days: set = set()
    pre_days: set = set()
    for day, action, n in rows:
        a = str(action)
        if a not in MW_ACTIONS:
            continue
        d = str(day)
        cnt = int(n or 0)
        days.setdefault(d, {})[a] = days.setdefault(d, {}).get(a, 0) + cnt
        if d > fix_day:
            post[a] += cnt
            post_days.add(d)
        elif d < fix_day:
            pre[a] += cnt
            pre_days.add(d)
    dup_post = sum(post[a] for a in DUP_ACTIONS)
    dup_pre = sum(pre[a] for a in DUP_ACTIONS)
    verdicts: List[str] = []
    if not post_days:
        verdicts.append(
            f"修复日（{fix_day}）当天数据混杂不计；从次日起累计判读——明天再跑即有修复后段位")
    elif dup_post == 0:
        verdicts.append(
            f"[OK] 修复后 {len(post_days)} 天零重复坐席事件"
            f"（takeover_auto+reclaim=0；修复前同口径共 {dup_pre}）——修复坐实")
    else:
        verdicts.append(
            f"[WARN] 修复后 {len(post_days)} 天仍有 takeover_auto={post['mw.takeover_auto']}"
            f" reclaim={post['mw.takeover_reclaim']}——存在未收口入口"
            "（Ctrl+点击新开/跨 profile/壳+浏览器并存属声明边界；持续则按天定位）")
    if post.get("mw.boot_standby", 0) > 0:
        verdicts.append(
            f"[INFO] 修复后 boot_standby={post['mw.boot_standby']}"
            "（浏览器会话恢复出的后台重复窗，协调器已静默待机，非本次修复范畴）")
    return {
        "fix_day": fix_day,
        "days": [
            {"day": d, **{a: days[d].get(a, 0) for a in MW_ACTIONS}}
            for d in sorted(days)
        ],
        "pre": pre, "post": post,
        "pre_days": len(pre_days), "post_days": len(post_days),
        "dup_pre": dup_pre, "dup_post": dup_post,
        "verdicts": verdicts,
    }


def collect_mw(db_path: Path, days: int) -> Optional[List[Tuple[str, str, int]]]:
    """读 ui_event_trend.db 近 N 天 mw.* 行；库不存在/坏 → None（绝不建库）。"""
    p = Path(db_path)
    if not p.is_file():
        return None
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - max(1, days) * 86400))
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return [
                (str(r[0]), str(r[1]), int(r[2] or 0))
                for r in con.execute(
                    "SELECT day, action, n FROM ui_event_trend_daily "
                    "WHERE day >= ? AND action LIKE 'mw.%' ORDER BY day, action",
                    (cutoff,),
                ).fetchall()
            ]
        finally:
            con.close()
    except Exception:
        return None


def _render(root: Path, summary: Optional[Dict[str, Any]]) -> None:
    print(f"\n=== {root} ===")
    if summary is None:
        print("  （无 ui_event_trend.db —— 该实例未开 ops.ui_event_trend，跳过）")
        return
    if not summary["days"]:
        print("  近窗口内无 mw.* 事件（既无重复窗口也无接管——本身就是好消息）")
    else:
        head = f"  {'day':<12}" + "".join(f"{a.split('.')[1]:>16}" for a in MW_ACTIONS)
        print(head)
        for row in summary["days"]:
            mark = " <- fix" if row["day"] == summary["fix_day"] else ""
            print(f"  {row['day']:<12}"
                  + "".join(f"{row[a]:>16}" for a in MW_ACTIONS) + mark)
    for v in summary["verdicts"]:
        print("  " + v)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="多窗口治理验收周读（只读）")
    ap.add_argument("--data-root", default="", help="指定实例数据根（默认自动发现全部活跃实例）")
    ap.add_argument("--days", type=int, default=14, help="回看天数（默认 14）")
    ap.add_argument("--fix-date", default=DEFAULT_FIX_DAY,
                    help=f"修复上线日（默认 {DEFAULT_FIX_DAY}；当天不入判词两段）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args(argv)

    out: Dict[str, Any] = {}
    for root in resolve_data_roots(args.data_root):
        rows = collect_mw(Path(root) / "config" / "ui_event_trend.db", args.days)
        summary = None if rows is None else summarize_mw(rows, args.fix_date)
        out[str(root)] = summary
        if not args.json:
            _render(Path(root), summary)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
