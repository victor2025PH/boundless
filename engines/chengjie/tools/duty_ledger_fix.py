# -*- coding: utf-8 -*-
"""工单台账校正（J-5 E，决策 D6，2026-09-05）：默认 dry-run，``--apply`` 才写。

首个用例＝**假 verified 回滚**：0905 00:14–00:29 三张已修单（#138/#140/#142）
被 backfill 二次观察误触 verify_yes 翻成 verified（详见
``docs/报障对账_钧_花无缺_20260905.md`` §2 与 J-5 A/B 修复）。回滚口径：

- ``status`` verified → fixed；**保留 notify_ts / notify_msg_id**（回访确实发过，
  不能让 61 单「fixed 未回访」的名单里多出三张假的，也不许重发回访）；
- body 追加一行 note ``[值守 0905] 假 verified 回滚：backfill 二次观察误触``；
- bug_events 落一条 ``ledger_fix`` 事件（谁、何时、把哪张单从什么改成什么），
  台账改动可追溯——值守面板/report 都读 bug_events。

用法::

    python tools/duty_ledger_fix.py --rollback-verified 138,140,142      # dry-run
    python tools/duty_ledger_fix.py --rollback-verified 138,140,142 --apply
    python tools/duty_ledger_fix.py --rollback-verified 138 --note "自定义 note" --apply
    python tools/duty_ledger_fix.py --count-fixed-unnotified               # 只读计数
    python tools/duty_ledger_fix.py --mark-fixed-file tmp/plan.json        # dry-run
    python tools/duty_ledger_fix.py --mark-fixed-file tmp/plan.json --apply

第二个用例＝**批量标 fixed 但不触发逐单回访**（K-5 ④段，老板 2026-09-05 拍 A 案）：
``/api/admin/bug-intake/{id}/status`` 标 fixed 会当场逐单 @报障人，一次标几十
单等于轰炸；本模式与该路由写同样三列 ``status / fix_note / updated_ts``，
``notify_ts`` 保持 0，让这些单进「fixed 未回访」池，由 ``duty_notify_summary``
的两份汇总回访一次盖掉。计划文件＝JSON 对象 ``{"37": "fix_note…", …}`` 或列表
``[{"id": 37, "fix_note": "…"}, …]``；只动 ``status ∈ {new, confirmed}`` 的行
（已 fixed / closed / verified 的一律跳过，幂等）；每单落一条 ``ledger_fix`` 事件。

护栏：回滚只动 ``status='verified'`` 的行（已被人改过的不重改，幂等）；直连 SQLite
短事务 ``timeout=10``（活库 WAL，引擎进程持连接）；**不 VACUUM / 不改 schema**；
不经 ``/api/admin/bug-intake/{id}/status`` 路由——那条路 fixed 会自动触发回访。
纯函数 ``plan_rollback`` / ``plan_mark_fixed`` 有门禁 ``tests/test_duty_ledger_fix.py``。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

DEFAULT_NOTE = "[值守 0905] 假 verified 回滚：backfill 二次观察误触"
EVENT_KIND = "ledger_fix"


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


def parse_ids(raw: str) -> List[int]:
    out: List[int] = []
    for x in str(raw or "").replace("#", "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            v = int(x)
        except ValueError:
            raise SystemExit(f"[err] 工单号不是整数：{x!r}")
        if v > 0 and v not in out:
            out.append(v)
    return out


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def plan_rollback(rows: Sequence[Dict[str, Any]], want_ids: Sequence[int],
                  note: str = DEFAULT_NOTE) -> List[Dict[str, Any]]:
    """每张目标单一条计划项：``action`` = rollback | skip_not_verified | missing。

    只有 ``status == 'verified'`` 的行给 rollback；其它状态一律 skip（幂等，
    重跑不会把人工已改的单再翻一次）；库里没有的 id 标 missing。
    """
    by_id = {int(r.get("id") or 0): r for r in rows or []}
    plan: List[Dict[str, Any]] = []
    for tid in want_ids:
        r = by_id.get(int(tid))
        if r is None:
            plan.append({"ticket_id": int(tid), "action": "missing"})
            continue
        item = {
            "ticket_id": int(tid),
            "title": str(r.get("title") or "").splitlines()[0][:60],
            "status_from": str(r.get("status") or ""),
            "notify_ts": float(r.get("notify_ts") or 0),
            "notify_msg_id": int(r.get("notify_msg_id") or 0),
        }
        if item["status_from"] != "verified":
            item["action"] = "skip_not_verified"
        else:
            item.update({"action": "rollback", "status_to": "fixed",
                         "note": str(note or DEFAULT_NOTE).strip()[:300]})
        plan.append(item)
    return plan


def render_plan(plan: Sequence[Dict[str, Any]], apply: bool) -> str:
    head = "[APPLY]" if apply else "[DRY-RUN]"
    lines = [f"{head} 假 verified 回滚计划（{len(plan)} 项）："]
    for p in plan:
        tid = p["ticket_id"]
        if p["action"] == "missing":
            lines.append(f"  #{tid}  ⛔ 库内无此单")
        elif p["action"] == "skip_not_verified":
            lines.append(f"  #{tid}  ⏭ 跳过：status={p['status_from']!r} 非 verified"
                         f"（{p.get('title', '')}）")
        else:
            nts = p["notify_ts"]
            nts_s = (time.strftime("%m-%d %H:%M", time.localtime(nts))
                     if nts else "未回访")
            lines.append(
                f"  #{tid}  {p['status_from']} → {p['status_to']}  "
                f"notify_ts 保留（{nts_s}, msg={p['notify_msg_id']}）  "
                f"note+=「{p['note']}」  （{p.get('title', '')}）")
    n_do = sum(1 for p in plan if p["action"] == "rollback")
    lines.append(f"  将改 {n_do} 单" + ("" if apply else "——加 --apply 执行"))
    return "\n".join(lines)


# ── DB ───────────────────────────────────────────────────────────────────────

def load_rows(db_path: Path, ids: Sequence[int]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        q = ",".join("?" for _ in ids)
        rows = con.execute(
            f"SELECT id,status,title,notify_ts,notify_msg_id FROM bug_tickets"
            f" WHERE id IN ({q})", [int(i) for i in ids]).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def apply_plan(db_path: Path, plan: Sequence[Dict[str, Any]],
               actor: str = "duty_ledger_fix",
               now: Optional[float] = None) -> int:
    """执行 rollback 项：短事务，逐单 ``WHERE status='verified'`` 再守一次。"""
    ts = float(now if now is not None else time.time())
    todo = [p for p in plan if p["action"] == "rollback"]
    if not todo:
        return 0
    con = sqlite3.connect(str(db_path), timeout=10)
    done = 0
    try:
        for p in todo:
            tid = int(p["ticket_id"])
            cur = con.execute(
                "UPDATE bug_tickets SET status=?, updated_ts=?,"
                " body=substr(body || char(10) || ?, 1, 4000)"
                " WHERE id=? AND status='verified'",
                (p["status_to"], ts, p["note"], tid))
            if cur.rowcount != 1:
                continue
            con.execute(
                "INSERT INTO bug_events(ts, chat_id, kind, reporter_id, detail)"
                " SELECT ?, chat_id, ?, ?, ? FROM bug_tickets WHERE id=?",
                (ts, EVENT_KIND, actor,
                 f"#{tid} {p['status_from']}->{p['status_to']} | {p['note']}",
                 tid))
            done += 1
        con.commit()
    finally:
        con.close()
    return done


# ── 批量标 fixed（不触发回访）────────────────────────────────────────────────

MARK_FIXED_FROM = ("new", "confirmed")
FIX_NOTE_MAX = 300  # 与 bug_intake.set_ticket_status 的截断一致


def load_mark_plan_file(path: Path) -> Dict[int, str]:
    """计划文件 → {ticket_id: fix_note}。对象形态键是单号字串；列表形态每项
    ``{"id", "fix_note"}``。空 fix_note 直接拒绝——标 fixed 没有说明就是给回访
    留白。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items: List[Any]
    if isinstance(raw, dict):
        items = [{"id": k, "fix_note": v} for k, v in raw.items()]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        raise SystemExit("[err] 计划文件须是 JSON 对象或列表")
    out: Dict[int, str] = {}
    for it in items:
        try:
            tid = int(str((it or {}).get("id")).replace("#", "").strip())
        except (TypeError, ValueError, AttributeError):
            raise SystemExit(f"[err] 计划项缺合法 id：{it!r}")
        note = str((it or {}).get("fix_note") or "").strip()
        if not note:
            raise SystemExit(f"[err] #{tid} fix_note 为空")
        out[tid] = note
    return out


def plan_mark_fixed(rows: Sequence[Dict[str, Any]],
                    notes: Dict[int, str]) -> List[Dict[str, Any]]:
    """每张目标单一条计划项：``action`` = mark | skip_status | missing。

    只有 ``status ∈ MARK_FIXED_FROM`` 的行给 mark；fixed / closed / verified
    一律 skip（重跑不会覆盖别人已写的 fix_note，也不会把 verified 翻回去）。
    """
    by_id = {int(r.get("id") or 0): r for r in rows or []}
    plan: List[Dict[str, Any]] = []
    for tid in sorted(notes):
        r = by_id.get(int(tid))
        if r is None:
            plan.append({"ticket_id": int(tid), "action": "missing"})
            continue
        item = {
            "ticket_id": int(tid),
            "title": str(r.get("title") or "").splitlines()[0][:60],
            "reporter": str(r.get("reporter_name") or ""),
            "status_from": str(r.get("status") or ""),
            "notify_ts": float(r.get("notify_ts") or 0),
        }
        if item["status_from"] not in MARK_FIXED_FROM:
            item["action"] = "skip_status"
        else:
            item.update({"action": "mark", "status_to": "fixed",
                         "fix_note": str(notes[tid]).strip()[:FIX_NOTE_MAX]})
        plan.append(item)
    return plan


def render_mark_plan(plan: Sequence[Dict[str, Any]], apply: bool) -> str:
    head = "[APPLY]" if apply else "[DRY-RUN]"
    lines = [f"{head} 批量标 fixed（不触发回访）计划（{len(plan)} 项）："]
    for p in plan:
        tid = p["ticket_id"]
        if p["action"] == "missing":
            lines.append(f"  #{tid}  ⛔ 库内无此单")
        elif p["action"] == "skip_status":
            lines.append(f"  #{tid}  ⏭ 跳过：status={p['status_from']!r} 不在 "
                         f"{'/'.join(MARK_FIXED_FROM)}（{p.get('title', '')}）")
        else:
            lines.append(
                f"  #{tid}  {p['status_from']} → fixed  [{p.get('reporter', '')}] "
                f"{p.get('title', '')}\n        fix_note: {p['fix_note']}")
    n_do = sum(1 for p in plan if p["action"] == "mark")
    lines.append(f"  将改 {n_do} 单，notify_ts 保持 0（进汇总回访池）"
                 + ("" if apply else "——加 --apply 执行"))
    return "\n".join(lines)


def load_rows_full(db_path: Path, ids: Sequence[int]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        q = ",".join("?" for _ in ids)
        rows = con.execute(
            f"SELECT id,status,title,reporter_name,notify_ts,fix_note FROM bug_tickets"
            f" WHERE id IN ({q})", [int(i) for i in ids]).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def apply_mark_fixed(db_path: Path, plan: Sequence[Dict[str, Any]],
                     actor: str = "duty_ledger_fix",
                     now: Optional[float] = None) -> int:
    """执行 mark 项：与 ``set_ticket_status`` 同三列，``WHERE status IN (…)`` 再守一次；
    不碰 notify_ts / notify_msg_id。"""
    ts = float(now if now is not None else time.time())
    todo = [p for p in plan if p["action"] == "mark"]
    if not todo:
        return 0
    marks = ",".join("?" for _ in MARK_FIXED_FROM)
    con = sqlite3.connect(str(db_path), timeout=10)
    done = 0
    try:
        for p in todo:
            tid = int(p["ticket_id"])
            cur = con.execute(
                "UPDATE bug_tickets SET status='fixed', fix_note=?, updated_ts=?"
                f" WHERE id=? AND status IN ({marks})",
                (p["fix_note"], ts, tid, *MARK_FIXED_FROM))
            if cur.rowcount != 1:
                continue
            con.execute(
                "INSERT INTO bug_events(ts, chat_id, kind, reporter_id, detail)"
                " SELECT ?, chat_id, ?, ?, ? FROM bug_tickets WHERE id=?",
                (ts, EVENT_KIND, actor,
                 f"#{tid} {p['status_from']}->fixed (no-notify, summary pending)",
                 tid))
            done += 1
        con.commit()
    finally:
        con.close()
    return done


def count_fixed_unnotified(db_path: Path) -> int:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return int(con.execute(
            "SELECT count(*) FROM bug_tickets WHERE status='fixed'"
            " AND notify_ts=0").fetchone()[0])
    finally:
        con.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="工单台账校正（默认 dry-run）")
    ap.add_argument("--rollback-verified", default="",
                    help="逗号分隔工单号：verified → fixed（保留 notify_ts）")
    ap.add_argument("--note", default=DEFAULT_NOTE)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--count-fixed-unnotified", action="store_true",
                    help="只读：status='fixed' AND notify_ts=0 的单数")
    ap.add_argument("--mark-fixed-file", default="",
                    help="JSON 计划文件：批量 new/confirmed → fixed + fix_note，"
                         "不触发逐单回访（notify_ts 保持 0）")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    db = Path(data_root) / "config" / "bug_intake.db"
    if not db.is_file():
        _out(f"[skip] 无工单台账（{db}）")
        return 0
    _out(f"[db] {db}")

    if args.count_fixed_unnotified:
        _out(f"fixed 且未回访（notify_ts=0）：{count_fixed_unnotified(db)} 单")

    if args.mark_fixed_file:
        notes = load_mark_plan_file(Path(args.mark_fixed_file))
        mplan = plan_mark_fixed(load_rows_full(db, list(notes)), notes)
        if args.json:
            _out(json.dumps(mplan, ensure_ascii=False, indent=1))
        else:
            _out(render_mark_plan(mplan, apply=args.apply))
        if args.apply:
            n = apply_mark_fixed(db, mplan)
            _out(f"[apply] 已标 fixed {n} 单（事件 kind={EVENT_KIND}，未发回访）")
            _out(f"fixed 且未回访（notify_ts=0）：{count_fixed_unnotified(db)} 单")

    ids = parse_ids(args.rollback_verified)
    if not ids:
        return 0
    plan = plan_rollback(load_rows(db, ids), ids, note=args.note)
    if args.json:
        _out(json.dumps(plan, ensure_ascii=False, indent=1))
    else:
        _out(render_plan(plan, apply=args.apply))
    if not args.apply:
        return 0
    n = apply_plan(db, plan)
    _out(f"[apply] 已回滚 {n} 单（事件 kind={EVENT_KIND}）")
    after = plan_rollback(load_rows(db, ids), ids, note=args.note)
    _out("[verify] " + " ".join(
        f"#{p['ticket_id']}={p.get('status_from', '?')}" for p in after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
