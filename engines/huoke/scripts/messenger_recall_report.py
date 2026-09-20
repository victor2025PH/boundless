#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Messenger 召回体检报告 CLI (P3 2026-08-15).

聚合 messenger_recall_runs 表, 验证 P0/P1/P2 的读取召回启发式是否真的有效:
  * 未读判定召回够不够 (救回占比)
  * 通知/预览/搜索各救回多少
  * 搜索误点率 (决定 max_search_opens 该不该收紧)
  * 身份错乱频度 (决定持久身份层该不该做)
  * 空读率 / 屏外漏读候选

用法::

    python scripts/messenger_recall_report.py                # 默认近 7 天
    python scripts/messenger_recall_report.py --days 1       # 近 24 小时
    python scripts/messenger_recall_report.py --device Q4N7  # 只看某设备
    python scripts/messenger_recall_report.py --json         # JSON 给程序消费
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
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    ap = argparse.ArgumentParser(description="Messenger 读取召回体检报告.")
    ap.add_argument("--days", type=int, default=7, help="统计窗口 (天)")
    ap.add_argument("--device", default="", help="只看某设备; 空=全部")
    ap.add_argument("--json", action="store_true", help="JSON 输出 (含判词)")
    args = ap.parse_args()

    from src.host.database import init_db
    from src.host.fb_store import messenger_recall_summary
    from src.host.messenger_recall_report import (format_text_report,
                                                  recall_verdicts)

    try:
        init_db()
    except Exception:
        pass

    summary = messenger_recall_summary(days=args.days,
                                       device_id=args.device or None)
    if args.json:
        out = dict(summary)
        out["verdicts"] = recall_verdicts(summary)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(format_text_report(summary))


if __name__ == "__main__":
    main()
