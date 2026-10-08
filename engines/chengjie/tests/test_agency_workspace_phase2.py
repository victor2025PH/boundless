# -*- coding: utf-8 -*-
"""代运营多客户工作区 · 第二段：写入打标（只写标签，不隔离，读取行为不变）。

规则（蛋博士 2026-10-08）：会话跟着账号走；agent_sends / outreach_log / kb_entries 跟着对应的
账号或当前工作区；都没有就写 default。本文件验证：
- 账号归属到 acme → 该账号的会话 / 坐席发送 / 触达都打 acme；未归属账号仍是 default；
- 账号未归属时，触达 / 坐席发送跟着「当前工作区」（bind_workspace），没绑定就是 default；
- 带来 default 不冲掉已有标签；账号改归属后，后续写入随之改标；
- 读取行为不变：同一组写入，打标与不打标两个库的各读接口输出逐字一致，且不出现 workspace_id；
- KB：显式 > 覆盖时沿用原归属 > 当前工作区 > default；请求体里的 workspace_id 不生效；
- 应急开关 CHENGJIE_WS_TAGGING=0 → 一律 default；注册表单例未初始化 → default、不建库。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.integrations import account_registry as reg_mod
from src.integrations.account_registry import AccountRegistry
from src.tenancy import workspace as ws
from src.utils.kb_store import KnowledgeBaseStore

T0 = 1_760_000_000.0
A1, A2 = "7000000001", "a2"          # 假账号 id


def _rows(db: Path, sql: str, args=()) -> list:
    con = sqlite3.connect(str(db))
    try:
        return [tuple(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """进程单例换成临时注册表；A1 归属 acme，A2 只登记不归属。"""
    reg = AccountRegistry(tmp_path / "reg.db")
    reg.upsert("telegram", A1, status="online")
    reg.upsert("telegram", A2, status="online")
    assert reg.set_workspace("telegram", A1, "acme") is True
    monkeypatch.setattr(reg_mod, "_registry", reg)
    reg_mod._WS_CACHE.clear()
    yield reg
    reg_mod._WS_CACHE.clear()
    reg._conn.close()


def _conv(acct: str, chat: str) -> InboxConversation:
    return InboxConversation(conversation_id=f"telegram:{acct}:{chat}", platform="telegram",
                             account_id=acct, chat_key=chat, display_name=f"客户{chat}")


def _write_all(st: InboxStore) -> None:
    """同一组写入：两个账号各一个会话 + 消息、坐席发送、触达。"""
    for i, acct in enumerate((A1, A2)):
        conv = _conv(acct, f"c{i}")
        cid = conv.conversation_id
        st.ingest_batch(conv, [
            InboxMessage(conversation_id=cid, platform_msg_id=f"m{i}a", direction="in",
                         text="hello", ts=T0 + i * 10),
        ])
        st.upsert_conversation(conv)
        st.record_agent_send(cid, "alice", agent_name="A", ts=T0 + i * 10 + 1, text="hi")
        st.record_outreach(cid, batch_id="b1", platform="telegram", account_id=acct, ts=T0 + i)


def _ws_of(db: Path, table: str, where: str, args) -> set:
    return {r[0] for r in _rows(db, f"SELECT workspace_id FROM {table} WHERE {where}", args)}


def test_writes_follow_account_workspace(tmp_path, registry):
    db = tmp_path / "inbox.db"
    st = InboxStore(db)
    _write_all(st)
    st.close()
    c1, c2 = f"telegram:{A1}:c0", f"telegram:{A2}:c1"
    assert _ws_of(db, "conversations", "conversation_id=?", (c1,)) == {"acme"}
    assert _ws_of(db, "conversations", "conversation_id=?", (c2,)) == {"default"}
    assert _ws_of(db, "agent_sends", "conversation_id=?", (c1,)) == {"acme"}
    assert _ws_of(db, "agent_sends", "conversation_id=?", (c2,)) == {"default"}
    assert _ws_of(db, "outreach_log", "conversation_id=?", (c1,)) == {"acme"}
    assert _ws_of(db, "outreach_log", "conversation_id=?", (c2,)) == {"default"}


def test_unassigned_account_uses_current_workspace_then_default(tmp_path, registry):
    db = tmp_path / "inbox.db"
    st = InboxStore(db)
    conv = _conv(A2, "x")
    cid = conv.conversation_id
    with ws.bind_workspace("beta"):
        st.ingest_batch(conv, [InboxMessage(conversation_id=cid, platform_msg_id="p1",
                                            direction="in", text="hi", ts=T0)])
        st.record_agent_send(cid, "bob", ts=T0 + 1)
        st.record_outreach(cid, platform="telegram", account_id=A2, ts=T0 + 2)
    st.record_outreach(f"telegram:{A2}:none", platform="telegram", account_id=A2, ts=T0 + 3)
    assert ws.current_write_workspace() == "default"               # 退出即还原
    st.close()
    assert _ws_of(db, "conversations", "conversation_id=?", (cid,)) == {"beta"}
    assert _ws_of(db, "agent_sends", "conversation_id=?", (cid,)) == {"beta"}
    assert _ws_of(db, "outreach_log", "conversation_id=?", (cid,)) == {"beta"}
    assert _ws_of(db, "outreach_log", "conversation_id=?", (f"telegram:{A2}:none",)) == {"default"}


def test_default_never_clobbers_and_reassignment_follows_account(tmp_path, registry):
    db = tmp_path / "inbox.db"
    st = InboxStore(db)
    conv = _conv(A1, "c0")
    cid = conv.conversation_id
    st.upsert_conversation(conv)
    assert _ws_of(db, "conversations", "conversation_id=?", (cid,)) == {"acme"}
    # 注册表暂不可用（单例未初始化）→ 带来 default，不冲掉 acme
    reg_mod._WS_CACHE.clear()
    import src.integrations.account_registry as m
    saved, m._registry = m._registry, None
    try:
        st.upsert_conversation(conv)
        st.record_agent_send(cid, "alice", ts=T0)            # 账号查不到 → 用会话已有标签
    finally:
        m._registry = saved
        reg_mod._WS_CACHE.clear()
    assert _ws_of(db, "conversations", "conversation_id=?", (cid,)) == {"acme"}
    assert _ws_of(db, "agent_sends", "conversation_id=?", (cid,)) == {"acme"}
    # 账号改归属 → 后续写入跟着走
    registry.set_workspace("telegram", A1, "zeta")
    st.upsert_conversation(conv)
    st.close()
    assert _ws_of(db, "conversations", "conversation_id=?", (cid,)) == {"zeta"}


def _read_outputs(st: InboxStore) -> dict:
    ids = [f"telegram:{A1}:c0", f"telegram:{A2}:c1"]
    return {
        "list": st.list_conversations(limit=50),
        "list_acct": st.list_conversations(account_id=A1, limit=50),
        "get": [st.get_conversation(c) for c in ids],
        "for_ids": st.get_conversations_for_ids(ids),
        "history": [(c, m) for c, m in st.iter_account_history("telegram", A1)],
        "recent": [st.list_recent_messages(c, limit=10) for c in ids],
        "outreach_ts": [st.last_outreach_ts(c) for c in ids],
        "outreach_n": [st.count_outreach_since(c, 0) for c in ids],
        "workspaces": st.list_workspaces(),
        "stats": st.get_workspace_stats("acme"),
    }


def test_reads_unchanged_tagged_vs_untagged(tmp_path, registry, monkeypatch):
    monkeypatch.setattr(InboxStore, "_now", staticmethod(lambda: T0 + 1000))
    tagged = InboxStore(tmp_path / "tagged.db")
    _write_all(tagged)
    monkeypatch.setenv("CHENGJIE_WS_TAGGING", "0")
    plain = InboxStore(tmp_path / "plain.db")
    _write_all(plain)
    monkeypatch.delenv("CHENGJIE_WS_TAGGING")
    a, b = _read_outputs(tagged), _read_outputs(plain)
    assert a == b                                            # 打标不改变任何读取输出
    assert len(a["list"]) == 2                               # 不隔离：两个工作区的会话都在
    assert "workspace_id" not in repr(a["list"]) and "workspace_id" not in repr(a["get"])
    tagged.close(); plain.close()
    assert _rows(tmp_path / "plain.db",
                 "SELECT COUNT(*) FROM conversations WHERE workspace_id != 'default'") == [(0,)]
    assert _rows(tmp_path / "tagged.db",
                 "SELECT COUNT(*) FROM conversations WHERE workspace_id = 'acme'") == [(1,)]


def test_kb_entry_workspace_rules(tmp_path, registry):
    db = tmp_path / "kb" / "kb.db"
    k = KnowledgeBaseStore(db)
    base = {"category": "faq", "title": "退款", "triggers": ["退款"], "steps": "a"}
    e_def = k.add_entry(dict(base))
    with ws.bind_workspace("acme"):
        e_cur = k.add_entry(dict(base, title="当前工作区"))
    e_exp = k.add_entry(dict(base, title="显式"), workspace_id="zeta")
    e_body = k.add_entry(dict(base, title="请求体", workspace_id="evil"))   # 请求体不能指定
    k.add_entry(dict(base, id=e_cur, title="覆盖后沿用"))                   # INSERT OR REPLACE
    k.add_entry(dict(base, id=e_exp, title="显式改回"), workspace_id="default")
    got = dict(_rows(db, "SELECT id, workspace_id FROM kb_entries"))
    assert got == {e_def: "default", e_cur: "acme", e_exp: "default", e_body: "default"}
    assert k.get_entry(e_cur)["title"] == "覆盖后沿用"
    assert "workspace_id" not in k.get_entry(e_cur)
    assert all("workspace_id" not in e for e in k.list_entries())


def test_kill_switch_and_no_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(reg_mod, "_registry", None)
    reg_mod._WS_CACHE.clear()
    assert ws.account_workspace("telegram", A1) == "default"
    assert reg_mod._registry is None                          # 不隐式建库
    assert ws.resolve_write_workspace(platform="telegram", account_id=A1) == "default"
    with ws.bind_workspace("acme"):
        assert ws.resolve_write_workspace() == "acme"
        monkeypatch.setenv("CHENGJIE_WS_TAGGING", "0")
        assert ws.resolve_write_workspace(explicit="zeta") == "default"
    monkeypatch.delenv("CHENGJIE_WS_TAGGING")
    assert ws.resolve_write_workspace(explicit="  zeta ") == "zeta"
    assert ws.resolve_write_workspace(fallback="default") == "default"


def test_registry_set_workspace_hidden_from_outputs(tmp_path):
    reg = AccountRegistry(tmp_path / "r.db")
    reg.upsert("telegram", A1)
    before = reg.get("telegram", A1)
    assert reg.set_workspace("telegram", A1, "acme") is True
    assert reg.set_workspace("telegram", "nobody", "acme") is False
    assert reg.workspace_of("telegram", A1) == "acme"
    after = reg.get("telegram", A1)
    assert "workspace_id" not in after
    before.pop("updated_at"); after.pop("updated_at")
    assert before == after
    reg.set_workspace("telegram", A1, "")
    assert reg.workspace_of("telegram", A1) == "default"
    reg._conn.close()
