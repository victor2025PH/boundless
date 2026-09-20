"""会话删除 + 防复活墓碑 + 账号数据清除 + 离线账号联系人面板隐藏（2026-08-16）。

老板三连需求的契约钉：
1. 坐席「删除会话」＝store 全表硬删 + 墓碑——账号在线时目录同步每 ~5min 全量
   推侧栏占位，没有墓碑删了就复活；**真实新消息**（ts 晚于删除时刻）自动解除
   墓碑回显，历史重放（telegram 实时聚合旁路 / thread-history 回填，ts 必然早于
   删除时刻）保持死亡。
2. 删除账号可选 ``purge_data``＝账号级清库（刻意不落墓碑：同号重登=重新加载）。
3. 联系人面板对 offline/removed 账号返回空（聊天页 2026-08-01 就隐藏了，该面
   是唯一漏网——老板实测离线号在「联系人」页签仍列会话 peer 当好友名单）。
"""

from __future__ import annotations

import time
import types
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore

CID = "messenger:6158:777"


def _store(tmp_path) -> InboxStore:
    return InboxStore(tmp_path / "inbox.db")


def _seed_conv(store: InboxStore, cid: str = CID, *, ts: float = 0.0) -> None:
    plat, acct, ck = cid.split(":", 2)
    ts = ts or (time.time() - 3600)
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name="Bea", last_text="hi",
                          last_ts=ts),
        [InboxMessage(conversation_id=cid, direction="in", text="hi", ts=ts),
         InboxMessage(conversation_id=cid, direction="out", text="hello",
                      ts=ts + 1)],
    )


# ── 1) store：单会话硬删 + 墓碑 ─────────────────────────────────────────────

def test_delete_conversation_purges_all_tables(tmp_path):
    s = _store(tmp_path)
    _seed_conv(s)
    # 铺关联表数据（升级记录/会话元数据），验证结构发现式全表清除
    with s._lock:
        s._conn.execute(
            "INSERT INTO escalations (conversation_id, ts) VALUES (?, ?)",
            (CID, time.time()))
        s._conn.execute(
            "INSERT OR REPLACE INTO conversation_meta"
            " (conversation_id, updated_at) VALUES (?, ?)",
            (CID, time.time()))
        s._conn.commit()
    deleted = s.delete_conversation_data(CID, deleted_by="boss")
    assert deleted.get("conversations") == 1
    assert deleted.get("messages") == 2
    assert deleted.get("escalations") == 1
    assert deleted.get("conversation_meta") == 1
    assert s.get_conversation(CID) is None
    assert s.list_recent_messages(CID, limit=10) == []
    assert s.is_conversation_tombstoned(CID) is True
    # FTS 随触发器同步清空（环境无 FTS5 时表不存在，跳过断言）
    if getattr(s, "_fts5_available", False):
        with s._lock:
            n = s._conn.execute(
                "SELECT count(*) FROM messages_fts WHERE conversation_id=?",
                (CID,)).fetchone()[0]
        assert n == 0


def test_delete_missing_conversation_is_safe(tmp_path):
    s = _store(tmp_path)
    deleted = s.delete_conversation_data("messenger:6158:nope")
    assert deleted == {}
    assert s.is_conversation_tombstoned("messenger:6158:nope") is True


# ── 2) 防复活：目录同步 / 历史回放 / 真实新消息三分法 ───────────────────────

def test_directory_sync_does_not_resurrect_tombstoned(tmp_path):
    s = _store(tmp_path)
    _seed_conv(s)
    s.delete_conversation_data(CID)
    n = s.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea", "ts": time.time()},
        {"jid": "888", "name": "Cat", "ts": time.time()},
    ])
    assert n == 1                       # 只有未删的 888 建了占位
    assert s.get_conversation(CID) is None
    assert s.get_conversation("messenger:6158:888") is not None


def test_history_replay_stays_dead(tmp_path):
    """历史重放（ts 早于删除时刻）不得复活——telegram 实时聚合每轮旁路 re-ingest
    会话末条，这正是删除后「几秒内诈尸」的向量。"""
    s = _store(tmp_path)
    old_ts = time.time() - 3600
    _seed_conv(s, ts=old_ts)
    s.delete_conversation_data(CID)
    n = s.ingest_batch(
        InboxConversation(conversation_id=CID, platform="messenger",
                          account_id="6158", chat_key="777",
                          display_name="Bea", last_text="hi", last_ts=old_ts),
        [InboxMessage(conversation_id=CID, direction="in", text="hi",
                      ts=old_ts)],
    )
    assert n == 0
    assert s.get_conversation(CID) is None
    assert s.is_conversation_tombstoned(CID) is True


def test_fresh_inbound_resurrects_and_clears_tombstone(tmp_path):
    """客户再开口（ts 晚于删除时刻）必须回显——宁可复活绝不丢消息。"""
    s = _store(tmp_path)
    _seed_conv(s)
    s.delete_conversation_data(CID)
    new_ts = time.time() + 5
    n = s.ingest_batch(
        InboxConversation(conversation_id=CID, platform="messenger",
                          account_id="6158", chat_key="777",
                          display_name="Bea", last_text="hello again",
                          last_ts=new_ts),
        [InboxMessage(conversation_id=CID, direction="in",
                      text="hello again", ts=new_ts)],
    )
    assert n == 1
    assert s.get_conversation(CID) is not None
    assert s.is_conversation_tombstoned(CID) is False
    # 解除后目录同步恢复正常占位更新
    assert s.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea", "ts": time.time()}]) == 1


def test_ingest_message_gate_same_semantics(tmp_path):
    s = _store(tmp_path)
    _seed_conv(s)
    s.delete_conversation_data(CID)
    old = InboxMessage(conversation_id=CID, direction="in", text="old",
                       ts=time.time() - 999)
    assert s.ingest_message(old) is False
    assert s.is_conversation_tombstoned(CID) is True
    fresh = InboxMessage(conversation_id=CID, direction="in", text="new",
                         ts=time.time() + 5)
    assert s.ingest_message(fresh) is True
    assert s.is_conversation_tombstoned(CID) is False


# ── 3) store：账号级清库 ────────────────────────────────────────────────────

def test_purge_account_conversations_scoped(tmp_path):
    s = _store(tmp_path)
    _seed_conv(s, "messenger:6158:777")
    _seed_conv(s, "messenger:6158:888")
    _seed_conv(s, "messenger:9999:777")     # 别的账号，不许误伤
    s.delete_conversation_data("messenger:6158:777")  # 留一块墓碑验证连带清
    deleted = s.purge_account_conversations("messenger", "6158",
                                            deleted_by="boss")
    assert deleted.get("conversations") == 1          # 888（777 已删）
    assert deleted.get("conversation_tombstones") == 1
    assert s.get_conversation("messenger:6158:888") is None
    assert s.is_conversation_tombstoned("messenger:6158:777") is False
    # 同号重登后首连全量同步可整体回灌（无墓碑阻拦）
    assert s.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea", "ts": time.time()}]) == 1
    # 别的账号原样
    assert s.get_conversation("messenger:9999:777") is not None


def test_purge_account_prefix_is_exact(tmp_path):
    """前缀等长比较：账号 '61' 的清库不得误删 '6158' 的数据。"""
    s = _store(tmp_path)
    _seed_conv(s, "messenger:6158:777")
    _seed_conv(s, "messenger:61:777")
    s.purge_account_conversations("messenger", "61")
    assert s.get_conversation("messenger:61:777") is None
    assert s.get_conversation("messenger:6158:777") is not None


# ── 4) 路由：会话删除端点 ───────────────────────────────────────────────────

def _read_app(tmp_path, *, with_session: bool = False):
    from src.web.routes.unified_inbox_read_routes import register_read_routes
    app = FastAPI()
    if with_session:
        app.add_middleware(SessionMiddleware, secret_key="t")
    register_read_routes(app, api_auth=lambda request: None,
                         config_manager=SimpleNamespace(config={}))
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return app, store


def test_delete_route_happy_path(tmp_path):
    app, store = _read_app(tmp_path)
    _seed_conv(store)
    c = TestClient(app)
    r = c.post("/api/unified-inbox/conversations/delete",
               json={"conversation_id": CID})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["deleted"].get("messages") == 2
    assert store.get_conversation(CID) is None
    assert store.is_conversation_tombstoned(CID) is True


def test_delete_route_accepts_triplet(tmp_path):
    app, store = _read_app(tmp_path)
    _seed_conv(store)
    c = TestClient(app)
    r = c.post("/api/unified-inbox/conversations/delete", json={
        "platform": "messenger", "account_id": "6158", "chat_key": "777"})
    assert r.status_code == 200
    assert store.get_conversation(CID) is None


def test_delete_route_denies_agent_and_viewer(tmp_path):
    app, store = _read_app(tmp_path, with_session=True)
    _seed_conv(store)

    @app.post("/test-login")
    async def _login(request: Request):
        request.session["role"] = request.query_params.get("role", "agent")
        return {"ok": True, "set": request.session.get("role")}

    c = TestClient(app)
    for role in ("agent", "viewer"):
        r0 = c.post(f"/test-login?role={role}")
        assert r0.status_code == 200 and r0.json().get("set") == role, r0.text
        r = c.post("/api/unified-inbox/conversations/delete",
                   json={"conversation_id": CID})
        assert r.status_code == 403, f"role={role} -> {r.status_code} {r.text}"
    assert store.get_conversation(CID) is not None   # 一行没删


def test_delete_route_requires_target(tmp_path):
    app, _ = _read_app(tmp_path)
    c = TestClient(app)
    assert c.post("/api/unified-inbox/conversations/delete",
                  json={}).status_code == 400


# ── 5) 路由：历史回填墓碑跳过 + 联系人面板离线隐藏 + 账号删除清库 ───────────

def _acct_app(tmp_path, cfg=None):
    import src.web.routes.unified_inbox_account_routes as uar
    app = FastAPI()
    uar.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config=cfg or {}))
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return app, store


def test_thread_history_respects_tombstone(tmp_path):
    app, store = _acct_app(tmp_path)
    _seed_conv(store)
    store.delete_conversation_data(CID)
    c = TestClient(app)
    r = c.post("/api/internal/protocol/thread-history", json={
        "platform": "messenger", "account_id": "6158", "chat_key": "777",
        "name": "Bea", "ts": time.time(),
        "messages": [{"direction": "in", "sender": "Bea", "text": "hi"}],
    })
    assert r.status_code == 200
    assert r.json().get("reason") == "tombstoned"
    assert store.get_conversation(CID) is None


def test_contacts_endpoint_hides_offline_account(tmp_path):
    from src.integrations.account_registry import get_account_registry
    app, store = _acct_app(tmp_path)
    _seed_conv(store)
    reg = get_account_registry()
    reg.upsert("messenger", "6158", mode="web", status="offline")
    c = TestClient(app)
    r = c.get("/api/platforms/messenger/6158/contacts?enriched=1")
    d = r.json()
    assert d["count"] == 0 and d["contacts"] == []
    assert d.get("account_offline") is True
    # 重登（online）即恢复正常读取
    reg.upsert("messenger", "6158", status="online")
    d2 = c.get("/api/platforms/messenger/6158/contacts?enriched=1").json()
    assert d2.get("account_offline") is None
    assert d2["count"] >= 1          # 并集口径：聊过的 777 回来了


def test_contacts_endpoint_unregistered_account_unaffected(tmp_path):
    """不在册账号（config/适配器来源）没有注册表退出语义——行为不变。"""
    app, store = _acct_app(tmp_path)
    _seed_conv(store)
    c = TestClient(app)
    d = c.get("/api/platforms/messenger/6158/contacts?enriched=1").json()
    assert d.get("account_offline") is None
    assert d["count"] >= 1


def test_account_remove_with_purge(tmp_path, monkeypatch):
    import src.integrations.messenger_web_login as mgw
    import src.web.routes.unified_inbox_account_routes as uar
    from src.integrations.account_registry import get_account_registry

    class _FakeOrch:
        async def stop_account(self, key):
            return True

    monkeypatch.setattr(uar, "get_orchestrator", lambda cfg=None: _FakeOrch())
    monkeypatch.setattr(uar, "ensure_builtin_workers", lambda cfg=None: None)

    async def _fake_post(url, payload, timeout=20.0):
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    app, store = _acct_app(tmp_path)
    _seed_conv(store)
    reg = get_account_registry()
    reg.upsert("messenger", "6158", mode="web", status="offline")
    c = TestClient(app)
    r = c.post("/api/accounts/messenger/6158/remove",
               json={"purge_data": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["purge_data"] is True
    assert d["purged"].get("conversations") == 1
    assert store.get_conversation(CID) is None
    assert str(reg.get("messenger", "6158")["status"]) == "removed"


def test_account_remove_default_keeps_data(tmp_path, monkeypatch):
    import src.integrations.messenger_web_login as mgw
    import src.web.routes.unified_inbox_account_routes as uar
    from src.integrations.account_registry import get_account_registry

    class _FakeOrch:
        async def stop_account(self, key):
            return True

    monkeypatch.setattr(uar, "get_orchestrator", lambda cfg=None: _FakeOrch())
    monkeypatch.setattr(uar, "ensure_builtin_workers", lambda cfg=None: None)

    async def _fake_post(url, payload, timeout=20.0):
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    app, store = _acct_app(tmp_path)
    _seed_conv(store)
    get_account_registry().upsert("messenger", "6158", mode="web",
                                  status="offline")
    c = TestClient(app)
    r = c.post("/api/accounts/messenger/6158/remove")
    assert r.status_code == 200
    assert r.json()["purged"] == {}
    assert store.get_conversation(CID) is not None   # 数据留库（旧语义）
