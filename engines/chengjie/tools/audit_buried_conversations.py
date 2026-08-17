# -*- coding: utf-8 -*-
"""被埋掉的会话体检（只读）—— P0-198，2026-08-04。

事故：一条 33 条消息的**活跃**会话在坐席聊天途中从工作台彻底消失。根因＝归档在实现上
是永久的（所有默认视图过滤 ``archived=1``，而入站链路从不复位该标记），于是客户之后
无论说多少句话，会话都不回来、也不产生任何提示。修复已上线（``_unarchive_on_inbound``
入站自动复活），但它**够不着存量**：真实归档时刻不可追溯（``archived_at`` 是本次才加的
列，存量只能回填「升级时刻」），存量被埋的会话里客户是在升级之前开口的。

本工具用一条与时间戳无关的信号把历史损失捞出来：**归档着、却有未读入站**——未读只可能
由入站消息产生，「没人读过」本身就是「没人看得见」的直接证据。

只读、零副作用：**刻意不自动解档**。存量行缺可信归档时刻，批量唤回等于拿一个猜测覆盖
运营的明示决定。这里只负责让损失可数、可核对；处置由人在工作台「更多 → 归档」里做，
或用 ``--print-curl`` 出的命令逐条解档。

用法::

    python tools/audit_buried_conversations.py                 # 本机所有活跃实例
    python tools/audit_buried_conversations.py --data-root D:\\x\\data
    python tools/audit_buried_conversations.py --db C:\\from198\\inbox.db
    python tools/audit_buried_conversations.py --json

198（打包桌面版，无 python 环境）怎么查：把它的 ``inbox.db`` 拷回来 ``--db`` 指过去；
路径见 ``%APPDATA%\\telegram-ai-desktop`` 下的数据根。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

# 与 InboxStore.list_buried_archived 同一判据。刻意在这里重写一遍 SQL 而不 import store：
# 本工具要能对**任意拷回来的 db 文件**只读运行（含别的机器、别的版本），而 InboxStore
# 构造会跑 migration＝对别人的库写事务，这是审计工具绝不能做的事。
_SQL = """
SELECT c.conversation_id, c.platform, c.account_id, c.chat_key, c.display_name,
       c.unread, c.last_ts, c.last_text, cm.archived_at, cm.auto_archived_at
FROM conversations c
JOIN conversation_meta cm ON cm.conversation_id = c.conversation_id
WHERE cm.archived = 1 AND c.unread >= ?
ORDER BY c.unread DESC, c.last_ts DESC
LIMIT ?
"""


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "-"
    except Exception:
        return "-"


def scan_db(db_path: Path, *, min_unread: int = 1, limit: int = 50) -> list[dict]:
    """只读扫一个 inbox.db。缺列（旧版库没有 archived_at）自动降级，不抛。"""
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(_SQL, (min_unread, limit)).fetchall()
        except sqlite3.OperationalError:
            # 旧版库无 archived_at 列 → 去掉那两个字段重试（体检不该被 schema 版本挡住）
            sql = _SQL.replace(", cm.archived_at, cm.auto_archived_at", "")
            rows = conn.execute(sql, (min_unread, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _candidate_dbs(data_root: Path) -> list[Path]:
    """inbox.db 在数据根下的可能位置（实测生产落 config/ 下；容错根下）。"""
    out = []
    for rel in ("config/inbox.db", "inbox.db"):
        p = data_root / rel
        if p.is_file():
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="被埋掉的会话体检（归档 + 有未读，只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现活跃实例）")
    ap.add_argument("--db", default="", help="直接指定 inbox.db（优先于 --data-root）")
    ap.add_argument("--min-unread", type=int, default=1, help="未读数下限（默认 1）")
    ap.add_argument("--limit", type=int, default=50, help="每库最多列出条数")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    if args.db:
        dbs = [Path(args.db)]
    else:
        dbs = []
        for root in resolve_data_roots(args.data_root):
            dbs.extend(_candidate_dbs(root))

    if not dbs:
        print("未找到任何 inbox.db（用 --db 显式指定，或检查实例数据根）")
        return 0

    result = []
    for db in dbs:
        try:
            rows = scan_db(db, min_unread=args.min_unread, limit=args.limit)
        except Exception as exc:  # 一个库坏掉不该让整轮体检失败
            result.append({"db": str(db), "error": str(exc), "rows": []})
            continue
        result.append({"db": str(db), "rows": rows})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    total = 0
    for item in result:
        print("=" * 72)
        print("DB:", item["db"])
        if item.get("error"):
            print("  读取失败:", item["error"])
            continue
        rows = item["rows"]
        if not rows:
            print("  ✓ 没有「归档着还有未读」的会话")
            continue
        total += len(rows)
        print("  ⚠ %d 条会话归档着、但有未读入站（客户在等，工作台看不见）：" % len(rows))
        for r in rows:
            aat = r.get("archived_at")
            auto = r.get("auto_archived_at")
            how = "?" if aat is None else ("自动" if (auto or 0) > 0 else "人工")
            print("   - %-38s 未读=%-4s 最后活动=%s 归档=%s(%s)" % (
                r.get("conversation_id"), r.get("unread"),
                _fmt_ts(r.get("last_ts") or 0), how, _fmt_ts(aat or 0),
            ))
            name = (r.get("display_name") or "").strip()
            preview = (r.get("last_text") or "").strip().replace("\n", " ")[:48]
            if name or preview:
                print("       %s | %s" % (name or "-", preview or "-"))
    print("=" * 72)
    if total:
        print("合计 %d 条待人工核对。处置：工作台「更多 → 归档」逐条确认，"
              "确实还在服务的点「取消归档」。" % total)
        # 非零退出便于挂进巡检/告警（≠ 程序出错，是「有事要人看」）
        return 2
    print("全部干净。")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
