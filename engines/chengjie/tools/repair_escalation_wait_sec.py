"""escalations.wait_sec 历史脏数据体检（P1-1，2026-10-08）——**只读 dry-run，没有 --apply**。

背景：升级快照过去用「末条入站消息 ts」算等待时长，ts≈0 / 早于 2015 的脏会话（LINE 合成
时间戳等）会算出「等了 56 年」。173 zhiliao 实测：10 天 1432 条升级里 875 条 wait_sec>1 年。
线上代码已改为以会话 ``last_in_ts`` 为准并钳制异常时间戳；本工具只盘点**存量**：

- ``bad_base``：该次升级前找不到可信的入站时间（≈0 / 早于 2015）→ 建议 wait_sec=0（未知）；
- ``recalc``：按「升级时刻之前最后一条可信入站」重算，与库里值偏差 >60s 且 >10%；
- ``bad_esc_ts``：升级记录自身 ts 不可信，只报告不给建议；
- ``dup_within_hour``：同会话 1 小时内多条（去重前遗留），只报告。

用法（库以 ``mode=ro`` URI 打开，零写入；``--sql-out`` 只把建议 UPDATE 写成文件供人审）：

  python tools/repair_escalation_wait_sec.py --db D:/chengjie-instances/zhiliao/data/config/inbox.db
  python tools/repair_escalation_wait_sec.py --db <inbox.db> --json --sample 20
  python tools/repair_escalation_wait_sec.py --db <inbox.db> --sql-out escalation_fix.sql

真要修：停写窗口内先备份 inbox.db（.bak_<时间戳>），人工审过 SQL 再执行。退出码恒 0（报告工具）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 与 src/web/routes/unified_inbox_sla.MIN_VALID_INBOUND_TS 同口径（2015-01-01 UTC）
MIN_VALID_TS = 1420070400.0
YEAR_SEC = 365 * 86400
DEDUP_SEC = 3600.0


def _valid(ts: Any) -> Optional[float]:
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return None
    return v if v >= MIN_VALID_TS else None


def open_ro(db: Path) -> sqlite3.Connection:
    uri = "file:" + str(Path(db).resolve()).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def analyze(conn: sqlite3.Connection, *, sample: int = 10) -> Dict[str, Any]:
    """纯读：返回汇总 + 建议（不写库）。"""
    rows = conn.execute(
        "SELECT id, conversation_id, reason, wait_sec, ts FROM escalations "
        "ORDER BY conversation_id, ts").fetchall()
    proposals: List[Dict[str, Any]] = []
    counts = {"total": len(rows), "ok": 0, "bad_base": 0, "recalc": 0,
              "bad_esc_ts": 0, "dup_within_hour": 0, "wait_over_1y": 0}
    prev: Dict[str, float] = {}
    for r in rows:
        cid = str(r["conversation_id"] or "")
        wait = int(r["wait_sec"] or 0)
        if wait > YEAR_SEC:
            counts["wait_over_1y"] += 1
        ets = _valid(r["ts"])
        if ets is None:
            counts["bad_esc_ts"] += 1
            continue
        if cid in prev and ets - prev[cid] < DEDUP_SEC:
            counts["dup_within_hour"] += 1
        prev[cid] = ets
        m = conn.execute(
            "SELECT MAX(ts) AS t FROM messages WHERE conversation_id=? AND direction='in' "
            "AND ts>=? AND ts<=?", (cid, MIN_VALID_TS, ets)).fetchone()
        base = _valid(m["t"] if m is not None else None)
        if base is None:
            new_wait = 0
            kind = "bad_base"
        else:
            new_wait = max(0, int(ets - base))
            diff = abs(new_wait - wait)
            if diff <= 60 or diff <= 0.1 * max(wait, 1):
                counts["ok"] += 1
                continue
            kind = "recalc"
        if kind == "bad_base" and wait == 0:
            counts["ok"] += 1
            continue
        counts[kind] += 1
        proposals.append({"id": int(r["id"]), "conversation_id": cid, "kind": kind,
                          "reason": str(r["reason"] or ""), "ts": ets,
                          "wait_sec": wait, "new_wait_sec": new_wait})
    return {"ok": True, "counts": counts, "proposals": len(proposals),
            "sample": proposals[:max(0, int(sample))], "_all": proposals}


def to_sql(proposals: List[Dict[str, Any]]) -> str:
    lines = ["-- escalations.wait_sec 修复建议（repair_escalation_wait_sec.py 生成；先备份、人工审过再执行）",
             "BEGIN;"]
    for p in proposals:
        lines.append(f"UPDATE escalations SET wait_sec={int(p['new_wait_sec'])} "
                     f"WHERE id={int(p['id'])} AND wait_sec={int(p['wait_sec'])};  -- {p['kind']}")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="escalations.wait_sec 存量体检（只读 dry-run）")
    ap.add_argument("--db", required=True, help="inbox.db 路径（只读打开）")
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sql-out", default="", help="把建议 UPDATE 写到该文件（不执行）")
    a = ap.parse_args(argv)
    db = Path(a.db)
    if not db.is_file():
        print(f"db not found: {db}", file=sys.stderr)
        return 0
    conn = open_ro(db)
    try:
        rep = analyze(conn, sample=a.sample)
    finally:
        conn.close()
    allp = rep.pop("_all")
    if a.sql_out:
        Path(a.sql_out).write_text(to_sql(allp), encoding="utf-8")
        rep["sql_out"] = str(a.sql_out)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        c = rep["counts"]
        print(f"escalations={c['total']} ok={c['ok']} recalc={c['recalc']} bad_base={c['bad_base']} "
              f"bad_esc_ts={c['bad_esc_ts']} dup_within_hour={c['dup_within_hour']} "
              f"wait_over_1y={c['wait_over_1y']}  (dry-run, 未写库)")
        for p in rep["sample"]:
            print(f"  #{p['id']} {p['kind']:8s} {p['conversation_id'][:48]:48s} "
                  f"{p['wait_sec']} -> {p['new_wait_sec']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
