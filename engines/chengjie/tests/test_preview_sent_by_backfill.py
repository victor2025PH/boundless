# -*- coding: utf-8 -*-
"""sent_by 历史回填只读预览（二批②）。"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "preview_sent_by_backfill.py"


def _load():
    spec = importlib.util.spec_from_file_location("preview_sent_by_backfill", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk(tmp_path: Path):
    db = tmp_path / "unified_inbox.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, platform TEXT, account_id TEXT,
        chat_key TEXT, display_name TEXT DEFAULT '', last_text TEXT DEFAULT '');
    CREATE TABLE messages (message_id TEXT PRIMARY KEY, conversation_id TEXT, direction TEXT,
        text TEXT DEFAULT '', ts REAL, ingested_at REAL, sent_by TEXT NOT NULL DEFAULT '');
    CREATE TABLE agent_sends (id INTEGER PRIMARY KEY, conversation_id TEXT, agent_id TEXT, ts REAL,
        claimed_mid TEXT NOT NULL DEFAULT '');
    CREATE TABLE reply_drafts (draft_id TEXT PRIMARY KEY, conversation_id TEXT, final_text TEXT DEFAULT '',
        decided_by TEXT DEFAULT '', sent_at REAL DEFAULT 0);
    CREATE TABLE outreach_log (id INTEGER PRIMARY KEY, conversation_id TEXT, status TEXT, note TEXT DEFAULT '', ts REAL);
    """)
    c.executemany("INSERT INTO conversations(conversation_id,platform,account_id,chat_key) VALUES(?,?,?,?)", [
        ("telegram:7000000001:1", "telegram", "7000000001", "1"),
        ("telegram:111:2", "telegram", "111", "2"),
    ])
    rows = [
        ("s1", "telegram:7000000001:1", "out", 1000.0, ""),   # R2 script
        ("a1", "telegram:111:2", "out", 2000.0, ""),          # R1 claimed
        ("d1", "telegram:111:2", "out", 3000.0, ""),          # R3 draft auto → ai
        ("d2", "telegram:111:2", "out", 4000.0, ""),          # R3 draft by agent → agent
        ("o1", "telegram:111:2", "out", 5000.0, ""),          # R5 outreach
        ("w1", "telegram:111:2", "out", 6000.0, ""),          # R6 agent window
        ("p1", "telegram:111:2", "out", 7000.0, ""),          # R0 phone
        ("r1", "telegram:111:2", "out", 8000.0, ""),          # R4 autoreply audit
        ("x1", "telegram:111:2", "out", 9000.0, "ai"),        # already attributed
        ("i1", "telegram:111:2", "in", 9100.0, ""),           # inbound ignored
    ]
    c.executemany("INSERT INTO messages(message_id,conversation_id,direction,ts,ingested_at,sent_by,text) "
                  "VALUES(?,?,?,?,?,?, 'SECRET-BODY')", [(m, cid, d, ts, ts, sb) for m, cid, d, ts, sb in rows])
    c.execute("INSERT INTO agent_sends(conversation_id,agent_id,ts,claimed_mid) VALUES('telegram:111:2','u1',2001,'a1')")
    c.execute("INSERT INTO agent_sends(conversation_id,agent_id,ts,claimed_mid) VALUES('telegram:111:2','u1',6030,'')")
    c.execute("INSERT INTO reply_drafts VALUES('dr1','telegram:111:2','x','autosend',3010)")
    c.execute("INSERT INTO reply_drafts VALUES('dr2','telegram:111:2','x','alice',4005)")
    c.execute("INSERT INTO outreach_log(conversation_id,status,ts) VALUES('telegram:111:2','sent',5002)")
    c.commit(); c.close()
    adb = tmp_path / "autoreply_audit.db"
    a = sqlite3.connect(adb)
    a.execute("CREATE TABLE autoreply_audit (id INTEGER PRIMARY KEY, ts REAL, conversation_id TEXT, "
              "inbound TEXT, reply TEXT, decision TEXT, reason TEXT)")
    a.execute("INSERT INTO autoreply_audit(ts,conversation_id,decision,inbound,reply) VALUES(8003,'telegram:111:2','send','B','B')")
    a.execute("INSERT INTO autoreply_audit(ts,conversation_id,decision) VALUES(7000,'telegram:111:2','hold')")
    a.commit(); a.close()
    return db, adb


def _h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_preview_counts_each_rule_and_never_writes(tmp_path):
    mod = _load()
    db, adb = _mk(tmp_path)
    before = (_h(db), _h(adb))
    conn, aconn = mod.open_ro(str(db)), mod.open_ro(str(adb))
    r = mod.preview(conn, audit_conn=aconn, window=60, script_accounts=["7000000001"])
    conn.close(); aconn.close()
    assert (_h(db), _h(adb)) == before
    br = {k: v["count"] for k, v in r["by_rule"].items()}
    assert br["R1_agent_claimed"] == 1 and br["R2_script_account"] == 1
    assert br["R3_draft_sent_ai"] == 1 and br["R3_draft_sent_agent"] == 1
    assert br["R4_autoreply_audit"] == 1 and br["R5_outreach_sent"] == 1
    assert br["R6_agent_window"] == 1 and br["R0_no_evidence"] == 1
    assert r["empty_out"] == 8 and r["inferable"] == 7 and r["total_out"] == 9
    assert r["by_value"] == {"agent": 3, "script": 1, "ai": 3, "phone": 1}
    assert "SECRET-BODY" not in repr(r)


def test_readonly_connection_rejects_writes(tmp_path):
    mod = _load()
    db, _ = _mk(tmp_path)
    conn = mod.open_ro(str(db))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE messages SET sent_by='ai'")
    conn.close()


@pytest.mark.parametrize("sql", [
    "SELECT text FROM messages",
    "SELECT m.original_text FROM messages m",
    "SELECT reply FROM autoreply_audit",
    "UPDATE messages SET sent_by='ai'",
    "DELETE FROM messages",
    "PRAGMA journal_mode=WAL",
])
def test_guard_blocks_body_columns_and_writes(sql):
    mod = _load()
    with pytest.raises(ValueError):
        mod._guard_sql(sql)


def test_guard_allows_metadata_and_literal_text():
    mod = _load()
    mod._guard_sql("SELECT message_id, conversation_id, ts FROM messages WHERE direction='out' AND sent_by=''")
    mod._guard_sql("SELECT conversation_id FROM outreach_log WHERE status='text'")


def test_tool_has_no_apply_or_update_path():
    src = TOOL.read_text(encoding="utf-8")
    assert "--apply" not in src.replace("没有** ``--apply``", "").replace("无 --apply", "")
    assert "UPDATE " not in src.replace("UPDATE 语句", "")
    assert "mode=ro" in src and "query_only" in src


def test_main_cli_with_instance_root(tmp_path, capsys):
    mod = _load()
    root = tmp_path / "inst"
    (root / "data" / "config").mkdir(parents=True)
    db, adb = _mk(root / "data" / "config")
    assert mod.main(["--instance-root", str(root), "--json", "--window", "60"]) == 0
    out = capsys.readouterr().out
    assert "R4_autoreply_audit" in out and "SECRET-BODY" not in out
