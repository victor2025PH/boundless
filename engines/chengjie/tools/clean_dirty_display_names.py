# -*- coding: utf-8 -*-
"""存量脏 display_name 清洗（2026-08-14，「好友名单显示 Active now」事故收尾）。

背景：messenger-web worker 旧版「name = lines[0]」把侧栏状态/导航文案（"Active now"/
"Active 3m ago"/"消息请求"…）当昵称入库，且 upsert CASE 允许非空名覆盖 → 库里真名
被脏名冲掉。取名/入口两层已修（server.js DIRTY_NAME_RE + protocol_bridge.
sanitize_peer_name），本工具做第三步：把**已经落库**的脏名清掉。

判定口径＝与线上完全同一个 ``sanitize_peer_name``（单一事实源，绝不在这里另写一套
词表）。每条命中行的处置：

1. 通讯录有名（``protocol_contacts`` 的 name/notify_name，同样过清洗）→ 回填真名；
2. 无 → ``display_name = ''``（前端回落 chat_key，「诚实的数字 id」优于假名；
   下次 ingest 抓到真名会经正常链路补上——CASE 护栏允许真名覆盖空名）。

**默认 dry-run 只读**（``mode=ro`` URI，对活体生产库零写事务）；``--apply`` 才写，
写时短事务逐库提交。多实例数据根自动发现（scripts/_data_root 契约）。

用法::

    python tools/clean_dirty_display_names.py                 # 全实例 dry-run 摸底
    python tools/clean_dirty_display_names.py --platform messenger
    python tools/clean_dirty_display_names.py --apply         # 真清洗
    python tools/clean_dirty_display_names.py --db D:\\path\\inbox.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.integrations.protocol_bridge import sanitize_peer_name  # noqa: E402


def scan_db(db_path: Path, platform: str = "") -> List[Dict[str, Any]]:
    """只读扫描单库：返回 [{conversation_id, platform, chat_key, dirty_name, fix}]。

    fix = 通讯录回填名（清洗后仍有效才用）或 ''（清空）。纯读，不写。
    """
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    out: List[Dict[str, Any]] = []
    try:
        sql = ("SELECT conversation_id, COALESCE(platform,''), COALESCE(account_id,''),"
               "       COALESCE(chat_key,''), COALESCE(display_name,'')"
               " FROM conversations WHERE COALESCE(display_name,'') != ''")
        args: List[Any] = []
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        for cid, plat, acct, ck, name in con.execute(sql, args).fetchall():
            name = str(name)
            # 与 chat_key 同值的「裸 id 名」不算脏（前端本就按 chat_key 回落语义处理）
            if name == str(ck) or sanitize_peer_name(name):
                continue
            fix = ""
            try:
                row = con.execute(
                    "SELECT COALESCE(name,''), COALESCE(notify_name,'')"
                    " FROM protocol_contacts"
                    " WHERE platform=? AND account_id=? AND chat_key=?",
                    (plat, acct, ck),
                ).fetchone()
                if row:
                    fix = sanitize_peer_name(row[0]) or sanitize_peer_name(row[1])
            except sqlite3.Error:
                fix = ""  # 无 protocol_contacts 表等 → 按清空处置
            out.append({
                "conversation_id": str(cid), "platform": str(plat),
                "chat_key": str(ck), "dirty_name": name, "fix": fix,
            })
    finally:
        con.close()
    return out


def apply_fixes(db_path: Path, rows: List[Dict[str, Any]]) -> int:
    """按 scan_db 结果写库（短事务；仅当 display_name 仍是扫描时的脏值才更新——
    与线上并发写不打架：期间被真名覆盖的行自动跳过）。"""
    if not rows:
        return 0
    con = sqlite3.connect(str(db_path))
    n = 0
    try:
        with con:
            for r in rows:
                cur = con.execute(
                    "UPDATE conversations SET display_name=?"
                    " WHERE conversation_id=? AND display_name=?",
                    (r["fix"], r["conversation_id"], r["dirty_name"]),
                )
                n += cur.rowcount or 0
    finally:
        con.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="清洗 conversations 里的状态文案脏名")
    ap.add_argument("--db", default="", help="直接指定 inbox.db 路径（跳过数据根解析）")
    ap.add_argument("--data-root", default="", help="显式数据根（最高优先）")
    ap.add_argument("--platform", default="", help="只清指定平台（默认全平台）")
    ap.add_argument("--apply", action="store_true", help="真写库（默认 dry-run 只读）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    dbs: List[Path] = []
    if args.db:
        dbs = [Path(args.db)]
    else:
        for root in resolve_data_roots(args.data_root or None):
            cand = Path(root) / "config" / "inbox.db"
            if cand.is_file():
                dbs.append(cand)
    if not dbs:
        print("(未找到 inbox.db)")
        return 1

    total_hits, total_fixed = 0, 0
    report: List[Dict[str, Any]] = []
    for db in dbs:
        hits = scan_db(db, platform=str(args.platform or "").lower())
        total_hits += len(hits)
        fixed = apply_fixes(db, hits) if (args.apply and hits) else 0
        total_fixed += fixed
        report.append({"db": str(db), "hits": hits, "fixed": fixed})
        if not args.json:
            print(f"\n== {db}  命中 {len(hits)} 条"
                  + (f"，已修 {fixed} 条" if args.apply else "（dry-run 未写）"))
            for h in hits:
                act = f"回填 -> {h['fix']!r}" if h["fix"] else "清空 -> ''"
                print(f"  [{h['platform']}] {h['conversation_id']}"
                      f"  脏名={h['dirty_name']!r}  {act}")
    if args.json:
        print(json.dumps({"apply": bool(args.apply), "total_hits": total_hits,
                          "total_fixed": total_fixed, "dbs": report},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\n合计：命中 {total_hits} 条"
              + (f"，已修 {total_fixed} 条" if args.apply
                 else "。加 --apply 执行清洗。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
