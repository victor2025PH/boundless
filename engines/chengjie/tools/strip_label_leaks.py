# -*- coding: utf-8 -*-
"""清理已落库的「系统标签泄漏」出站行（2026-09-12「[我方语音消息]」事故收尾工具）。

事故期落库的出站行正文带着 LLM 照抄的系统标注（「[我方发出的语音] 对呀…」「[我方语音消息]
天哪…」）。它们已经发给客户、撤不回；但留在库里会（a）继续以 assistant 内容进 LLM 历史
教坏模型（归一层虽会剥「[我方发出的…]」，其它改写形态剥不到）、（b）污染 last_reply /
会话预览 / 接力摘要。本工具按巡检同一判定（``label_leak_scan.classify_outbound_text``）
找出 ``system_label`` 行，用出稿口同一剥法（``strip_system_labels``）改写 ``text`` /
``original_text``，只动这两列。

安全边界：
- **默认 dry-run**：只打印「改前 → 改后」；``--apply`` 才写库；
- 写库前把原行整份落 JSON 备份（``--backup`` 指定路径，缺省 ``<数据根>/.ops/label_leak_backup_<ts>.json``）；
- 单事务、``busy_timeout`` 5s；``messages_fts`` 由 store 的 AFTER UPDATE OF text 触发器自动同步；
- 只改 ``direction='out'``、未软删、未撤回的行；``bracket_prefix``（低置信）只列不改。

用法::

    python tools/strip_label_leaks.py                    # dry-run，最近 7 天，自动发现活跃实例
    python tools/strip_label_leaks.py --hours 48 --apply # 真改
    python tools/strip_label_leaks.py --db D:/x/inbox.db --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.ai.outbound_text_guard import strip_system_labels  # noqa: E402
from src.inbox.label_leak_scan import KIND_SYSTEM_LABEL, classify_outbound_text  # noqa: E402

def _select_sql(con: sqlite3.Connection) -> str:
    """按库里实际有的列拼查询——退役/未迁移的老库可能没有 sent_by / revoked / deleted_at。"""
    cols = {str(r[1]) for r in con.execute("PRAGMA table_info(messages)").fetchall()}
    sent_by = "sent_by" if "sent_by" in cols else "'' AS sent_by"
    original = "original_text" if "original_text" in cols else "'' AS original_text"
    where = ["direction = 'out'", "ts > ?", "(text LIKE '[%' OR text LIKE '【%')"]
    if "deleted_at" in cols:
        where.append("deleted_at = 0")
    if "revoked" in cols:
        where.append("revoked = 0")
    return (f"SELECT message_id, conversation_id, ts, text, {original}, media_type, {sent_by} "
            f"FROM messages WHERE {' AND '.join(where)} ORDER BY ts")


def _open(db_path: Path, *, rw: bool) -> sqlite3.Connection:
    if rw:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.execute("PRAGMA busy_timeout = 5000")
    else:
        uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def plan(db_path: Path, *, since_ts: float) -> List[Dict[str, Any]]:
    """找出要改的行：``[{message_id, conversation_id, ts, kind, before, after,
    before_original, after_original, media_type}]``（kind=system_label 才有 after）。"""
    con = _open(db_path, rw=False)
    try:
        rows = con.execute(_select_sql(con), (float(since_ts),)).fetchall()
    finally:
        con.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        text = str(r["text"] or "")
        kind = classify_outbound_text(
            text, media_type=str(r["media_type"] or ""), sent_by=str(r["sent_by"] or ""))
        if not kind:
            continue
        item: Dict[str, Any] = {
            "message_id": str(r["message_id"]), "conversation_id": str(r["conversation_id"]),
            "ts": float(r["ts"] or 0), "kind": kind, "media_type": str(r["media_type"] or ""),
            "before": text, "after": text,
            "before_original": str(r["original_text"] or ""),
            "after_original": str(r["original_text"] or ""),
        }
        if kind == KIND_SYSTEM_LABEL:
            item["after"] = strip_system_labels(text)[0]
            if item["before_original"]:
                item["after_original"] = strip_system_labels(item["before_original"])[0]
        out.append(item)
    return out


def apply(db_path: Path, items: List[Dict[str, Any]], *, backup: Path) -> int:
    """写库（只改 system_label 行）。先备份原行，再单事务 UPDATE。返回改动行数。"""
    todo = [i for i in items if i["kind"] == KIND_SYSTEM_LABEL and i["after"] != i["before"]]
    if not todo:
        return 0
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({
        "db": str(db_path), "ts": time.time(),
        "rows": [{"message_id": i["message_id"], "conversation_id": i["conversation_id"],
                  "text": i["before"], "original_text": i["before_original"]} for i in todo],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    con = _open(db_path, rw=True)
    n = 0
    try:
        cols = {str(r[1]) for r in con.execute("PRAGMA table_info(messages)").fetchall()}
        has_original = "original_text" in cols
        with con:
            for i in todo:
                if has_original:
                    cur = con.execute(
                        "UPDATE messages SET text = ?, original_text = ? "
                        "WHERE message_id = ? AND text = ?",
                        (i["after"], i["after_original"], i["message_id"], i["before"]))
                else:
                    cur = con.execute(
                        "UPDATE messages SET text = ? WHERE message_id = ? AND text = ?",
                        (i["after"], i["message_id"], i["before"]))
                n += int(cur.rowcount or 0)
    finally:
        con.close()
    return n


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", default="", help="inbox.db 路径（缺省：活跃实例数据根/config/inbox.db）")
    ap.add_argument("--data-root", default="", help="实例数据根（与 --db 二选一）")
    ap.add_argument("--hours", type=float, default=168.0, help="回看小时数（默认 7 天）")
    ap.add_argument("--apply", action="store_true", help="真改库（缺省 dry-run）")
    ap.add_argument("--backup", default="", help="备份 JSON 路径（--apply 时）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    if args.db:
        dbs = [Path(args.db)]
        roots = [Path(args.db).resolve().parent.parent]
    else:
        roots = resolve_data_roots(args.data_root)
        dbs = [r / "config" / "inbox.db" for r in roots]
    since = time.time() - max(0.5, float(args.hours)) * 3600.0
    report: Dict[str, Any] = {"apply": bool(args.apply), "dbs": [], "changed": 0}
    rc = 0
    for root, db in zip(roots, dbs):
        if not db.is_file():
            report["dbs"].append({"db": str(db), "missing": True})
            continue
        items = plan(db, since_ts=since)
        entry: Dict[str, Any] = {"db": str(db), "found": len(items),
                                 "system_label": sum(1 for i in items if i["kind"] == KIND_SYSTEM_LABEL),
                                 "items": items}
        if not args.json:
            print(f"== {db}  found={len(items)}  system_label={entry['system_label']}  "
                  f"({'APPLY' if args.apply else 'dry-run'})")
            for i in items:
                mark = "FIX " if i["kind"] == KIND_SYSTEM_LABEL else "note"
                print(f"  [{mark}] {_fmt_ts(i['ts'])} {i['conversation_id']} media={i['media_type'] or '-'}")
                print(f"         before: {i['before'][:90]!r}")
                if i["kind"] == KIND_SYSTEM_LABEL:
                    print(f"         after : {i['after'][:90]!r}")
        if args.apply:
            backup = Path(args.backup) if args.backup else (
                root / ".ops" / f"label_leak_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
            try:
                n = apply(db, items, backup=backup)
                entry["changed"] = n
                entry["backup"] = str(backup) if n else ""
                report["changed"] += n
                if not args.json:
                    print(f"  -> changed {n} row(s); backup: {backup if n else '(none)'}")
            except Exception as e:  # noqa: BLE001
                entry["error"] = repr(e)
                rc = 1
                if not args.json:
                    print(f"  !! apply failed: {e!r}")
        report["dbs"].append(entry)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
