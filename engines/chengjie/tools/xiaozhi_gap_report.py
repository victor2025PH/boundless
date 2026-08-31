# -*- coding: utf-8 -*-
"""小智语料缺口日/周报：读生产 qa_log（只读），输出「该补什么」照单。

飞轮的取数半边（qa_log 模块 docstring 里写明的「飞轮起点」）：
  真实用户问题 → 未答（answered=0）/差评（verdict=down）按归一化键聚频
  → 本清单 → 人工/agent 按红线补 help_terms / howto_pack（只写真实行为）
  → tools/xiaozhi_dialog_eval.py 金标复测 → 随下个包出货。

只读纪律：sqlite 以 mode=ro URI 打开，绝不在生产库上跑 CREATE/DELETE
（AssistantQALog 类的 ensure_schema/滚动清理都不走）。默认扫全部活跃
实例数据根，--db 可指定单库。

跑法：
    python tools/xiaozhi_gap_report.py [--days 14] [--limit 20] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))


def _norm_q(q: str) -> str:
    """与 src.assistant.qa_log._norm_q 同口径（这里复制以保持零导入副作用——
    导入 qa_log 模块本身无害，但保持本工具对生产代码零依赖更稳）。"""
    s = re.sub(r"[\s\u3000]+", "", str(q or ""))
    s = re.sub(r"[?？!！。.,，;；:：~～]+$", "", s)
    return s.lower()[:120]


def _discover_dbs() -> list[Path]:
    roots = []
    inst_base = Path(r"D:\chengjie-instances")
    if inst_base.is_dir():
        for d in inst_base.iterdir():
            f = d / "data" / "config" / "assistant.db"
            if f.is_file():
                roots.append(f)
    return roots


def _scan(db: Path, since: float) -> tuple[list, dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT q, answered, verdict, ts FROM qa_log WHERE ts >= ?",
            (since,)).fetchall()
    finally:
        con.close()
    total = len(rows)
    answered = sum(1 for r in rows if r["answered"])
    down = sum(1 for r in rows if r["verdict"] == "down")
    agg: dict[str, dict] = {}
    for r in rows:
        if r["answered"] and r["verdict"] != "down":
            continue
        key = _norm_q(r["q"])
        if not key:
            continue
        slot = agg.setdefault(key, {"q": str(r["q"])[:120], "count": 0,
                                    "down": 0, "last_ts": 0.0})
        slot["count"] += 1
        slot["last_ts"] = max(slot["last_ts"], float(r["ts"]))
        if r["verdict"] == "down":
            slot["down"] += 1
    misses = sorted(agg.values(), key=lambda x: (-x["count"], x["q"]))
    return misses, {"total": total, "answered": answered, "down": down}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--db", default="", help="指定单个 assistant.db（默认扫全部实例）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    dbs = [Path(args.db)] if args.db else _discover_dbs()
    dbs = [d for d in dbs if d.is_file()]
    if not dbs:
        print("SKIP: 找不到 assistant.db（无实例数据根/--db 路径不存在）")
        return 0

    since = time.time() - max(1, args.days) * 86400
    out = {}
    for db in dbs:
        try:
            misses, stats = _scan(db, since)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] {db}: {exc}")
            continue
        inst = db.parts[-4] if len(db.parts) >= 4 else str(db)
        out[inst] = {"stats": stats, "misses": misses[: args.limit]}

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for inst, data in out.items():
        st = data["stats"]
        rate = (st["answered"] / st["total"] * 100) if st["total"] else 0.0
        print(f"\n== {inst}: 近{args.days}天 {st['total']} 问，自答率 "
              f"{rate:.0f}%，差评 {st['down']}")
        if not data["misses"]:
            print("   （无未答/差评——语料当前无缺口）")
            continue
        print("   缺口照单（补 src/web/help_terms.py / src/assistant/howto_pack.py，"
              "红线：只写已验证的真实行为）：")
        for m in data["misses"]:
            mark = f" 👎x{m['down']}" if m["down"] else ""
            print(f"   {m['count']:>3}x{mark}  {m['q']}")
    print("\n补完语料后跑：python tools/xiaozhi_dialog_eval.py（金标复测+趋势）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
