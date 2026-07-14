#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日数据快照 — 拉 dashboard 关键指标落 CSV.

用法:
    python scripts/daily_snapshot.py                         # 默认 ./reports/daily_YYYYMMDD.csv
    python scripts/daily_snapshot.py --base http://...
    python scripts/daily_snapshot.py --output reports/daily.csv

为什么不用 dashboard:
- dashboard 是实时看板, 数据不留底
- 30 phones 真实跑起来后, 1-2 周后回顾要看趋势 → 需要每日快照
- 凌晨 3 点 cron 跑一次, 1 周后 7 行 CSV 就能拼出基线漏斗趋势

输出列 (24 个指标):
- date
- 客户分桶 (in_funnel/messenger/line/accepted/converted/lost) × 6
- 漏斗事件 (friend_request_sent/greeting_sent/message_received/wa_referral_sent/converted/lost) × 6
- referral_decision: total/by_level × 4 + refer_rate
- handoff: pending_n + breach_n (>30 min)
- agent_sla: 累计接管 / 转化率 / 平均响应分钟
- push: total/success/failure/4xx
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _http_get(base: str, path: str, api_key: str = "",
               timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    url = base.rstrip("/") + path
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        print(f"⚠️  fetch {path} failed: {exc}", file=sys.stderr)
        return None


def _safe_get(d: Optional[Dict[str, Any]], *keys, default=None):
    cur = d
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


def collect_snapshot(base: str, api_key: str = "") -> Dict[str, Any]:
    """拉所有 dashboard 端点 + 整合到 1 行扁平 dict."""
    today = datetime.date.today().isoformat()

    # 1. funnel/stats (近 30 天)
    funnel = _http_get(base, "/cluster/customers/funnel/stats?days=30",
                       api_key) or {}
    cs = funnel.get("customers_by_status") or {}
    ev = funnel.get("events_by_type") or {}

    # 2. handoff pending
    handoffs = _http_get(base,
                          "/cluster/customers/handoff/pending?limit=500",
                          api_key) or {}
    pending = handoffs.get("handoffs") or []
    import time as _t
    now_ts = _t.time()
    breach_n = 0
    for h in pending:
        init_at = h.get("initiated_at")
        if not init_at:
            continue
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(init_at.replace("Z", "+00:00")).timestamp()
            if now_ts - ts > 1800:  # 30 min
                breach_n += 1
        except Exception:
            pass

    # 3. SLA agents (累计)
    sla = _http_get(base, "/cluster/customers/sla/agents?days=30", api_key) or {}
    agents = sla.get("agents") or []
    total_handled = sum(int(a.get("handled") or 0) for a in agents)
    total_converted = sum(int(a.get("converted_n") or 0) for a in agents)
    total_lost = sum(int(a.get("lost_n") or 0) for a in agents)
    avg_accept = None
    accept_samples = 0
    for a in agents:
        if a.get("accept_n"):
            accept_samples += int(a["accept_n"])
    # 加权平均响应分钟 (按 accept_n 加权)
    if accept_samples > 0:
        weighted_sum = 0.0
        for a in agents:
            n = int(a.get("accept_n") or 0)
            m = a.get("avg_accept_minutes")
            if n > 0 and m is not None:
                weighted_sum += float(m) * n
        avg_accept = weighted_sum / accept_samples if accept_samples else None
    overall_conv_rate = (total_converted / total_handled) if total_handled else 0.0

    # 4. referral_decisions aggregate
    rd = _http_get(base, "/cluster/referral-decisions/aggregate?days=30",
                    api_key) or {}
    rd_total = rd.get("total") or 0
    rd_by_level = rd.get("by_level") or {}
    rd_refer_rate = rd.get("refer_rate") or 0.0

    # 5. push metrics
    push = _http_get(base, "/cluster/customers/push/metrics", api_key) or {}
    pm = push.get("metrics") or {}

    return {
        "date": today,
        # 客户分桶
        "cust_in_funnel": int(cs.get("in_funnel") or 0),
        "cust_in_messenger": int(cs.get("in_messenger") or 0),
        "cust_in_line": int(cs.get("in_line") or 0),
        "cust_accepted": int(cs.get("accepted_by_human") or 0),
        "cust_converted": int(cs.get("converted") or 0),
        "cust_lost": int(cs.get("lost") or 0),
        # 漏斗事件 (近 30 天累计)
        "ev_friend_req": int(ev.get("friend_request_sent") or 0),
        "ev_greeting": int(ev.get("greeting_sent") or 0),
        "ev_msg_recv": int(ev.get("message_received") or 0),
        "ev_wa_referral": int(ev.get("wa_referral_sent") or 0),
        "ev_converted": int(ev.get("customer_converted") or 0),
        "ev_lost": int(ev.get("customer_lost") or 0),
        # handoff
        "handoff_pending": len(pending),
        "handoff_breach_30min": breach_n,
        # SLA 累计
        "sla_total_handled": total_handled,
        "sla_total_converted": total_converted,
        "sla_total_lost": total_lost,
        "sla_conversion_rate": round(overall_conv_rate, 4),
        "sla_avg_accept_min": round(avg_accept, 2) if avg_accept is not None else "",
        "sla_active_agents": len(agents),
        # referral_decision
        "rd_total": rd_total,
        "rd_hard_allow": int(rd_by_level.get("hard_allow") or 0),
        "rd_hard_block": int(rd_by_level.get("hard_block") or 0),
        "rd_soft_pass": int(rd_by_level.get("soft_pass") or 0),
        "rd_soft_fail": int(rd_by_level.get("soft_fail") or 0),
        "rd_refer_rate": round(rd_refer_rate, 4),
        # push
        "push_total": int(pm.get("push_total") or 0),
        "push_success": int(pm.get("push_success") or 0),
        "push_failure": int(pm.get("push_failure") or 0),
        "push_4xx": int(pm.get("push_4xx") or 0),
        "push_queue_pending": int(pm.get("queue_pending") or 0),
    }


def write_snapshot(row: Dict[str, Any], output: Path) -> bool:
    """append-only CSV. 第一次写表头, 后续 append 一行."""
    output.parent.mkdir(parents=True, exist_ok=True)
    headers = list(row.keys())
    write_header = not output.exists()
    with output.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            w.writeheader()
        w.writerow(row)
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.environ.get(
        "OPENCLAW_E2E_BASE", "http://127.0.0.1:8000"))
    p.add_argument("--api-key", default=os.environ.get("OPENCLAW_API_KEY", ""))
    p.add_argument("--output", default="reports/daily_snapshot.csv",
                   help="CSV 输出路径; 默认 reports/daily_snapshot.csv (append)")
    p.add_argument("--print-only", action="store_true",
                   help="只打印不写文件")
    args = p.parse_args()

    print(f"📸 拉快照: {args.base}")
    row = collect_snapshot(args.base, args.api_key)

    # 简洁打印 (终端可看)
    print()
    for k in ("date",
              "cust_in_funnel", "cust_in_messenger", "cust_in_line",
              "cust_converted", "cust_lost",
              "ev_friend_req", "ev_greeting", "ev_msg_recv", "ev_wa_referral",
              "handoff_pending", "handoff_breach_30min",
              "sla_total_handled", "sla_conversion_rate", "sla_avg_accept_min",
              "rd_total", "rd_refer_rate",
              "push_total", "push_failure"):
        v = row.get(k, "")
        print(f"  {k:30s} = {v}")

    if not args.print_only:
        out = Path(args.output)
        write_snapshot(row, out)
        print(f"\n✅ 落库: {out}")


if __name__ == "__main__":
    main()
