#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat review CLI — 5 min/天高效过真实对话, 判断 bot 自然度.

为什么这个工具:
- "人工 review 至少 50 条对话" 是 OPS_RUNBOOK 的核心动作
- dashboard 翻 50 条要 30 min 太慢
- CLI 一条一条过 + 快捷键标注, 5 min/天可持续

用法:
    python scripts/chat_review.py                   # 默认 18080, 拉最近活跃 20 条
    python scripts/chat_review.py --limit 50
    python scripts/chat_review.py --status in_messenger  # 只看 messenger 阶段的
    python scripts/chat_review.py --customer <cid>       # 只看一个

交互快捷键:
    g  → good (健康对话)
    b  → bad (有问题, 提示输入 note)
    s  → skip (跳过, 不标注)
    n  → next (下一条)
    p  → prev
    q  → quit (随时退出, 已标注的存 CSV)

输出: reports/chat_review_YYYYMMDD.csv (append-only)
    customer_id, primary_name, status, decision, note, reviewed_at
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
from typing import Any, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ── HTTP ─────────────────────────────────────────────────────────────
def _http_get(base: str, path: str, api_key: str = "",
               timeout: float = 10.0) -> Optional[Any]:
    url = base.rstrip("/") + path
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        print(f"⚠️  fetch {path}: {exc}", file=sys.stderr)
        return None


# ── 显示 ─────────────────────────────────────────────────────────────
def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty():
        return msg
    return f"\033[{code}m{msg}\033[0m"


def _fmt_time(s: str) -> str:
    if not s:
        return "?"
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%m-%d %H:%M")
    except Exception:
        return s


def render_customer(c: Dict[str, Any]) -> None:
    """打印一条客户的完整 review 视图."""
    print()
    print(_color("36", "═" * 70))
    print(_color("1;36",
                  f"  👤 {c.get('primary_name') or '?'}  "
                  f"({(c.get('customer_id') or '')[:12]}…)"))
    print(_color("36", "═" * 70))

    ai = c.get("ai_profile") or {}
    print(f"  status: {_color('33', c.get('status') or '?')}    "
          f"persona: {ai.get('persona_key') or '?'}    "
          f"variant: {ai.get('ab_variant') or '?'}    "
          f"priority: {c.get('priority_tag') or '?'}    "
          f"country: {c.get('country') or '?'}")

    # AI 画像 (可选)
    if ai.get("topics") or ai.get("interests"):
        print(f"  画像: {_color('90', json.dumps(ai, ensure_ascii=False)[:120])}")

    # handoff
    handoffs = c.get("handoffs") or []
    if handoffs:
        print()
        print(_color("35", "  📋 Handoff:"))
        for h in handoffs[-3:]:
            outcome = h.get("outcome") or "未完结"
            outcome_c = ("32" if outcome == "converted"
                          else "31" if outcome == "lost" else "33")
            print(f"    {_fmt_time(h.get('initiated_at'))} → "
                  f"{_fmt_time(h.get('completed_at') or '')} "
                  f"({_color(outcome_c, outcome)})  "
                  f"接管: {h.get('accepted_by_human') or '—'}  "
                  f"摘要: {(h.get('ai_summary') or '')[:60]}")

    # chat 历史 (按时间正序, 让 review 像读对话)
    chats = c.get("chats") or []
    if not chats:
        print()
        print(_color("90", "  (无聊天历史)"))
        return

    print()
    print(_color("36", f"  💬 聊天 ({len(chats)} 条):"))
    print()
    for ch in chats:
        direction = ch.get("direction")
        ts_str = _fmt_time(ch.get("ts"))
        content = (ch.get("content") or "").replace("\n", " ")
        if direction == "incoming":
            print(f"    {_color('34', '客户')} {_color('90', ts_str)} "
                  f"{_color('34', '◀')}  {content}")
        else:
            ai_tag = " [AI]" if ch.get("ai_generated") else ""
            tpl_tag = (f" [{ch.get('template_id')}]"
                       if ch.get("template_id") else "")
            print(f"    {_color('32', 'Bot ')} {_color('90', ts_str)} "
                  f"{_color('32', '▶')}  {content}{_color('90', ai_tag + tpl_tag)}")

    print()
    print(_color("36", "─" * 70))


def review_loop(base: str, api_key: str, customers: List[Dict[str, Any]],
                output_csv: Path) -> None:
    """主交互循环."""
    if not customers:
        print(_color("33", "⚠️  没有可 review 的客户. 用 --limit / --status 调整范围"))
        return

    # 准备 CSV
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_csv.exists()
    csv_f = output_csv.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(
        csv_f, fieldnames=[
            "customer_id", "primary_name", "status",
            "decision", "note", "reviewed_at",
        ])
    if write_header:
        writer.writeheader()

    idx = 0
    reviewed = 0
    try:
        while idx < len(customers):
            c = customers[idx]
            cid = c.get("customer_id")
            # 拉详情 (含 chats + handoffs)
            detail = _http_get(base, f"/cluster/customers/{cid}", api_key)
            if not detail:
                print(_color("31", f"❌ 拉详情失败, skip {cid}"))
                idx += 1
                continue
            render_customer(detail)
            print(_color("1;36",
                          f"  [{idx + 1}/{len(customers)}]  "
                          "[g]ood  [b]ad  [s]kip  [n]ext  [p]rev  [q]uit"))
            ans = input("  > ").strip().lower()
            if ans == "q":
                break
            elif ans == "p":
                idx = max(0, idx - 1)
                continue
            elif ans == "g":
                writer.writerow({
                    "customer_id": cid,
                    "primary_name": detail.get("primary_name") or "",
                    "status": detail.get("status") or "",
                    "decision": "good",
                    "note": "",
                    "reviewed_at": datetime.datetime.utcnow().isoformat(),
                })
                csv_f.flush()
                reviewed += 1
                print(_color("32", "  ✅ 标 good"))
            elif ans == "b":
                note = input(
                    _color("31", "  bad note (一句话, Enter 跳过): ")
                ).strip()
                writer.writerow({
                    "customer_id": cid,
                    "primary_name": detail.get("primary_name") or "",
                    "status": detail.get("status") or "",
                    "decision": "bad",
                    "note": note,
                    "reviewed_at": datetime.datetime.utcnow().isoformat(),
                })
                csv_f.flush()
                reviewed += 1
                print(_color("31", "  ❌ 标 bad"))
            elif ans == "s":
                print(_color("90", "  ⊘ skip"))
            # n / 空回车默认 next
            idx += 1
    finally:
        csv_f.close()
    print()
    print(_color("1;36",
                  f"📝 review 结束 · 已标注 {reviewed} 条 · "
                  f"输出: {output_csv}"))


# ── main ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.environ.get(
        "OPENCLAW_E2E_BASE", "http://127.0.0.1:18080"))
    p.add_argument("--api-key", default=os.environ.get("OPENCLAW_API_KEY", ""))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--status", default="",
                   help="只看某 status (e.g. in_messenger)")
    p.add_argument("--customer", default="", help="只 review 单一 customer_id")
    p.add_argument("--output", default="",
                   help="CSV 输出路径; 默认 reports/chat_review_YYYYMMDD.csv")
    args = p.parse_args()

    output = (Path(args.output) if args.output
              else Path("reports") /
              f"chat_review_{datetime.date.today().strftime('%Y%m%d')}.csv")

    # 拉客户列表
    if args.customer:
        c = _http_get(args.base, f"/cluster/customers/{args.customer}",
                       args.api_key)
        customers = [c] if c else []
    else:
        qs = f"limit={max(1, min(args.limit, 200))}"
        if args.status:
            qs += f"&status={args.status}"
        data = _http_get(args.base, f"/cluster/customers-search?{qs}",
                          args.api_key)
        if not data:
            data = _http_get(args.base, f"/cluster/customers?{qs}",
                              args.api_key)
        # customers-search 返 {customers:[...]}, customers 可能直接返 list
        if isinstance(data, dict):
            customers = data.get("customers") or []
        elif isinstance(data, list):
            customers = data
        else:
            customers = []
    print(_color("1;36",
                  f"📋 准备 review {len(customers)} 个客户  "
                  f"(base={args.base}  output={output})"))
    review_loop(args.base, args.api_key, customers, output)


if __name__ == "__main__":
    main()
