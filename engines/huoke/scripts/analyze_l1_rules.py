# -*- coding: utf-8 -*-
"""P2-4 Sprint D-1 CLI: 离线分析 L1 规则的真阳性率 + 兴趣热榜。

用法::
    python scripts/analyze_l1_rules.py                       # 最近 7 天
    python scripts/analyze_l1_rules.py --hours 72            # 最近 72 小时
    python scripts/analyze_l1_rules.py --persona jp_female_midlife
    python scripts/analyze_l1_rules.py --interests           # 只看兴趣热榜

产出：
    ┌──────────────────────────┬──────┬────────┬──────────┬─────────────┐
    │ reason                   │ hits │ L2 cfm │ precision│ 建议        │
    ├──────────────────────────┼──────┼────────┼──────────┼─────────────┤
    │ 昵称含 田(+30)           │ 18   │ 14     │ 0.778    │ boost       │
    │ 昵称为纯日文假名/汉字... │ 34   │  6     │ 0.176    │ demote      │
    └──────────────────────────┴──────┴────────┴──────────┴─────────────┘
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _y(s): return f"\033[33m{s}\033[0m"
def _b(s): return f"\033[36m{s}\033[0m"


def _format_row(cols, widths):
    return " │ ".join(str(c).ljust(w) for c, w in zip(cols, widths))


def analyze_rules(hours: int, persona_key: str | None):
    from src.host.database import get_conn

    since = f"-{hours} hours"
    sql = """SELECT target_key, stage, match, insights_json
             FROM fb_profile_insights
             WHERE classified_at >= datetime('now', ?)"""
    params = [since]
    if persona_key:
        sql += " AND persona_key = ?"
        params.append(persona_key)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    per_target: dict = {}
    for r in rows:
        tk = r[0]
        stage = r[1]
        match = int(r[2] or 0)
        try:
            ij = json.loads(r[3] or "{}")
        except Exception:
            ij = {}
        d = per_target.setdefault(tk, {"reasons": set(), "l2_matched": False})
        for rz in (ij.get("l1_reasons") or []):
            d["reasons"].add(str(rz))
        if stage == "L2" and match == 1:
            d["l2_matched"] = True

    reason_agg: dict = {}
    for tk, info in per_target.items():
        for rz in info["reasons"]:
            a = reason_agg.setdefault(rz, {"hits": 0, "l2_match": 0})
            a["hits"] += 1
            if info["l2_matched"]:
                a["l2_match"] += 1

    if not reason_agg:
        print(_y(f"窗口内（{hours}h, persona={persona_key or '*'}）没有 L1 reasons 数据。"))
        print("   先跑至少 1 次 profile_hunt，再回来看。")
        return

    total = len(per_target)
    match_cnt = sum(1 for d in per_target.values() if d["l2_matched"])
    print(_b(f"\n┌─ L1 rule analytics (hours={hours}, persona={persona_key or '*'}) ─"))
    print(f"│  总 target: {total}   L2 matched: {match_cnt}   overall match rate: "
          f"{_g(f'{match_cnt/total:.1%}') if total else '0%'}")

    widths = [40, 6, 6, 10, 18]
    header = _format_row(["reason", "hits", "L2cfm", "precision", "建议"], widths)
    print("│  " + header)
    print("│  " + "─" * len(header))
    items = sorted(reason_agg.items(), key=lambda kv: (-kv[1]["hits"], -kv[1]["l2_match"]))
    for rz, a in items:
        p = (a["l2_match"] / a["hits"]) if a["hits"] else 0
        hint = ""
        if a["hits"] >= 5:
            if p >= 0.75:
                hint = _g("↑ boost")
            elif p <= 0.15:
                hint = _r("↓ demote")
            else:
                hint = _y("— keep")
        else:
            hint = "(样本少)"
        print("│  " + _format_row([rz[:40], a["hits"], a["l2_match"], f"{p:.3f}", hint], widths))
    print("└──────────────────────────────────────────────────────────")


def analyze_interests(hours: int, persona_key: str | None, limit: int):
    from src.host.database import get_conn

    since = f"-{hours} hours"
    sql = """SELECT topic, COUNT(*) AS n, COUNT(DISTINCT device_id) AS devs,
                    MAX(seen_at) AS last_seen
             FROM fb_content_exposure
             WHERE seen_at >= datetime('now', ?)"""
    params = [since]
    if persona_key:
        sql += " AND meta_json LIKE ?"
        params.append(f'%"persona_key": "{persona_key}"%')
    sql += " GROUP BY topic ORDER BY n DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        print(_y(f"\n窗口内 ({hours}h) 没有兴趣数据（需要有 L2 命中过的用户）。"))
        return

    print(_b(f"\n┌─ 兴趣热榜 top {limit} (hours={hours}, persona={persona_key or '*'}) ─"))
    w = [28, 6, 6, 20]
    print("│  " + _format_row(["topic", "count", "devs", "last_seen"], w))
    print("│  " + "─" * 62)
    for r in rows:
        print("│  " + _format_row([str(r[0])[:28], r[1], r[2], r[3] or ""], w))
    print("└──────────────────────────────────────────────────────────")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--persona", default=None)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--interests", action="store_true", help="只输出兴趣热榜")
    ap.add_argument("--rules", action="store_true", help="只输出 L1 规则分析")
    args = ap.parse_args()

    show_rules = args.rules or not (args.interests)
    show_ints = args.interests or not (args.rules)

    if show_rules:
        analyze_rules(args.hours, args.persona)
    if show_ints:
        analyze_interests(args.hours, args.persona, args.limit)


if __name__ == "__main__":
    main()
