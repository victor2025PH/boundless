#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_orphan_wa_conversations.py — 一次性修复：并孤儿 WhatsApp 会话（实施31 事故随修）。

背景
----
Node(Baileys) 侧 accountId 竞态会把入站消息按 ``conversation_id='whatsapp::<peer>'``
（account 段为空）落库，与正确会话 ``whatsapp:<account>:<peer>`` 分裂成两条。本脚本把
孤儿会话安全并入唯一候选目标，并同步所有随行表。

用法（集成者在主站停机窗口执行；脚本自带防呆）
----
    python scripts/merge_orphan_wa_conversations.py                 # dry-run（默认，只读）
    python scripts/merge_orphan_wa_conversations.py --commit        # 真正写入（单事务）
    python scripts/merge_orphan_wa_conversations.py --db <path> --platform whatsapp

行为
----
1. 找 conversation_id LIKE '<platform>::%' 的孤儿会话；peer = chat_key（空则取
   conversation_id 末段）。
2. 候选目标 = conversation_id LIKE '<platform>:%:<peer>' 且 account_id 非空的会话；
   **排除自聊会话（account_id == peer，即 Message-yourself）**——孤儿里是对端发来的
   会话消息，绝不属于自聊线程（不排除的话「peer 同时也是本方账号」场景会凑出两个
   候选而误跳过）。排除后恰好 1 个候选才合并；0 或多个 → 跳过并打印原因。
3. 合并（对每条孤儿消息）：
   - message_id 是确定性主键且**内嵌 conversation_id**（store._message_pk:
     '<conv>:<pmid>' 或 '<conv>:h:<hash16>'）——同步改写前缀成目标会话形式，
     否则将来同 pmid 重新入站（如 WA_BACKFILL 历史回填）不再幂等去重。
   - 目标已有同 message_id / 同 platform_msg_id /（hash 键）同 text+ts 的行 →
     视为重复（INSERT OR IGNORE 语义），跳过并连同其 message_analysis / FTS 行删除。
   - messages_fts：先查 sqlite_master 触发器——AFTER UPDATE OF text 不覆盖本场景，
     移动行手动 UPDATE fts；删除行有 messages_fts_ad 触发器则交给触发器，否则手动。
4. reply_drafts 孤儿行直接 DELETE（草稿可再生，draft_id 主键内嵌旧 conv id 不值得迁）。
5. conversation_meta：目标已有行 → 丢弃孤儿行；否则 UPDATE 过去。
6. conversations 目标行 last_ts/last_text 取两边 ts 最大的消息、unread 相加；
   最后 DELETE 孤儿 conversations 行。
7. 其余含 conversation_id 列的表（escalations/conversation_claims/…）只**盘点报告**
   不改写（超出本次修复授权范围；集成者按报告决定）。

防呆
----
- 默认 dry-run：以 SQLite 只读模式（mode=ro）打开，物理上写不进去。
- --commit：BEGIN IMMEDIATE 单事务，任何异常整体回滚；busy_timeout 5s 拿不到锁
  即报错退出（提示主站可能还在跑）。
- messages 的 UNIQUE 约束运行时 pragma 自查并打印；写入逐行捕 IntegrityError
  兜底跳过计数（当前库仅 message_id 主键唯一，改写前已按其语义显式查重）。
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

# Windows GBK 控制台坑：强制 utf-8 输出（emoji 昵称/中文消息预览不炸 UnicodeEncodeError）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_DB = r"D:\chengjie-instances\zhiliao\data\config\inbox.db"

# 本脚本显式处理的表；其余含 conversation_id 的表只盘点不动
HANDLED_TABLES = {
    "conversations", "messages", "message_analysis", "messages_fts",
    "reply_drafts", "conversation_meta",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ── 只读探查 ──────────────────────────────────────────────────────────────────

def inspect_unique_indexes(conn: sqlite3.Connection, table: str) -> List[str]:
    """打印并返回 table 上的唯一索引描述（含隐式 PK autoindex）。"""
    out: List[str] = []
    for row in conn.execute(f"PRAGMA index_list('{table}')"):
        # (seq, name, unique, origin, partial)
        name, unique, origin = row[1], row[2], row[3]
        if not unique:
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')")]
        out.append(f"{name} ({origin}) ON ({', '.join(cols)})")
    return out


def fts_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """messages_fts 是否存在 + 维护它的触发器有哪些。"""
    has_fts = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone())
    triggers = {
        r[0]: (r[1] or "")
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND sql LIKE '%messages_fts%'")
    }
    # AFTER DELETE 触发器在 → 删 messages 行时 fts 行自动清；conversation_id 的
    # UPDATE 没有任何触发器覆盖（messages_fts_au 只挂 UPDATE OF text）→ 恒手动同步。
    has_delete_trigger = any(
        "AFTER DELETE" in sql.upper() for sql in triggers.values())
    return {"has_fts": has_fts, "triggers": sorted(triggers),
            "has_delete_trigger": has_delete_trigger}


def table_names_with_conversation_id(conn: sqlite3.Connection) -> List[str]:
    out: List[str] = []
    for (t,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")]
        except sqlite3.Error:
            continue
        if "conversation_id" in cols:
            out.append(t)
    return out


# ── 合并计划 ──────────────────────────────────────────────────────────────────

def find_orphans(conn: sqlite3.Connection, platform: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM conversations WHERE conversation_id LIKE ? "
        "ORDER BY conversation_id",
        (f"{platform}::%",)))


def find_candidates(conn: sqlite3.Connection, platform: str, peer: str,
                    orphan_id: str) -> Tuple[List[sqlite3.Row], List[str]]:
    """候选目标 + 被排除项的说明（自聊/形态不符），供输出可见。"""
    rows = list(conn.execute(
        "SELECT * FROM conversations WHERE conversation_id LIKE ? "
        "AND account_id != '' AND conversation_id != ? ORDER BY conversation_id",
        (f"{platform}:%:{peer}", orphan_id)))
    kept: List[sqlite3.Row] = []
    excluded: List[str] = []
    for r in rows:
        cid, acct = r["conversation_id"], str(r["account_id"] or "")
        if cid != f"{platform}:{acct}:{peer}":
            excluded.append(f"{cid} (形态不符，LIKE 误匹配防呆)")
            continue
        if acct == peer:
            excluded.append(f"{cid} (自聊会话 account==peer，孤儿入站不属于自聊线程)")
            continue
        kept.append(r)
    return kept, excluded


def swap_message_id(old_id: str, orphan_id: str, target_id: str,
                    pmid: str) -> Optional[str]:
    """message_id 前缀替换：'<orphan>:<rest>' → '<target>:<rest>'。

    hash 段只依赖 text|ts（与 conversation 无关），前缀替换即为 store 会为目标
    会话生成的确定性主键。异常形态（不以孤儿 conv 打头）→ 有 pmid 用
    '<target>:<pmid>'，否则 None（跳过该行并告警，绝不瞎编 hash）。
    """
    prefix = orphan_id + ":"
    if old_id.startswith(prefix):
        return target_id + ":" + old_id[len(prefix):]
    if pmid:
        return f"{target_id}:{pmid}"
    return None


def plan_message_moves(conn: sqlite3.Connection, orphan_id: str,
                       target_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """对每条孤儿消息定性：move（改写 conv+message_id）/ dup（删）/ weird（跳过）。"""
    moves: List[Dict[str, Any]] = []
    dups: List[Dict[str, Any]] = []
    weird: List[Dict[str, Any]] = []
    for m in conn.execute(
            "SELECT message_id, platform_msg_id, direction, text, ts "
            "FROM messages WHERE conversation_id = ? ORDER BY ts",
            (orphan_id,)):
        old_id = m["message_id"]
        pmid = str(m["platform_msg_id"] or "").strip()
        new_id = swap_message_id(old_id, orphan_id, target_id, pmid)
        item = {"old_id": old_id, "new_id": new_id, "pmid": pmid,
                "direction": m["direction"], "ts": m["ts"],
                "preview": (m["text"] or "")[:24]}
        if new_id is None:
            weird.append(item)
            continue
        # INSERT OR IGNORE 语义查重（store 的唯一性 = 确定性 message_id 主键）：
        # ① 目标里已有同主键；② 同 pmid（另一路径落库、主键形式不同）；
        # ③ hash 键行：目标里已有同 text+ts（跨路径孪生）。
        dup_why = ""
        if conn.execute("SELECT 1 FROM messages WHERE message_id = ?",
                        (new_id,)).fetchone():
            dup_why = f"目标已有同主键 {new_id}"
        elif pmid and conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? "
                "AND platform_msg_id = ?", (target_id, pmid)).fetchone():
            dup_why = f"目标已有同 platform_msg_id={pmid}"
        elif not pmid and conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? "
                "AND text = ? AND ts = ?",
                (target_id, m["text"], m["ts"])).fetchone():
            dup_why = "目标已有同 text+ts 行（hash 键孪生）"
        if dup_why:
            item["why"] = dup_why
            dups.append(item)
        else:
            moves.append(item)
    return {"moves": moves, "dups": dups, "weird": weird}


def recalc_target_values(conn: sqlite3.Connection, orphan: sqlite3.Row,
                         target: sqlite3.Row) -> Dict[str, Any]:
    """合并后 conversations 目标行应有的 last_ts/last_text/unread（据两边消息预计算，
    dry-run 与 commit 同一口径）。"""
    row = conn.execute(
        "SELECT text, ts FROM messages WHERE conversation_id IN (?, ?) "
        "ORDER BY ts DESC LIMIT 1",
        (orphan["conversation_id"], target["conversation_id"])).fetchone()
    last_ts = float(row["ts"]) if row else max(
        float(target["last_ts"] or 0), float(orphan["last_ts"] or 0))
    last_text = (row["text"] if row else target["last_text"]) or ""
    unread = int(target["unread"] or 0) + int(orphan["unread"] or 0)
    return {"last_ts": last_ts, "last_text": last_text, "unread": unread}


# ── 执行 ─────────────────────────────────────────────────────────────────────

class Stats:
    def __init__(self) -> None:
        self.merged = 0
        self.skipped = 0
        self.msg_moved = 0
        self.msg_dup_deleted = 0
        self.msg_weird_kept = 0
        self.msg_integrity_skipped = 0
        self.ana_moved = 0
        self.ana_deleted = 0
        self.fts_updated = 0
        self.fts_deleted = 0
        self.drafts_deleted = 0
        self.meta_moved = 0
        self.meta_dropped = 0
        self.leftover_warnings = 0


def merge_one(conn: sqlite3.Connection, orphan: sqlite3.Row,
              target: sqlite3.Row, fts: Dict[str, Any], commit: bool,
              stats: Stats) -> None:
    o_id, t_id = orphan["conversation_id"], target["conversation_id"]
    tag = "执行" if commit else "计划"
    plan = plan_message_moves(conn, o_id, t_id)
    vals = recalc_target_values(conn, orphan, target)

    log(f"  [{tag}] 消息迁移 {len(plan['moves'])} 条 / 重复跳过删除 "
        f"{len(plan['dups'])} 条 / 异常形态保留 {len(plan['weird'])} 条")
    for it in plan["moves"]:
        log(f"    MOVE {it['old_id']}")
        log(f"      -> {it['new_id']}  ({it['direction']} ts={it['ts']} "
            f"text={it['preview']!r})")
    for it in plan["dups"]:
        log(f"    DUP-SKIP {it['old_id']}  ({it['why']}) -> 删除孤儿行及其 "
            f"analysis/FTS 行")
    for it in plan["weird"]:
        log(f"    WEIRD-KEEP {it['old_id']}  (主键不以孤儿会话打头且无 pmid，"
            f"不迁不删，需人工)")
        stats.msg_weird_kept += 1

    # 随行表计划量（dry-run 也报数）
    n_drafts = conn.execute(
        "SELECT COUNT(*) FROM reply_drafts WHERE conversation_id = ?",
        (o_id,)).fetchone()[0]
    meta_orphan = conn.execute(
        "SELECT 1 FROM conversation_meta WHERE conversation_id = ?",
        (o_id,)).fetchone()
    meta_target = conn.execute(
        "SELECT 1 FROM conversation_meta WHERE conversation_id = ?",
        (t_id,)).fetchone()
    meta_action = "无孤儿行"
    if meta_orphan:
        meta_action = ("目标已有 → 丢弃孤儿行" if meta_target
                       else "目标没有 → UPDATE 过去")
    log(f"  [{tag}] reply_drafts 删除 {n_drafts} 行；conversation_meta：{meta_action}")
    log(f"  [{tag}] conversations 目标行重算 last_ts={vals['last_ts']} "
        f"unread={vals['unread']} last_text={vals['last_text'][:24]!r}；"
        f"随后 DELETE 孤儿行 {o_id}")

    if not commit:
        # dry-run：summary 报计划量（与 commit 同口径，便于对照）
        stats.merged += 1
        stats.msg_moved += len(plan["moves"])
        stats.msg_dup_deleted += len(plan["dups"])
        stats.drafts_deleted += n_drafts
        if meta_orphan:
            if meta_target:
                stats.meta_dropped += 1
            else:
                stats.meta_moved += 1
        return

    # ── 真正写入（外层已 BEGIN IMMEDIATE，单事务） ──
    for it in plan["dups"]:
        cur = conn.execute("DELETE FROM message_analysis WHERE message_id = ?",
                           (it["old_id"],))
        stats.ana_deleted += max(cur.rowcount, 0)
        conn.execute("DELETE FROM messages WHERE message_id = ?",
                     (it["old_id"],))
        stats.msg_dup_deleted += 1
        if fts["has_fts"] and not fts["has_delete_trigger"]:
            conn.execute("DELETE FROM messages_fts WHERE message_id = ?",
                         (it["old_id"],))
            stats.fts_deleted += 1

    for it in plan["moves"]:
        try:
            conn.execute(
                "UPDATE messages SET conversation_id = ?, message_id = ? "
                "WHERE message_id = ?", (t_id, it["new_id"], it["old_id"]))
        except sqlite3.IntegrityError as e:
            # 查重已做；此处只是 pragma 自查之外的兜底（如未知唯一索引）
            log(f"    !! IntegrityError（{e}）→ 跳过 {it['old_id']}，孤儿行保留")
            stats.msg_integrity_skipped += 1
            continue
        stats.msg_moved += 1
        cur = conn.execute(
            "UPDATE message_analysis SET conversation_id = ?, message_id = ? "
            "WHERE message_id = ?", (t_id, it["new_id"], it["old_id"]))
        stats.ana_moved += cur.rowcount if cur.rowcount > 0 else 0
        if fts["has_fts"]:
            # UPDATE OF text 触发器不覆盖 conversation_id/message_id 改写 → 恒手动
            cur = conn.execute(
                "UPDATE messages_fts SET conversation_id = ?, message_id = ? "
                "WHERE message_id = ?", (t_id, it["new_id"], it["old_id"]))
            stats.fts_updated += cur.rowcount if cur.rowcount > 0 else 0

    # 兜底：残余 analysis 行（message_id 已不在 messages 里的陈旧行）连会话引用一起挪
    cur = conn.execute(
        "UPDATE message_analysis SET conversation_id = ? WHERE conversation_id = ?",
        (t_id, o_id))
    stats.ana_moved += cur.rowcount if cur.rowcount > 0 else 0

    cur = conn.execute("DELETE FROM reply_drafts WHERE conversation_id = ?",
                       (o_id,))
    stats.drafts_deleted += cur.rowcount if cur.rowcount > 0 else 0

    if meta_orphan:
        if meta_target:
            conn.execute("DELETE FROM conversation_meta WHERE conversation_id = ?",
                         (o_id,))
            stats.meta_dropped += 1
        else:
            conn.execute(
                "UPDATE conversation_meta SET conversation_id = ? "
                "WHERE conversation_id = ?", (t_id, o_id))
            stats.meta_moved += 1

    conn.execute(
        "UPDATE conversations SET last_ts = ?, last_text = ?, unread = ? "
        "WHERE conversation_id = ?",
        (vals["last_ts"], vals["last_text"], vals["unread"], t_id))
    conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (o_id,))
    stats.merged += 1


def report_leftovers(conn: sqlite3.Connection, orphan_id: str,
                     stats: Stats) -> None:
    """其余含 conversation_id 的表：只报数不改（本次授权范围外）。"""
    for t in table_names_with_conversation_id(conn):
        if t in HANDLED_TABLES:
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM '{t}' WHERE conversation_id = ?",
            (orphan_id,)).fetchone()[0]
        if n:
            log(f"  [盘点] {t} 仍有 {n} 行引用 {orphan_id}"
                f"（本脚本不动，集成者按需处置）")
            stats.leftover_warnings += 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="并孤儿 WhatsApp 会话（'platform::peer' → 'platform:account:peer'）")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"inbox.db 路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--platform", default="whatsapp", help="平台前缀（默认 whatsapp）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印计划不写（默认行为，写明白用）")
    ap.add_argument("--commit", action="store_true",
                    help="真正写入（单事务；不给此参数即 dry-run）")
    args = ap.parse_args()

    if args.dry_run and args.commit:
        log("错误：--dry-run 与 --commit 互斥")
        return 2
    commit = bool(args.commit)
    mode = "COMMIT（写入）" if commit else "DRY-RUN（只读，不写任何数据）"

    db_uri = "file:" + args.db.replace("\\", "/")
    try:
        if commit:
            conn = sqlite3.connect(db_uri, uri=True, timeout=5)
        else:
            conn = sqlite3.connect(db_uri + "?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        log(f"错误：打不开数据库 {args.db}：{e}")
        return 2
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    log(f"== merge_orphan_wa_conversations · 模式：{mode}")
    log(f"   db={args.db} platform={args.platform}")

    uniq = inspect_unique_indexes(conn, "messages")
    log(f"   messages 唯一索引自查：{'; '.join(uniq) if uniq else '（无）'}")
    log("   （message_id 主键内嵌 conversation_id → 迁移时同步改写前缀，"
        "查重按该确定性主键语义）")
    fts = fts_state(conn)
    log(f"   messages_fts={'存在' if fts['has_fts'] else '不存在'} "
        f"触发器={fts['triggers'] or '（无）'} "
        f"删除有触发器兜底={fts['has_delete_trigger']}")

    orphans = find_orphans(conn, args.platform)
    log(f"   孤儿会话（{args.platform}::%）：{len(orphans)} 条")
    if not orphans:
        log("== 无事可做，退出")
        conn.close()
        return 0

    stats = Stats()
    try:
        if commit:
            conn.execute("BEGIN IMMEDIATE")
        for orphan in orphans:
            o_id = orphan["conversation_id"]
            peer = str(orphan["chat_key"] or "").strip()
            if not peer:
                peer = o_id[len(args.platform) + 2:]
            log(f"\n-- 孤儿 {o_id} (display_name={orphan['display_name']!r} "
                f"unread={orphan['unread']} last_ts={orphan['last_ts']}) peer={peer}")
            if not peer:
                log("  跳过：解析不出 peer")
                stats.skipped += 1
                continue
            cands, excluded = find_candidates(conn, args.platform, peer, o_id)
            for e in excluded:
                log(f"  排除候选：{e}")
            if len(cands) != 1:
                ids = [c["conversation_id"] for c in cands]
                log(f"  跳过：候选目标数 = {len(cands)}（需恰好 1 个）{ids}")
                stats.skipped += 1
                continue
            target = cands[0]
            log(f"  目标 {target['conversation_id']} "
                f"(account_id={target['account_id']} unread={target['unread']} "
                f"last_ts={target['last_ts']})")
            merge_one(conn, orphan, target, fts, commit, stats)
            report_leftovers(conn, o_id, stats)
        if commit:
            conn.commit()
            log("\n== 事务已提交")
    except Exception as e:
        if commit:
            conn.rollback()
            log(f"\n== 异常，事务已整体回滚：{e!r}")
        else:
            log(f"\n== 异常（dry-run 只读，无需回滚）：{e!r}")
        conn.close()
        return 1

    log("\n== summary ==")
    log(f"   模式            : {mode}")
    log(f"   孤儿会话        : {len(orphans)}（合并 {stats.merged}，跳过 {stats.skipped}）")
    log(f"   消息            : 迁移 {stats.msg_moved}，重复删除 {stats.msg_dup_deleted}，"
        f"异常保留 {stats.msg_weird_kept}，IntegrityError 兜底跳过 {stats.msg_integrity_skipped}")
    log(f"   message_analysis: 迁移 {stats.ana_moved}，随重复删除 {stats.ana_deleted}")
    log(f"   messages_fts    : 手动改写 {stats.fts_updated}，手动删除 {stats.fts_deleted}"
        f"（删除{'走触发器' if fts['has_delete_trigger'] else '手动'}）")
    log(f"   reply_drafts    : 删除 {stats.drafts_deleted}")
    log(f"   conversation_meta: 迁移 {stats.meta_moved}，丢弃 {stats.meta_dropped}")
    log(f"   盘点告警（未动） : {stats.leftover_warnings} 处")
    if not commit:
        log("   （dry-run 未写任何数据；确认无误后加 --commit 执行）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
