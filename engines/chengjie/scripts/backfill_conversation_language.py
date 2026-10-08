#!/usr/bin/env python
"""会话语言历史回填（2026-10-08 智语 · P1-5）。

按会话的**入站**消息文本重新投票（``src.inbox.session_lang.vote_session_language``，与实时
路径同一实现），报告回填后的语言分布、unknown 占比、仍判不出的原因，并可导出回填计划。

默认 **dry-run，不写 inbox.db**：计划阶段以只读方式打开（sqlite ``mode=ro``）。
``--sql-out`` 只生成带护栏的 UPDATE 语句文件。``--apply`` 才真正写入，并且只更新
``fill_unknown``（当前 ``language`` 为 ``unknown`` 或空）的行；``tl_upgrade_suggestions``
（例如已标 en 的 Taglish）保持建议，不改已有语言。

    python scripts/backfill_conversation_language.py --db config/inbox.db
    python scripts/backfill_conversation_language.py --db config/inbox.db --recheck en,id \
        --plan-out lang_plan.json --sql-out lang_plan.sql
    python scripts/backfill_conversation_language.py --db config/inbox.db --apply

``--recheck en,id``：额外列出当前是 en / id、但入站证据显示是他加禄（Taglish）的会话（只列建议）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _open_ro(db: Path) -> sqlite3.Connection:
    uri = "file:" + db.resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_plan(db: Path, *, recheck: List[str] = (), per_conv: int = 40,
               include_groups: bool = True) -> Dict[str, Any]:
    from src.inbox.session_lang import vote_session_language
    conn = _open_ro(db)
    try:
        convs = conn.execute(
            "SELECT conversation_id, platform, chat_type, language FROM conversations"
        ).fetchall()
        before = Counter((r["language"] or "unknown") for r in convs)
        targets = {"unknown", ""} | {x.strip().lower() for x in recheck if x.strip()}
        changes: List[Dict[str, Any]] = []
        upgrades: List[Dict[str, Any]] = []
        still: Counter = Counter()
        after = Counter(before)
        for r in convs:
            cur = (r["language"] or "unknown").lower()
            if cur not in targets:
                continue
            if not include_groups and str(r["chat_type"] or "private") != "private":
                continue
            rows = conn.execute(
                "SELECT text FROM messages WHERE conversation_id=? AND direction='in' "
                "AND TRIM(IFNULL(text,''))!='' ORDER BY ts DESC LIMIT ?",
                (r["conversation_id"], int(per_conv)),
            ).fetchall()
            vote = vote_session_language([x["text"] for x in rows])
            new = str(vote["lang"])
            if cur in ("unknown", ""):
                if new == "unknown":
                    still[str(vote["evidence"])] += 1
                    continue
                changes.append({"conversation_id": r["conversation_id"], "platform": r["platform"],
                                "from": cur or "unknown", "to": new, "variant": vote["variant"],
                                "votes": vote["votes"], "n_inbound": len(rows)})
                after[cur or "unknown"] -= 1
                after[new] += 1
            elif new == "tl" and cur != "tl":
                upgrades.append({"conversation_id": r["conversation_id"], "platform": r["platform"],
                                 "from": cur, "to": new, "variant": vote["variant"],
                                 "votes": vote["votes"], "n_inbound": len(rows)})
    finally:
        conn.close()
    total = sum(before.values())
    unk_b = before.get("unknown", 0) + before.get("", 0)
    unk_a = after.get("unknown", 0) + after.get("", 0)
    return {
        "mode": "dry-run",
        "db": str(db),
        "total": total,
        "before": dict(before.most_common()),
        "after": {k: v for k, v in after.most_common() if v},
        "unknown_pct_before": round(unk_b / total * 100, 1) if total else 0.0,
        "unknown_pct_after": round(unk_a / total * 100, 1) if total else 0.0,
        "fill_unknown": changes,
        "tl_upgrade_suggestions": upgrades,
        "still_unknown_reasons": dict(still),
    }


def apply_unknown_fills(db: Path, plan: Dict[str, Any]) -> Dict[str, int]:
    """把 ``fill_unknown`` 写入库。只改 ``language IN ('unknown','')`` 的行。

    不读取 ``tl_upgrade_suggestions``。同一计划再跑一遍时，已经写过的行不再命中
    WHERE，``updated`` 为 0。读计划仍走 ``build_plan`` 的只读连接；本函数才读写打开。
    """
    conn = sqlite3.connect(str(db))
    updated = skipped = 0
    try:
        conn.execute("BEGIN")
        for row in plan.get("fill_unknown") or []:
            new_lang = str(row.get("to") or "").strip().lower()
            cid = str(row.get("conversation_id") or "")
            if not cid or not new_lang or new_lang == "unknown":
                skipped += 1
                continue
            cur = conn.execute(
                "UPDATE conversations SET language=? "
                "WHERE conversation_id=? AND language IN ('unknown','')",
                (new_lang, cid),
            )
            if cur.rowcount == 1:
                updated += 1
            else:
                skipped += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"updated": updated, "skipped": skipped}


def plan_sql(plan: Dict[str, Any]) -> str:
    lines = ["-- 会话语言回填计划（dry-run 生成，未执行）。审过后在停机窗口执行；",
             "-- 每条都带 language='unknown' 护栏，重复执行无副作用。",
             "BEGIN;"]
    for c in plan["fill_unknown"]:
        cid = str(c["conversation_id"]).replace("'", "''")
        lines.append(
            f"UPDATE conversations SET language='{c['to']}' "
            f"WHERE conversation_id='{cid}' AND language IN ('unknown','');")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="会话语言历史回填（默认 dry-run；--apply 只补 unknown）")
    ap.add_argument("--db", default="config/inbox.db")
    ap.add_argument("--apply", action="store_true",
                    help="写入 fill_unknown。不改已有语言，也不执行 tl 改判建议")
    ap.add_argument("--recheck", default="", help="逗号分隔：额外复核这些现有语言（如 en,id）是否其实是 tl")
    ap.add_argument("--per-conv", type=int, default=40, help="每个会话最多取多少条入站消息投票")
    ap.add_argument("--private-only", action="store_true", help="只看私聊")
    ap.add_argument("--plan-out", default="", help="把完整计划写成 JSON（含会话 id，注意别外传）")
    ap.add_argument("--sql-out", default="", help="生成带护栏的 UPDATE 语句文件（不执行）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    db = Path(a.db)
    if not db.exists():
        print(f"inbox.db 不存在: {db}", file=sys.stderr)
        return 1
    plan = build_plan(db, recheck=[x for x in a.recheck.split(",") if x],
                      per_conv=a.per_conv, include_groups=not a.private_only)
    if a.plan_out:
        Path(a.plan_out).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.sql_out:
        Path(a.sql_out).write_text(plan_sql(plan), encoding="utf-8")
    if a.json:
        print(json.dumps({k: v for k, v in plan.items()
                          if k not in ("fill_unknown", "tl_upgrade_suggestions")}
                         | {"fill_unknown_n": len(plan["fill_unknown"]),
                            "tl_upgrade_n": len(plan["tl_upgrade_suggestions"])},
                         ensure_ascii=False, indent=2))
    else:
        tag = "[dry-run]" if not a.apply else "[plan]"
        tail = "未写库" if not a.apply else "随后只补 unknown"
        print(f"{tag} {db}  会话 {plan['total']} 个（{tail}）")
        print(f"  现状: {plan['before']}")
        print(f"  回填后: {plan['after']}")
        print(f"  unknown: {plan['unknown_pct_before']}% → {plan['unknown_pct_after']}%")
        print(f"  可补 unknown: {len(plan['fill_unknown'])} 个；仍判不出: {plan['still_unknown_reasons']}")
        if plan["tl_upgrade_suggestions"]:
            print(f"  建议改判 tl（仅建议）: {len(plan['tl_upgrade_suggestions'])} 个")
        by = Counter(c["to"] for c in plan["fill_unknown"])
        if by:
            print(f"  补写分布: {dict(by)}")
    if a.apply:
        result = apply_unknown_fills(db, plan)
        print(f"[apply] updated={result['updated']} skipped={result['skipped']} "
              f"(tl suggestions not written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
