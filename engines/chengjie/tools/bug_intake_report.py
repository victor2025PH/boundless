# -*- coding: utf-8 -*-
"""报障群值守周读 CLI（bug_intake P2，2026-08-18）。只读，零写风险。

回答三件事（市场经理/工程双视角的裁决读数）：
1. 报障 vs 用法问题占比——用法问题多 = 引导/文档缺陷（产品信号），bug 多 = 版本质量；
2. 工单画像——按严重度/状态分布、Top 重复工单（report_count 即优先级信号）、
   修复回访送达率（notify_ts）；
3. KB 补货建议——用法问题聚类 Top（同类问题反复被问 = 该沉淀成知识条目）。

用法：
    python tools/bug_intake_report.py [--days 7] [--db PATH] [--json]

DB 定位顺序：--db 显式 > AITR_DATA_ROOT/config/bug_intake.db > 自动发现活跃
实例数据根（scripts/_data_root 契约）> CWD config/bug_intake.db。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

_ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE))

from src.ops.bug_intake import titles_similar  # noqa: E402


def discover_db(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit)
    env_root = os.environ.get("AITR_DATA_ROOT", "").strip()
    if env_root:
        return Path(env_root) / "config" / "bug_intake.db"
    try:
        from scripts._data_root import resolve_data_roots
        for root in resolve_data_roots():
            p = Path(root) / "config" / "bug_intake.db"
            if p.exists():
                return p
    except Exception:
        pass
    return Path("config") / "bug_intake.db"


def load_rows(db: Path, since: float) -> Dict[str, List[Dict[str, Any]]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tickets = [dict(r) for r in con.execute(
        "SELECT * FROM bug_tickets WHERE created_ts>=? ORDER BY id DESC",
        (since,)).fetchall()]
    events = [dict(r) for r in con.execute(
        "SELECT * FROM bug_events WHERE ts>=? ORDER BY id DESC",
        (since,)).fetchall()]
    con.close()
    return {"tickets": tickets, "events": events}


def cluster_usage_questions(events: List[Dict[str, Any]],
                            top_n: int = 5) -> List[Dict[str, Any]]:
    """用法问题按标题相似度贪心聚类（复用工单查重同一相似度口径）。

    纯函数可测；每簇记 {text, count}，count 降序——反复被问的就是 KB 缺口。
    """
    clusters: List[Dict[str, Any]] = []
    for ev in events:
        if str(ev.get("kind") or "") != "usage":
            continue
        t = str(ev.get("detail") or "").strip()
        if not t:
            continue
        for c in clusters:
            if titles_similar(t, c["text"], threshold=0.6):
                c["count"] += 1
                break
        else:
            clusters.append({"text": t[:80], "count": 1})
    return sorted(clusters, key=lambda c: -c["count"])[:top_n]


def summarize(data: Dict[str, List[Dict[str, Any]]],
              days: int) -> Dict[str, Any]:
    tickets = data["tickets"]
    events = data["events"]
    kinds: Dict[str, int] = {}
    for ev in events:
        k = str(ev.get("kind") or "?")
        kinds[k] = kinds.get(k, 0) + 1
    bug_n = kinds.get("bug_new", 0) + kinds.get("bug_dup", 0)
    usage_n = kinds.get("usage", 0)
    denom = bug_n + usage_n
    by_sev: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    notified = fixed = 0
    for t in tickets:
        by_sev[str(t.get("severity") or "?")] = by_sev.get(
            str(t.get("severity") or "?"), 0) + 1
        by_status[str(t.get("status") or "?")] = by_status.get(
            str(t.get("status") or "?"), 0) + 1
        if str(t.get("status")) in ("fixed", "verified", "closed"):
            fixed += 1
            if float(t.get("notify_ts") or 0) > 0:
                notified += 1
    top_dup = sorted(
        (t for t in tickets if int(t.get("report_count") or 1) > 1),
        key=lambda t: -int(t.get("report_count") or 1))[:5]
    return {
        "window_days": days,
        "tickets_total": len(tickets),
        "by_severity": by_sev,
        "by_status": by_status,
        "bug_reports": bug_n,
        "usage_questions": usage_n,
        "usage_ratio": round(usage_n / denom, 3) if denom else 0.0,
        "crisis_holds": kinds.get("crisis_hold", 0),
        "fix_notify": {"fixed": fixed, "notified": notified},
        "top_duplicated": [
            {"id": t["id"], "title": str(t.get("title") or "")[:60],
             "count": int(t.get("report_count") or 1),
             "severity": t.get("severity")} for t in top_dup],
        "kb_suggestions": cluster_usage_questions(events),
    }


def render(s: Dict[str, Any]) -> str:
    lines = [
        f"=== 报障群值守周读（近 {s['window_days']} 天）===",
        (f"工单 {s['tickets_total']} 条  严重度 {s['by_severity']}  "
         f"状态 {s['by_status']}"),
        (f"报障 {s['bug_reports']} vs 用法问题 {s['usage_questions']}"
         f"（用法占比 {s['usage_ratio']:.0%}——高=引导/文档缺陷信号）"),
        f"危机词压制 {s['crisis_holds']} 次",
        (f"修复回访：fixed 家族 {s['fix_notify']['fixed']} 条，"
         f"已送达 @回访 {s['fix_notify']['notified']} 条"),
    ]
    if s["top_duplicated"]:
        lines.append("Top 重复工单（报告人数=优先级信号）：")
        for t in s["top_duplicated"]:
            lines.append(f"  #{t['id']} [{t['severity']}] ×{t['count']} "
                         f"{t['title']}")
    if s["kb_suggestions"]:
        lines.append("KB 补货建议（用法问题聚类 Top）：")
        for c in s["kb_suggestions"]:
            lines.append(f"  ×{c['count']}  {c['text']}")
    if not s["tickets_total"] and not s["usage_questions"]:
        lines.append("（窗口内零流量）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="",
                    help="把摘要追加为一行 JSON（周批趋势账本）")
    args = ap.parse_args()
    db = discover_db(args.db)
    if not db.exists():
        print(f"bug_intake.db 不存在：{db}（值守尚无数据）")
        return 0
    data = load_rows(db, time.time() - args.days * 86400)
    s = summarize(data, args.days)
    if args.out_jsonl:
        line = dict(s)
        line["ts"] = time.time()
        p = Path(args.out_jsonl)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(json.dumps(s, ensure_ascii=False, indent=1) if args.json
          else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
