# -*- coding: utf-8 -*-
"""代运营多客户工作区 · 第一段（DESIGN_agency_workspace，2026-10-08 蛋博士批准）。

第一段只加 workspace_id 字段与默认工作区，现有行为不变。本文件验证：
- 老库升级：用「关掉第一段迁移」的同一份代码建库 = 升级前的老库，灌数据、记下各接口输出；
  再用正式代码打开（触发迁移），各接口输出逐字一致；新列存在且旧行 = 'default'；
- 重复迁移：连开多次不报错、输出不变、InboxStore.migration_errors == 0；
- 空库：直接建出新 schema，与老库升级后的 schema 一致；
- 默认工作区行存在，但 list_workspaces 输出不变（未编辑过的种子行不列出）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.inbox import store as inbox_store_mod
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.integrations import account_registry as reg_mod
from src.integrations.account_registry import AccountRegistry
from src.licensing.quota_store import LicenseQuotaStore
from src.tenancy import workspace as ws
from src.utils import kb_store as kb_mod
from src.utils.kb_store import KnowledgeBaseStore
from src.utils.web_user_store import WebUserStore

T0 = 1_760_000_000.0


def _cols(db: Path, table: str) -> list:
    con = sqlite3.connect(str(db))
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        con.close()


def _scalar(db: Path, sql: str, args=()):
    con = sqlite3.connect(str(db))
    try:
        r = con.execute(sql, args).fetchone()
        return r[0] if r else None
    finally:
        con.close()


# ── 收件箱 ──────────────────────────────────────────────────────────────

def _old_list_workspaces(self):
    """升级前的 list_workspaces 原样（无 kind 过滤）。"""
    import json
    with self._lock:
        rows = self._conn.execute(
            "SELECT workspace_id, display_name, config_json, created_at, updated_at "
            "FROM workspaces ORDER BY created_at"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(zip(["workspace_id", "display_name", "config_json", "created_at", "updated_at"], row))
        try:
            d["config"] = json.loads(d.pop("config_json") or "{}")
        except Exception:
            d["config"] = {}
        result.append(d)
    return result


def _old_inbox_code(monkeypatch):
    """模拟升级前的代码：迁移表里去掉第一段语句，list_workspaces 用旧实现。"""
    phase1 = set(inbox_store_mod._AGENCY_WS_PHASE1_MIGRATIONS)
    monkeypatch.setattr(inbox_store_mod, "_MIGRATIONS",
                        [s for s in inbox_store_mod._MIGRATIONS if s not in phase1])
    monkeypatch.setattr(InboxStore, "list_workspaces", _old_list_workspaces)


def _seed_inbox(st: InboxStore) -> None:
    for i, (acct, chat) in enumerate((("7000000001", "c1"), ("7000000001", "c2"), ("a2", "c3"))):
        cid = f"telegram:{acct}:{chat}"
        conv = InboxConversation(conversation_id=cid, platform="telegram", account_id=acct,
                                 chat_key=chat, display_name=f"客户{i}")
        st.ingest_batch(conv, [
            InboxMessage(conversation_id=cid, platform_msg_id=f"m{i}a", direction="in",
                         text="hello", ts=T0 + i * 10),
            InboxMessage(conversation_id=cid, platform_msg_id=f"m{i}b", direction="out",
                         text="hi", ts=T0 + i * 10 + 1, sent_by="agent"),
        ])
    st.set_conv_tags("telegram:7000000001:c1", ["vip"])
    st.set_conversation_pinned("telegram:a2:c3", True)
    st.upsert_workspace("acme", "Acme", {"brand": "Acme"})


def _inbox_outputs(st: InboxStore) -> dict:
    ids = ["telegram:7000000001:c1", "telegram:7000000001:c2", "telegram:a2:c3"]
    hist = [(conv, msgs) for conv, msgs in st.iter_account_history("telegram", "7000000001")]
    out = {
        "list": st.list_conversations(limit=50),
        "list_acct": st.list_conversations(account_id="7000000001", limit=50),
        "get": [st.get_conversation(c) for c in ids],
        "for_ids": st.get_conversations_for_ids(ids),
        "tagged": st.list_tagged_conversations("vip"),
        "pinned": st.list_pinned_conversations(),
        "history": hist,
        "workspaces": st.list_workspaces(),
        "stats": st.get_workspace_stats("acme"),
        "recent": st.list_recent_messages(ids[0], limit=10),
    }
    # pinned_at / updated_at 由墙钟生成，跨次打开不变（同一行），无需剔除
    return out


def test_inbox_old_db_upgrade_keeps_outputs_identical_and_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "inbox.db"
    with monkeypatch.context() as m:
        _old_inbox_code(m)
        old = InboxStore(db)
        _seed_inbox(old)
        before = _inbox_outputs(old)
        old.close()
    assert "workspace_id" not in _cols(db, "conversations")          # 确是老库
    assert "kind" not in _cols(db, "workspaces")

    for _ in range(3):                                               # 正式代码，连开三次
        st = InboxStore(db)
        assert getattr(st, "migration_errors", 0) == 0
        assert _inbox_outputs(st) == before
        st.close()

    for t in ("conversations", "agent_sends", "outreach_log"):
        assert "workspace_id" in _cols(db, t)
    assert {"kind", "status", "report_token_hash"} <= set(_cols(db, "workspaces"))
    assert _scalar(db, "SELECT COUNT(*) FROM conversations WHERE workspace_id != 'default'") == 0
    assert _scalar(db, "SELECT kind FROM workspaces WHERE workspace_id='default'") == "default"
    assert _scalar(db, "SELECT kind FROM workspaces WHERE workspace_id='acme'") == "client"
    assert _scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE name='idx_conv_workspace'") == 1


def test_inbox_empty_db_has_phase1_schema_and_hides_new_column(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.db"
    st = InboxStore(fresh)
    _seed_inbox(st)
    conv = st.get_conversation("telegram:7000000001:c1")
    assert conv and "workspace_id" not in conv
    assert all("workspace_id" not in c for c in st.list_conversations(limit=10))
    assert [w["workspace_id"] for w in st.list_workspaces()] == ["acme"]   # 种子行不列出
    st.upsert_workspace("default", "主空间")                                 # 被编辑过就照常列出
    assert {w["workspace_id"] for w in st.list_workspaces()} == {"acme", "default"}
    st.close()
    # 空库 schema == 老库升级后的 schema
    old_db = tmp_path / "old.db"
    with monkeypatch.context() as m:
        _old_inbox_code(m)
        InboxStore(old_db).close()
    InboxStore(old_db).close()
    for t in ("conversations", "agent_sends", "outreach_log", "workspaces"):
        assert _cols(fresh, t) == _cols(old_db, t), t


# ── 账号注册表 ──────────────────────────────────────────────────────────

def _seed_reg(r: AccountRegistry) -> None:
    r.upsert("telegram", "7000000001", label="主号", status="online", business_line="companion",
             meta={"note": "x"})
    r.upsert("whatsapp", "a2", label="副号", status="offline")


def _reg_outputs(r: AccountRegistry) -> dict:
    return {"list": r.list(), "all": r.list(include_removed=True),
            "get": r.get("telegram", "7000000001"), "get2": r.get("whatsapp", "a2")}


def test_account_registry_upgrade_identical_idempotent_and_empty(tmp_path, monkeypatch):
    db = tmp_path / "reg.db"
    with monkeypatch.context() as m:
        phase1 = set(reg_mod._AGENCY_WS_PHASE1_MIGRATIONS)
        m.setattr(reg_mod, "_MIGRATIONS", [s for s in reg_mod._MIGRATIONS if s not in phase1])
        old = AccountRegistry(db)
        _seed_reg(old)
        before = _reg_outputs(old)
        old._conn.close()
    assert "workspace_id" not in _cols(db, "platform_accounts")
    for _ in range(3):
        r = AccountRegistry(db)
        assert _reg_outputs(r) == before
        r._conn.close()
    assert "workspace_id" in _cols(db, "platform_accounts")
    assert _scalar(db, "SELECT COUNT(*) FROM platform_accounts WHERE workspace_id='default'") == 2
    fresh = tmp_path / "fresh.db"
    r = AccountRegistry(fresh)
    _seed_reg(r)
    assert "workspace_id" not in r.get("telegram", "7000000001")
    r._conn.close()
    assert _cols(fresh, "platform_accounts") == _cols(db, "platform_accounts")


# ── 知识库 ──────────────────────────────────────────────────────────────

def _seed_kb(k: KnowledgeBaseStore) -> list:
    ids = []
    for i in range(2):
        ids.append(k.add_entry({"category": "faq", "title": f"退款{i}", "triggers": ["退款"],
                                "scenario": "s", "steps": "a", "example_reply_zh": "好的"}))
    return ids


def _kb_outputs(k: KnowledgeBaseStore, ids: list) -> dict:
    exp = k.export_all(include_disabled=True)
    exp.pop("exported_at", None)
    return {"get": [k.get_entry(i) for i in ids], "list": k.list_entries(),
            "export": exp}


def test_kb_upgrade_identical_idempotent_and_empty(tmp_path, monkeypatch):
    db = tmp_path / "kb" / "kb.db"
    with monkeypatch.context() as m:
        m.setattr(kb_mod.KnowledgeBaseStore, "_migrate_agency_ws", staticmethod(lambda c: None))
        old = KnowledgeBaseStore(db)
        ids = _seed_kb(old)
        before = _kb_outputs(old, ids)
    assert "workspace_id" not in _cols(db, "kb_entries")
    for _ in range(3):
        k = KnowledgeBaseStore(db)
        assert _kb_outputs(k, ids) == before
    assert "workspace_id" in _cols(db, "kb_entries")
    assert _scalar(db, "SELECT COUNT(*) FROM kb_entries WHERE workspace_id != 'default'") == 0
    fresh = tmp_path / "kb2" / "kb.db"
    k = KnowledgeBaseStore(fresh)
    fid = _seed_kb(k)[0]
    assert "workspace_id" not in k.get_entry(fid)
    assert _cols(fresh, "kb_entries") == _cols(db, "kb_entries")


# ── 坐席账号 ────────────────────────────────────────────────────────────

def _wu_outputs(w: WebUserStore) -> dict:
    u = w.get_user("alice")
    v = w.verify("alice", "pw-123456")
    for d in (u, v):
        if d:
            d.pop("last_login", None)          # verify 会刷新登录时间
    return {"get": u, "verify": v, "list": [dict(x, last_login=None) for x in w.list_users()]}


def test_web_users_upgrade_identical_idempotent_and_empty(tmp_path, monkeypatch):
    db = tmp_path / "web_users.db"
    with monkeypatch.context() as m:
        m.setattr(WebUserStore, "_migrate_agency_ws", lambda self: None)
        old = WebUserStore(db)
        old.create_user("alice", "pw-123456", role="admin", display_name="A")
        before = _wu_outputs(old)
        old._conn.close()
    assert "home_workspace_id" not in _cols(db, "web_users")
    assert _scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE name='workspace_members'") == 0
    for _ in range(3):
        w = WebUserStore(db)
        assert _wu_outputs(w) == before
        w._conn.close()
    assert "home_workspace_id" in _cols(db, "web_users")
    assert _cols(db, "workspace_members") == ["username", "workspace_id", "ws_role",
                                              "perms_json", "created_at"]
    assert _scalar(db, "SELECT home_workspace_id FROM web_users WHERE username='alice'") == "default"
    fresh = tmp_path / "fresh_users.db"
    w = WebUserStore(fresh)
    w.create_user("bob", "pw-123456", role="agent")
    assert "home_workspace_id" not in w.get_user("bob")
    w._conn.close()
    assert _cols(fresh, "web_users") == _cols(db, "web_users")


# ── 额度 / 辅助 ─────────────────────────────────────────────────────────

def test_quota_store_workspace_quotas_table_idempotent(tmp_path):
    db = tmp_path / "quota.db"
    for _ in range(2):
        q = LicenseQuotaStore(db)
        q._conn.close()
    assert _cols(db, "workspace_quotas") == ["workspace_id", "monthly_chars", "max_accounts",
                                             "max_seats", "updated_at"]


def test_tenancy_helpers_are_no_behavior_stubs():
    assert ws.DEFAULT_WORKSPACE_ID == "default"
    assert ws.current_workspace(None) == "default"
    assert ws.current_workspace(object()) == "default"
    assert ws.add_column_sql("t") == "ALTER TABLE t ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'"
    d = {"a": 1, "workspace_id": "x"}
    assert ws.strip_workspace(d) == {"a": 1} and ws.strip_workspace(None) is None
    assert ws.strip_all([{"workspace_id": 1, "b": 2}]) == [{"b": 2}]
    assert "INSERT OR IGNORE INTO workspaces" in ws.ensure_default_workspace_sql()
