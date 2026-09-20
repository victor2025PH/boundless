"""历史账号治理 P0（2026-08-17）契约钉：彻底删除 + 历史只读视角后端。

事故背景：账号面板「历史 / 已退出」分区只能看不能删（history_only 号 /remove
直接 404、removed 号 purge 完留「已移除 · 0 会话」死行）；「查看会话」对
已退出号是三层死意图（面板关闭 → chip 回落 → 服务端过滤 + thread 409）。
这里钉住后端半边：

1. ``AccountRegistry.delete_row``：硬删（与软删 remove 互补）、幂等；
2. ``store.purge_account_conversations`` 清完后 ``account_directory`` 不再
   含该号（历史区条目随数据消失，无需额外簿记）；
3. ``_collect_chats_from_store(include_hidden=)``：默认跳过 offline 行为不变，
   开启后 offline 行放行且 ``read_only=True``（composer 端上必须锁死）；
4. 静态接线钉：purge-history 路由 + 安全门禁（活跃/config/合成号 409）、
   chats 路由 include_hidden 透传 + ``hidden_included`` 特性探测标记、
   thread 路由 ``history=1`` 放行只读查档。
"""

from __future__ import annotations

import inspect
import time
from types import SimpleNamespace

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.integrations.account_registry import AccountRegistry


def _seed_conv(store: InboxStore, plat: str, acct: str, ck: str, ts: float):
    cid = f"{plat}:{acct}:{ck}"
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name=ck,
                          last_text="hi", last_ts=ts),
        [InboxMessage(conversation_id=cid, direction="in", text="hi", ts=ts)],
    )


# ── 1) 注册表硬删 ───────────────────────────────────────────────────────────

def test_registry_delete_row_hard_deletes(tmp_path):
    reg = AccountRegistry(tmp_path / "reg.db")
    reg.upsert("telegram", "a1", mode="protocol")
    reg.set_status("telegram", "a1", "removed")
    assert reg.get("telegram", "a1") is not None
    assert reg.delete_row("telegram", "a1") is True
    assert reg.get("telegram", "a1") is None
    # include_removed 全量口径也不该再看到（软删是留行，硬删是消行）
    assert reg.list(include_removed=True) == []
    # 幂等：再删返回 False 不抛
    assert reg.delete_row("telegram", "a1") is False


def test_registry_delete_row_missing_returns_false(tmp_path):
    reg = AccountRegistry(tmp_path / "reg.db")
    assert reg.delete_row("telegram", "nope") is False


# ── 2) 清库后历史条目自然消失 ───────────────────────────────────────────────

def test_purge_then_account_directory_drops_entry(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "ghost1", "u1", now - 10)
    _seed_conv(s, "telegram", "keep1", "u2", now - 5)
    assert ("telegram", "ghost1") in s.account_directory()
    purged = s.purge_account_conversations("telegram", "ghost1", deleted_by="t")
    assert sum(purged.values()) > 0
    d = s.account_directory()
    assert ("telegram", "ghost1") not in d
    assert ("telegram", "keep1") in d


# ── 3) include_hidden 行为语义 ──────────────────────────────────────────────

def _fake_request(store):
    cm = SimpleNamespace(config={})
    state = SimpleNamespace(inbox_store=store, config_manager=cm)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_collect_chats_include_hidden_semantics(tmp_path, monkeypatch):
    import src.web.routes.unified_inbox_aggregate as agg
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "out1", "u1", now - 10)   # 已退出号
    _seed_conv(s, "telegram", "live1", "u2", now - 5)   # 正常号
    monkeypatch.setattr(
        agg, "_account_status_map",
        lambda req: {("telegram", "out1"): "offline"})
    req = _fake_request(s)

    # 默认口径：offline 行整批跳过（聊天页语义不变）
    rows = agg._collect_chats_from_store(req, limit=30)
    accts = {str(c.get("account_id") or "") for c in rows}
    assert "live1" in accts and "out1" not in accts

    # 历史视角：offline 行放行且只读锁死
    rows2 = agg._collect_chats_from_store(
        req, limit=30, account_id="out1", include_hidden=True)
    assert rows2, "include_hidden 下 offline 行必须可见"
    for c in rows2:
        assert str(c.get("account_id") or "") == "out1"
        assert c.get("read_only") is True, "已退出号历史必须只读"
        assert str(c.get("account_status") or "") == "offline"

    # include_hidden=False + scoped：仍然不可见（默认行为回归钉）
    rows3 = agg._collect_chats_from_store(req, limit=30, account_id="out1")
    assert rows3 == []


# ── 4) 路由静态接线钉 ───────────────────────────────────────────────────────

def test_purge_history_route_wired_with_guards():
    import src.web.routes.unified_inbox_account_routes as acct_mod
    src = inspect.getsource(acct_mod)
    assert "/api/accounts/{platform}/{account_id}/purge-history" in src
    # 安全门禁三件套：活跃在册 409 / config 常驻号 409 / 内置合成号 409
    assert "err.ws.account_active_no_purge" in src
    assert "is_synthetic_account" in src
    assert "_collect_config_accounts(cfg)" in src
    # 数据清除 + 注册表硬删两步收尾
    assert "purge_account_conversations" in src
    assert "delete_row" in src
    # 审计落 ops_events
    assert "account_purge" in src


def test_chats_route_include_hidden_wired():
    import src.web.routes.unified_inbox_read_routes as read_mod
    src = inspect.getsource(read_mod)
    assert "include_hidden: int = 0" in src
    # 仅 account_id scoped 才放行（全局列表口径不变）
    assert "bool(include_hidden) and bool(account_id)" in src
    # 特性探测标记：前端据此区分旧后端
    assert '"hidden_included": want_hidden' in src


def test_thread_route_history_param_wired():
    import src.web.routes.unified_inbox_read_routes as read_mod
    src = inspect.getsource(read_mod)
    assert "history: int = 0" in src
    assert '_st == "offline" and not history' in src


def test_collect_chats_signature_has_include_hidden():
    import src.web.routes.unified_inbox_aggregate as agg
    sig = inspect.signature(agg._collect_chats_from_store)
    assert "include_hidden" in sig.parameters
    assert sig.parameters["include_hidden"].default is False


# ── 5) 路由级端到端（最小 app：只挂读路由 + 临时 store）────────────────────

def _mini_client(store):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from src.web.routes.unified_inbox_read_routes import register_read_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_read_routes(app, api_auth=api_auth, config_manager=None)
    app.state.inbox_store = store
    return TestClient(app)


def test_chats_route_include_hidden_end_to_end(tmp_path, monkeypatch):
    import src.web.routes.unified_inbox_aggregate as agg
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "out1", "u1", now - 10)
    monkeypatch.setattr(
        agg, "_account_status_map",
        lambda req: {("telegram", "out1"): "offline"})
    c = _mini_client(s)
    # 默认 scoped：offline 行被过滤（旧口径回归钉）
    d0 = c.get("/api/unified-inbox/chats?platform=telegram&account_id=out1").json()
    assert d0["ok"] and d0["chats"] == [] and d0["hidden_included"] is False
    # include_hidden=1 + account_id scoped：放行 + 标记 + 只读锁
    d1 = c.get("/api/unified-inbox/chats?platform=telegram&account_id=out1"
               "&include_hidden=1").json()
    assert d1["ok"] and d1["hidden_included"] is True
    assert len(d1["chats"]) == 1
    assert d1["chats"][0]["read_only"] is True
    # include_hidden 无 account_id：不放行（防全局列表被隐藏行污染）
    d2 = c.get("/api/unified-inbox/chats?platform=telegram&include_hidden=1").json()
    assert d2["ok"] and d2["chats"] == [] and d2["hidden_included"] is False
    s.close()


def test_thread_route_history_bypasses_offline_409(tmp_path, monkeypatch):
    import src.web.routes.unified_inbox_read_routes as read_mod
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "out1", "u1", now - 10)
    monkeypatch.setattr(
        read_mod, "_account_status_map",
        lambda req: {("telegram", "out1"): "offline"})
    c = _mini_client(s)
    base = "/api/unified-inbox/thread?platform=telegram&account_id=out1&chat_key=u1"
    r0 = c.get(base)
    assert r0.status_code == 409, "无 history 参数必须维持 409（聊天页语义不变）"
    r1 = c.get(base + "&history=1")
    assert r1.status_code == 200, "history=1＝显式只读查档，必须放行"
    d1 = r1.json()
    assert d1.get("ok") is True
    assert any("hi" in str(m.get("text") or "") for m in (d1.get("messages") or []))
    s.close()
