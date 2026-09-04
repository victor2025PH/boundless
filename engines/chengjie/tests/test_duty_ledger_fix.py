# -*- coding: utf-8 -*-
"""J-5 E：台账校正工具 ``tools/duty_ledger_fix.py`` 门禁。

钉三条不变量：① 只动 verified 行（幂等，重跑零改动）；② 回滚**保留**
notify_ts/notify_msg_id（不许把真回访过的单变回「未回访」→ 重发回访）；
③ 每张改动落一条 ``ledger_fix`` 事件，改动可追溯。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import duty_ledger_fix as dlf  # noqa: E402


def _mk_db(tmp_path: Path) -> Path:
    db = tmp_path / "bug_intake.db"
    con = sqlite3.connect(str(db))
    con.execute(
        """CREATE TABLE bug_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT 'telegram',
            account_id TEXT NOT NULL DEFAULT '', reporter_id TEXT NOT NULL DEFAULT '',
            reporter_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'bug',
            severity TEXT NOT NULL DEFAULT 'P2', status TEXT NOT NULL DEFAULT 'new',
            dup_of INTEGER NOT NULL DEFAULT 0, report_count INTEGER NOT NULL DEFAULT 1,
            notify_ts REAL NOT NULL DEFAULT 0, notify_note TEXT NOT NULL DEFAULT '',
            fix_note TEXT NOT NULL DEFAULT '', notify_msg_id INTEGER NOT NULL DEFAULT 0)""")
    con.execute(
        """CREATE TABLE bug_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
            reporter_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""")
    now = time.time()
    rows = [
        # id, status, title, body, notify_ts, notify_msg_id
        (138, "verified", "连不上官网", "连不上官网\n[截图已收到]", now - 3600, 1025),
        (140, "verified", "克隆语音错误", "克隆语音错误", now - 3500, 1024),
        (142, "verified", "两道开关", "两道开关", now - 7000, 1012),
        (150, "fixed", "别的单", "别的单", 0, 0),
        (151, "verified", "真验证的单", "真验证的单", now - 100, 1030),
    ]
    for tid, st, title, body, nts, nmid in rows:
        con.execute(
            "INSERT INTO bug_tickets(id,created_ts,updated_ts,chat_id,title,body,"
            "status,notify_ts,notify_msg_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, now - 86400, now - 1000, "-100", title, body, st, nts, nmid))
    con.commit()
    con.close()
    return db


def _row(db: Path, tid: int) -> dict:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM bug_tickets WHERE id=?", (tid,)).fetchone())
    finally:
        con.close()


def _events(db: Path) -> list:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT kind,reporter_id,detail,chat_id FROM bug_events"
                           " ORDER BY id").fetchall()
    finally:
        con.close()


def test_parse_ids_tolerates_hash_and_dups():
    assert dlf.parse_ids("#138, 140,142,138,, 0") == [138, 140, 142]
    with pytest.raises(SystemExit):
        dlf.parse_ids("138,abc")


def test_plan_only_touches_verified_rows(tmp_path):
    db = _mk_db(tmp_path)
    ids = [138, 150, 999]
    plan = dlf.plan_rollback(dlf.load_rows(db, ids), ids)
    by = {p["ticket_id"]: p for p in plan}
    assert by[138]["action"] == "rollback" and by[138]["status_to"] == "fixed"
    assert by[138]["notify_ts"] > 0 and by[138]["notify_msg_id"] == 1025
    assert by[150]["action"] == "skip_not_verified"
    assert by[999]["action"] == "missing"
    # 顺序跟随请求顺序
    assert [p["ticket_id"] for p in plan] == ids
    txt = dlf.render_plan(plan, apply=False)
    assert "DRY-RUN" in txt and "#138" in txt and "notify_ts 保留" in txt
    assert "将改 1 单" in txt and "--apply" in txt


def test_dry_run_path_writes_nothing(tmp_path):
    db = _mk_db(tmp_path)
    before = {t: _row(db, t) for t in (138, 140, 142)}
    plan = dlf.plan_rollback(dlf.load_rows(db, [138, 140, 142]), [138, 140, 142])
    assert sum(1 for p in plan if p["action"] == "rollback") == 3
    # 只做 plan 不 apply → 库零变化、零事件
    assert {t: _row(db, t) for t in (138, 140, 142)} == before
    assert _events(db) == []


def test_apply_rolls_back_keeps_notify_and_logs_event(tmp_path):
    db = _mk_db(tmp_path)
    ids = [138, 140, 142]
    plan = dlf.plan_rollback(dlf.load_rows(db, ids), ids)
    fixed_ts = 1_800_000_000.0
    assert dlf.apply_plan(db, plan, now=fixed_ts) == 3
    for tid, nmid in ((138, 1025), (140, 1024), (142, 1012)):
        r = _row(db, tid)
        assert r["status"] == "fixed"
        assert r["notify_ts"] > 0, "回滚不得抹掉 notify_ts（否则会重发回访）"
        assert r["notify_msg_id"] == nmid
        assert r["updated_ts"] == fixed_ts
        assert r["body"].splitlines()[-1] == dlf.DEFAULT_NOTE
        assert r["fix_note"] == ""  # 不碰修复说明
    # 未点名 / 非 verified 的不动
    assert _row(db, 150)["status"] == "fixed"
    assert _row(db, 151)["status"] == "verified"
    evs = _events(db)
    assert len(evs) == 3
    assert all(k == dlf.EVENT_KIND and cid == "-100" for k, _, _, cid in evs)
    assert {d.split(" ")[0] for _, _, d, _ in evs} == {"#138", "#140", "#142"}
    assert all("verified->fixed" in d and dlf.DEFAULT_NOTE in d for _, _, d, _ in evs)


def test_apply_is_idempotent_on_rerun(tmp_path):
    db = _mk_db(tmp_path)
    ids = [138, 140, 142]
    plan = dlf.plan_rollback(dlf.load_rows(db, ids), ids)
    assert dlf.apply_plan(db, plan) == 3
    plan2 = dlf.plan_rollback(dlf.load_rows(db, ids), ids)
    assert all(p["action"] == "skip_not_verified" for p in plan2)
    assert dlf.apply_plan(db, plan2) == 0
    # 即便拿旧 plan 重放，WHERE status='verified' 第二道守卫也拦住
    assert dlf.apply_plan(db, plan) == 0
    assert len(_events(db)) == 3
    assert _row(db, 138)["body"].count(dlf.DEFAULT_NOTE) == 1


def test_custom_note_is_clamped_and_used(tmp_path):
    db = _mk_db(tmp_path)
    note = "[值守] 自定义原因 " + "x" * 500
    plan = dlf.plan_rollback(dlf.load_rows(db, [140]), [140], note=note)
    assert len(plan[0]["note"]) == 300
    dlf.apply_plan(db, plan)
    assert _row(db, 140)["body"].endswith(plan[0]["note"])


def test_count_fixed_unnotified(tmp_path):
    db = _mk_db(tmp_path)
    assert dlf.count_fixed_unnotified(db) == 1  # 只有 #150
    plan = dlf.plan_rollback(dlf.load_rows(db, [138]), [138])
    dlf.apply_plan(db, plan)
    # 回滚保留 notify_ts → 不进「fixed 未回访」名单
    assert dlf.count_fixed_unnotified(db) == 1
