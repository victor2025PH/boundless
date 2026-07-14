#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 8 漏斗报告 CLI (2026-04-24).

从 lead_journey 表聚合 A 端 add_friend → greeting 漏斗统计. 真机压测时
实时跑, 看:
  * 总量 / 转化率
  * greeting 的 inline vs messenger_fallback 分布
  * greeting_blocked 的 top reason (帮定位瓶颈)
  * per-persona 分布 (哪类客群贡献最多)

用法::

    python scripts/phase8_funnel_report.py                   # 默认近 7 天
    python scripts/phase8_funnel_report.py --days 1          # 近 24 小时
    python scripts/phase8_funnel_report.py --actor agent_a   # 只看 A 端
    python scripts/phase8_funnel_report.py --json            # JSON 给程序消费
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    ap = argparse.ArgumentParser(
        description="A 端 lead_journey 漏斗汇总报告.")
    ap.add_argument("--days", type=int, default=7, help="统计窗口 (天)")
    ap.add_argument("--actor", default="",
                     help="只看某 actor (agent_a / agent_b); 空=不限")
    ap.add_argument("--json", action="store_true",
                     help="JSON 输出 (给程序消费)")
    args = ap.parse_args()

    from src.host.lead_mesh.funnel_report import compute_funnel, format_text_report

    stats = compute_funnel(days=args.days,
                             actor=args.actor or None)

    if args.json:
        print(json.dumps(stats.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_text_report(stats))


if __name__ == "__main__":
    main()
