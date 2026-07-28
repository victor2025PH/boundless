# -*- coding: utf-8 -*-
"""对练趋势周报：把两条趋势线读成人能看的表。

两条线各回答一个不同的问题——分开看才有意义：
- ``logs/duel/duel_trend.jsonl``（夜跑写）：**产品**在变好还是变差。
  归一到 ``defects_per_100_turns``，因为夜跑轮数会调，绝对缺陷数不可比。
- ``logs/eval/duel_semantic_trend.jsonl``（周批写）：**裁判**还准不准。
  金标是固定的，所以这条线一掉就是模型/提示词漂移，而不是产品变差。

先前踩过的坑正是「只有覆盖式的 latest_summary.json，没有历史」——出了变化无从
判断是偶发还是趋势。

用法::

    python -m scripts.duel_trend_report [--days 14] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
NIGHTLY_TREND = _ROOT / "logs" / "duel" / "duel_trend.jsonl"
SEMANTIC_TREND = _ROOT / "logs" / "eval" / "duel_semantic_trend.jsonl"

# 单一读盘实现（API / CLI 同口径）
from src.utils.duel_bench_status import (  # noqa: E402
    read_trend, trend_direction,
)


def render_nightly(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "（夜跑趋势为空——DuelNightly 还没跑过，或 --trend-out 没接）"
    lines = ["日期              场次  轮次  缺陷  每百轮  超标场景"]
    for r in rows[-14:]:
        ts = str(r.get("ts") or "")[:16].replace("T", " ")
        over = r.get("over_budget_scenarios") or []
        lines.append(
            f"{ts:17} {r.get('scenarios', 0):>4} {r.get('turns', 0):>5}"
            f" {r.get('defects', 0):>5} {r.get('defects_per_100_turns', 0):>7}"
            f"  {', '.join(over) if over else '-'}")
    kinds: Dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("by_kind") or {}).items():
            kinds[k] = kinds.get(k, 0) + int(v or 0)
    if kinds:
        lines.append("累计按类型：" + "  ".join(
            f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
    first, last = rows[0], rows[-1]
    d0 = float(first.get("defects_per_100_turns") or 0)
    d1 = float(last.get("defects_per_100_turns") or 0)
    if len(rows) >= 2:
        _map = {"better": "变好", "worse": "变差", "flat": "持平"}
        trend = _map.get(trend_direction(rows), "未知")
        lines.append(f"结论：每百轮缺陷 {d0} → {d1}（{trend}）")
    return "\n".join(lines)


def render_semantic(rows: List[Dict[str, Any]]) -> str:
    llm = [r for r in rows if r.get("mode") == "llm"]
    if not llm:
        return "（语义评测实跑趋势为空——周批还没跑，或没配云端 key）"
    lines = ["日期              syco  fact  ill   误报  结论"]
    for r in llm[-10:]:
        ts = str(r.get("ts") or "")[:16].replace("T", " ")

        def pct(k: str) -> str:
            v = r.get(f"{k}_recall")
            return f"{float(v):.0%}" if isinstance(v, (int, float)) else "  -"

        lines.append(
            f"{ts:17} {pct('sycophancy'):>5} {pct('persona_fact'):>5}"
            f" {pct('ill_timed'):>5} {str(r.get('false_alarms', '-')):>5}"
            f"  {'PASS' if r.get('passed') else 'FAIL'}")
    bad = [r for r in llm if not r.get("passed")]
    lines.append(f"结论：{len(llm)} 次实跑，{len(bad)} 次 FAIL"
                 + ("（裁判可能漂移，去看 duel_semantic_weekly.log）" if bad else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="对练趋势周报")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天（0=全部）")
    ap.add_argument("--nightly", default=str(NIGHTLY_TREND))
    ap.add_argument("--semantic", default=str(SEMANTIC_TREND))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    n = read_trend(Path(a.nightly), days=a.days or None)
    s = read_trend(Path(a.semantic), days=a.days or None)
    if a.json:
        print(json.dumps({"nightly": n, "semantic": s},
                         ensure_ascii=False, indent=2))
        return 0
    print("=== 对练趋势（产品侧：夜跑真实对话）===")
    print(render_nightly(n))
    print()
    print("=== 裁判趋势（金标固定，掉了就是漂移）===")
    print(render_semantic(s))
    if not n and not s:
        print("\n两条线都空——先让 DuelNightly / DuelSemanticWeekly 各跑一次。",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
