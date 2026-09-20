# -*- coding: utf-8 -*-
"""存量记忆治理清扫（#96 实施91，2026-08-31）——方向可疑/矛盾/重复条目一次盘点。

背景（原图 835/867 实锤）：1.0.63 前的存量抽取产出过三类坏条目——
① 方向反/双解句式（『用户称呼自己为steven』：真相是客户对我方打招呼喊
  steven，被记成客户自称；『用户称呼自己为"fei"』连运营都读不懂指谁）；
② 同槽矛盾并存（同客户『用户今年22岁』『用户21岁』『用户今年21岁』三条
  打架）；③ 同槽同值重复。抽取端已修（batchB prompt 禁双解句式）、消费端
  已降权（无溯源名字类条目带「方向存疑」标注）、矛盾消解已随 consolidate
  默认开（本批），但**存量**条目只有新消息触发 consolidate 的会话才被治理
  ——本工具做全库一次性清扫。

用法（默认 dry-run 只读盘点，绝不写库）：
    python -m scripts.memory_legacy_sweep                # 全实例只读报告
    python -m scripts.memory_legacy_sweep --json         # 机器可读
    python -m scripts.memory_legacy_sweep --user KEY     # 只看某记忆键
    python -m scripts.memory_legacy_sweep --apply        # 实施治理（见下）

--apply 动作（保守，绝不硬删）：
- 同槽矛盾 → 走 store.resolve_contradictions（官方实现：保留最新、旧值标
  stale，保留备查/审计）；
- 同槽同值重复 → 保留最新一条，其余标 stale；
- 双解句式/无溯源名字类条目 → **只报告不动**（方向无原话可核，标 stale 会
  连真值一起杀；消费端降权标注已在，运营经记忆页可编辑/删除）。

数据根解析走 scripts/_data_root 契约（CLI 值 → AITR_DATA_ROOT → 活跃实例 →
引擎根）；dry-run 用 sqlite ro URI 对活体生产库零写事务。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

_SCAN_CAP_PER_USER = 500
_SAMPLE_CAP = 6

# 双解句式（#96 修向②的存量面）：主语「用户…自己」读不出方向的病句形
_DOUBLE_PARSE_MARKERS = ("称呼自己为", "把自己叫做", "对自己的称呼")


def _episodic_db_path(root: Path) -> Optional[Path]:
    """按 skill_manager 同一取径：memory.db_path → <root>/config/bot.db。"""
    try:
        cfg = load_merged_config(root)
        mdb = str(((cfg.get("memory") or {}).get("db_path")) or "").strip()
        p = Path(mdb) if mdb else (Path(root) / "config" / "bot.db")
        return p if p.is_file() else None
    except Exception:
        return None


def _ro_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scan_user(
    conn: sqlite3.Connection, user_id: str,
) -> Dict[str, Any]:
    """一个记忆键的只读盘点：矛盾组 / 同值重复组 / 双解句式 / 无溯源名字类。"""
    from src.utils.episodic_memory_store import _looks_name_class_fact
    from src.utils.memory_slots import extract_slot, slots_conflict

    rows = conn.execute(
        """
        SELECT id, content, created_at, last_seen,
               COALESCE(tier, 'raw') AS tier,
               COALESCE(source_quote, '') AS source_quote
        FROM episodic_memory
        WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'
        ORDER BY created_at DESC LIMIT ?
        """,
        (user_id, _SCAN_CAP_PER_USER),
    ).fetchall()

    groups: Dict[str, List[Dict[str, Any]]] = {}
    double_parse: List[Dict[str, Any]] = []
    nameclass_noquote: List[Dict[str, Any]] = []
    for r in rows:
        content = str(r["content"] or "")
        ts = float(r["last_seen"] if r["last_seen"] is not None
                   else (r["created_at"] or 0.0))
        item = {"id": int(r["id"]), "content": content[:60], "ts": ts}
        if any(m in content for m in _DOUBLE_PARSE_MARKERS):
            double_parse.append(item)
        if not str(r["source_quote"] or "").strip() and \
                _looks_name_class_fact(content):
            nameclass_noquote.append(item)
        slot = extract_slot(content)
        if slot:
            groups.setdefault(slot[0], []).append({**item, "slot": slot})

    conflict_groups: List[Dict[str, Any]] = []
    dup_groups: List[Dict[str, Any]] = []
    dup_stale_ids: List[int] = []
    for base, items in groups.items():
        if len(items) < 2:
            continue
        newest = max(items, key=lambda x: x["ts"])
        conflicts = [it for it in items if it["id"] != newest["id"]
                     and slots_conflict(it["slot"], newest["slot"])]
        dups = [it for it in items if it["id"] != newest["id"]
                and it["slot"] == newest["slot"]]
        if conflicts:
            conflict_groups.append({
                "slot": base, "keep": newest["content"],
                "stale": [c["content"] for c in conflicts],
            })
        if dups:
            dup_groups.append({
                "slot": base, "keep": newest["content"],
                "dups": [d["content"] for d in dups],
            })
            dup_stale_ids.extend(d["id"] for d in dups)

    return {
        "raw_rows": len(rows),
        "conflict_groups": conflict_groups,
        "dup_groups": dup_groups,
        "_dup_stale_ids": dup_stale_ids,
        "double_parse": double_parse[:_SAMPLE_CAP],
        "double_parse_n": len(double_parse),
        "nameclass_noquote": nameclass_noquote[:_SAMPLE_CAP],
        "nameclass_noquote_n": len(nameclass_noquote),
    }


def sweep_root(root: Path, *, user: str = "", apply: bool = False) -> Dict[str, Any]:
    db = _episodic_db_path(root)
    out: Dict[str, Any] = {"root": str(root), "db": str(db or ""),
                           "users": 0, "conflicts": 0, "dups": 0,
                           "double_parse": 0, "nameclass_noquote": 0,
                           "applied": {}, "detail": []}
    if db is None:
        out["skip"] = "no_db"
        return out
    conn = _ro_conn(db)
    try:
        if user:
            uids = [user]
        else:
            uids = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT user_id FROM episodic_memory").fetchall()]
        apply_conflict_users: List[str] = []
        apply_dup_ids: List[int] = []
        for uid in uids:
            rep = scan_user(conn, uid)
            if not (rep["conflict_groups"] or rep["dup_groups"]
                    or rep["double_parse_n"] or rep["nameclass_noquote_n"]):
                continue
            out["users"] += 1
            out["conflicts"] += len(rep["conflict_groups"])
            out["dups"] += len(rep["dup_groups"])
            out["double_parse"] += rep["double_parse_n"]
            out["nameclass_noquote"] += rep["nameclass_noquote_n"]
            dup_ids = rep.pop("_dup_stale_ids", [])
            if rep["conflict_groups"]:
                apply_conflict_users.append(uid)
            apply_dup_ids.extend(dup_ids)
            out["detail"].append({"user": uid, **rep})
    finally:
        conn.close()

    if apply and (apply_conflict_users or apply_dup_ids):
        from src.utils.episodic_memory_store import EpisodicMemoryStore
        store = EpisodicMemoryStore(db)
        superseded = 0
        for uid in apply_conflict_users:
            try:
                r = store.resolve_contradictions(uid)
                superseded += int(r.get("superseded") or 0)
            except Exception:
                pass
        dup_marked = 0
        if apply_dup_ids:
            try:
                wconn = store._conn  # noqa: SLF001 —— 官方连接，短事务
                marks = [(i,) for i in apply_dup_ids]
                cur = wconn.executemany(
                    "UPDATE episodic_memory SET tier='stale' "
                    "WHERE id=? AND COALESCE(tier,'raw')='raw'", marks)
                wconn.commit()
                dup_marked = int(cur.rowcount or 0)
            except Exception:
                pass
        out["applied"] = {"conflict_superseded": superseded,
                          "dup_marked_stale": dup_marked}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Win 控制台 GBK 防线
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="存量记忆治理清扫（#96）")
    ap.add_argument("--data-root", default="", help="显式数据根（缺省=契约解析）")
    ap.add_argument("--user", default="", help="只扫某个记忆键")
    ap.add_argument("--apply", action="store_true",
                    help="实施治理（矛盾消解+同值重复标 stale；缺省只读报告）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root)
    t0 = time.time()
    reports = [sweep_root(r, user=args.user, apply=args.apply) for r in roots]
    if args.json:
        print(json.dumps({"reports": reports, "elapsed_sec": round(
            time.time() - t0, 2)}, ensure_ascii=False, indent=1))
        return 0
    for rep in reports:
        print(f"=== {rep['root']} ===")
        if rep.get("skip"):
            print(f"  跳过（{rep['skip']}）")
            continue
        print(f"  受影响记忆键 {rep['users']}  矛盾组 {rep['conflicts']}  "
              f"同值重复组 {rep['dups']}  双解句式 {rep['double_parse']}  "
              f"无溯源名字类 {rep['nameclass_noquote']}")
        for d in rep["detail"][:20]:
            print(f"  - {d['user']}")
            for g in d["conflict_groups"][:4]:
                print(f"      [矛盾:{g['slot']}] 保「{g['keep']}」 "
                      f"降 {g['stale']}")
            for g in d["dup_groups"][:4]:
                print(f"      [重复:{g['slot']}] 保「{g['keep']}」 "
                      f"降 {g['dups']}")
            for it in d["double_parse"][:3]:
                print(f"      [双解句式] {it['content']}")
            for it in d["nameclass_noquote"][:3]:
                print(f"      [无溯源名字类] {it['content']}")
        if rep.get("applied"):
            print(f"  已实施: {rep['applied']}")
        elif rep["users"]:
            print("  （dry-run；--apply 实施矛盾消解+同值重复降级）")
    print(f"耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
