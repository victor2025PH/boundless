#!/usr/bin/env python3
"""gw_usage_report.py — 租户 AI 用量趋势报表（读本地台账；--fetch 先实采一轮）。

台账由托管履约守护 tick 自动积累（30min 采样粒度，网关只留 3 天故本地攒 60 天）；
本工具只读渲染：主体 × 近 N 天利用率矩阵 + 「建议升级套餐」判据行。

用法：
  python tools/gw_usage_report.py               # 读台账出报表（零网络）
  python tools/gw_usage_report.py --days 30     # 拉长窗口
  python tools/gw_usage_report.py --fetch       # 先向网关实采一轮再出报表
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.ops import tenant_usage_trend as tut  # noqa: E402

SECRETS = Path(r"D:\chengjie-instances\.ops\fulfill")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _fetch_once() -> int:
    site = (SECRETS / "site.txt").read_text(encoding="utf-8").strip().rstrip("/")
    key = (SECRETS / "admin_key.txt").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"{site}/api/admin/gw-budget",
                                 headers={"x-setup-key": key}, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if not resp.get("ok"):
        print(f"[错误] 网关返回异常: {resp}", file=sys.stderr)
        return 1
    now = time.time()
    ledger = tut.load_ledger()
    day = tut.day_key(now)
    n = 0
    for row in resp.get("overrides") or []:
        if tut.upsert_sample(ledger, day, str(row.get("subject") or ""),
                             int(row.get("used_today") or 0),
                             int(row.get("budget") or 0)):
            n += 1
    tut.prune_ledger(ledger, day)
    ledger["last_sample_ts"] = int(now)
    tut.save_ledger(ledger)
    print(f"[fetch] 已采样 {len(resp.get('overrides') or [])} 主体（{n} 条有更新）\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="租户 AI 用量趋势报表")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--fetch", action="store_true", help="先向网关实采一轮（需履约机密）")
    args = ap.parse_args(argv)
    if args.fetch:
        rc = _fetch_once()
        if rc:
            return rc
    ledger = tut.load_ledger()
    print(tut.render_report(ledger, tut.day_key(), days=max(1, args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
