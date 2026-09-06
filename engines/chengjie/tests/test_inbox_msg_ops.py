"""官方级消息管理（2026-08-17 P0/P1）契约钉：置顶 / 仅工作台软删 / 双端撤回。

boss 需求「消息列表与官方一致」的服务端不变量：
1. 置顶＝conversation_meta.pinned_at 服务端落库（工作台全局），tags_map 携带、
   scoped 可查、置顶行不受 top-N 时间窗截断（list_pinned_conversations）。
2. 「仅工作台删除」＝messages 软删（deleted_at>0）：UI 读路径（include_deleted=False
   / 全文检索）不可见；**业务口径默认参必须仍看到**（回复时延/replied-after 护栏
   ——本地删除不改变「客户收到过回复」的事实）；可恢复（undo 服务端退路）；
   删末条后会话预览重算不撒谎。与 revoked（平台侧撤回灰墓碑）语义分开。
3. 双端撤回路由：服务端强制「只撤自己发出的消息」；telegram/line 经编排器
   delete_messages；messenger 如实 unsupported；成功落 revoked + 本地状态。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore

CID = "telegram:8244899900:5433982810"


def _store(tmp_path) -> InboxStore:
    return InboxStore(tmp_path / "inbox.db")


def _seed(store: InboxStore, cid: str = CID, *, n_out: int = 2) -> float:
    plat, acct, ck = cid.split(":", 2)
    base = time.time() - 3600
    msgs = [InboxMessage(conversation_id=cid, direction="in", text="hello there",
                         ts=base, platform_msg_id="9001")]
    for i in range(n_out):
        msgs.append(InboxMessage(
            conversation_id=cid, direction="out", text=f"reply {i}",
            ts=base + 10 + i, platform_msg_id=str(5001 + i)))
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name="Alina",
                          last_text=msgs[-1].text, last_ts=msgs[-1].ts),
        msgs,
    )
    return base


# ── 1) 置顶 ────────────────────────────────────────────────────────────────

def test_pin_toggle_and_tags_map(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    ts = s.set_conversation_pinned(CID, True)
    assert ts > 0
    meta = s.list_conv_tags_map([CID])[CID]
    assert meta["pinned_at"] == ts
    # 取消置顶归零；tags_map 其余键不受影响
    assert s.set_conversation_pinned(CID, False) == 0.0
    meta2 = s.list_conv_tags_map([CID])[CID]
    assert meta2["pinned_at"] == 0.0
    assert meta2["archived"] is False


def test_list_pinned_conversations_scoped(tmp_path):
    s = _store(tmp_path)
    _seed(s, "telegram:acct1:111")
    _seed(s, "telegram:acct2:222")
    _seed(s, "line:acct3:333")
    for cid in ("telegram:acct1:111", "line:acct3:333"):
        s.set_conversation_pinned(cid, True)
    allp = s.list_pinned_conversations()
    assert {r["conversation_id"] for r in allp} == {
        "telegram:acct1:111", "line:acct3:333"}
    assert all(float(r["pinned_at"] or 0) > 0 for r in allp)
    tg = s.list_pinned_conversations(platform="telegram")
    assert [r["conversation_id"] for r in tg] == ["telegram:acct1:111"]
    assert s.list_pinned_conversations(platform="telegram",
                                       account_id="acct2") == []


def test_pinned_survives_topn_window(tmp_path):
    """置顶恒在首屏：置顶会话老到掉出 list_conversations 的 top-N 窗口后，
    list_pinned_conversations 仍必须把它找回来（聚合层据此合并）。"""
    s = _store(tmp_path)
    old_cid = "telegram:a:100"
    _seed(s, old_cid)
    s.set_conversation_pinned(old_cid, True)
    now = time.time()
    for i in range(30):
        _seed(s, f"telegram:a:{200 + i}")
        with s._lock:
            s._conn.execute(
                "UPDATE conversations SET last_ts=? WHERE conversation_id=?",
                (now + i, f"telegram:a:{200 + i}"))
            s._conn.commit()
    recent = s.list_conversations(limit=10, platform="telegram", account_id="a")
    assert old_cid not in {r["conversation_id"] for r in recent}
    pinned = s.list_pinned_conversations(platform="telegram", account_id="a")
    assert [r["conversation_id"] for r in pinned] == [old_cid]


# ── 2) 仅工作台软删 / 恢复 / 清空 ──────────────────────────────────────────

def _mids(store: InboxStore, cid: str = CID):
    return [r["message_id"] for r in store.list_recent_messages(cid, limit=50)]


def test_delete_local_filters_ui_but_not_business(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    mids = _mids(s)
    n = s.delete_messages_local(CID, [mids[-1]], deleted_by="katie")
    assert n == 1
    ui = s.list_recent_messages(CID, limit=50, include_deleted=False)
    assert len(ui) == 2 and mids[-1] not in {r["message_id"] for r in ui}
    # 业务口径（默认参）必须仍看到软删行——回复时延/replied-after 依赖这个事实
    biz = s.list_recent_messages(CID, limit=50)
    assert len(biz) == 3
    row = s.get_message(mids[-1])
    assert row["deleted_at"] > 0 and row["deleted_by"] == "katie"
    # 幂等：重复删不重复计数
    assert s.delete_messages_local(CID, [mids[-1]]) == 0


def test_delete_local_recomputes_preview_and_restore(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    mids = _mids(s)
    conv0 = s.get_conversation(CID)
    assert conv0["last_text"] == "reply 1"
    # 删末条 → 预览滚回上一条可见消息（预览撒谎比没有预览更糟）
    s.delete_messages_local(CID, [mids[-1]])
    conv1 = s.get_conversation(CID)
    assert conv1["last_text"] == "reply 0"
    # 恢复 → 预览滚回原样（undo 的服务端退路）
    assert s.restore_messages_local(CID, [mids[-1]]) == 1
    conv2 = s.get_conversation(CID)
    assert conv2["last_text"] == "reply 1"
    assert len(s.list_recent_messages(CID, limit=50, include_deleted=False)) == 3


def test_clear_conversation_keeps_row_and_ts(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    before = s.get_conversation(CID)
    n = s.clear_conversation_messages(CID, deleted_by="boss")
    assert n == 3
    assert s.list_recent_messages(CID, limit=50, include_deleted=False) == []
    conv = s.get_conversation(CID)
    assert conv is not None                      # 会话行保留（≠删除会话）
    assert conv["last_text"] == ""               # 预览清空
    assert conv["last_ts"] == before["last_ts"]  # 列表位置不跳（官方语义）
    assert s.is_conversation_tombstoned(CID) is False


def test_search_excludes_soft_deleted(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    hits = s.search_messages("hello there", limit=10)
    assert any(r["conversation_id"] == CID for r in hits)
    mids = _mids(s)
    s.delete_messages_local(CID, [mids[0]])      # 软删那条入站 hello
    hits2 = s.search_messages("hello there", limit=10)
    assert not any(r["conversation_id"] == CID for r in hits2)


def test_revoked_is_not_local_deleted(tmp_path):
    """revoked（平台撤回灰墓碑，仍可见）与软删（不可见）语义分开。"""
    s = _store(tmp_path)
    _seed(s)
    assert s.mark_message_revoked(CID, "5001") is True
    ui = s.list_recent_messages(CID, limit=50, include_deleted=False)
    assert len(ui) == 3
    assert any(r["revoked"] for r in ui)


# ── 3) msgops 路由（pin / delete / restore / clear / meta）─────────────────

def _msgops_app(tmp_path, *, with_session: bool = False):
    from src.web.routes.unified_inbox_msgops_routes import register_msgops_routes
    app = FastAPI()
    if with_session:
        app.add_middleware(SessionMiddleware, secret_key="t")
    register_msgops_routes(app, api_auth=lambda request: None,
                           config_manager=SimpleNamespace(config={}))
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return app, store


def test_meta_endpoint_shape(tmp_path):
    app, _ = _msgops_app(tmp_path)
    d = TestClient(app).get("/api/unified-inbox/message-ops/meta").json()
    assert d["ok"] is True and d["pin"] is True and d["local_delete"] is True
    assert d["revoke_platforms"]["telegram"] is True
    assert d["revoke_platforms"]["line"] is True
    assert d["revoke_platforms"]["messenger"] is False
    assert d["can_clear"] is True and d["can_delete_conv"] is True


def test_pin_route_roundtrip(tmp_path):
    app, store = _msgops_app(tmp_path)
    _seed(store)
    c = TestClient(app)
    d = c.post("/api/unified-inbox/conversations/pin",
               json={"conversation_id": CID, "pinned": True}).json()
    assert d["ok"] is True and d["pinned"] is True and d["pinned_at"] > 0
    assert store.list_conv_tags_map([CID])[CID]["pinned_at"] > 0
    d2 = c.post("/api/unified-inbox/conversations/pin",
                json={"conversation_id": CID, "pinned": False}).json()
    assert d2["ok"] is True and d2["pinned"] is False
    # 未落库会话：如实 not_found（不建 meta 孤儿行）
    d3 = c.post("/api/unified-inbox/conversations/pin",
                json={"conversation_id": "telegram:x:nope"}).json()
    assert d3["ok"] is False and d3["reason"] == "not_found"


def test_messages_delete_restore_routes(tmp_path):
    app, store = _msgops_app(tmp_path)
    _seed(store)
    mids = _mids(store)
    c = TestClient(app)
    d = c.post("/api/unified-inbox/messages/delete",
               json={"conversation_id": CID, "message_ids": mids[-2:]}).json()
    assert d["ok"] is True and d["deleted"] == 2
    assert len(store.list_recent_messages(CID, limit=50,
                                          include_deleted=False)) == 1
    d2 = c.post("/api/unified-inbox/messages/restore",
                json={"conversation_id": CID, "message_ids": mids[-2:]}).json()
    assert d2["ok"] is True and d2["restored"] == 2
    assert len(store.list_recent_messages(CID, limit=50,
                                          include_deleted=False)) == 3
    # 缺 message_ids → 400
    assert c.post("/api/unified-inbox/messages/delete",
                  json={"conversation_id": CID}).status_code == 400


def test_clear_route_role_gate(tmp_path):
    app, store = _msgops_app(tmp_path, with_session=True)
    _seed(store)

    @app.post("/test-login")
    async def _login(request: Request):
        request.session["role"] = request.query_params.get("role", "agent")
        return {"ok": True}

    c = TestClient(app)
    c.post("/test-login?role=agent")
    assert c.post("/api/unified-inbox/conversations/clear",
                  json={"conversation_id": CID}).status_code == 403
    # meta 对 agent 角色如实回 can_clear=False（前端据此不渲染入口）
    m = c.get("/api/unified-inbox/message-ops/meta").json()
    assert m["can_clear"] is False and m["can_delete_conv"] is False
    c.post("/test-login?role=admin")
    d = c.post("/api/unified-inbox/conversations/clear",
               json={"conversation_id": CID}).json()
    assert d["ok"] is True and d["cleared"] == 3


# ── 4) 双端撤回路由（message-op 扩 telegram/line）──────────────────────────

class _FakeOrch:
    def __init__(self, res=None):
        self.calls = []
        self.res = res if res is not None else {"ok": True, "deleted": 1}

    async def delete_messages(self, platform, account_id, chat_key,
                              message_ids, *, revoke=True):
        self.calls.append((platform, account_id, chat_key,
                           list(message_ids), revoke))
        return self.res


def _acct_app(tmp_path, monkeypatch, orch):
    import src.web.routes.unified_inbox_account_routes as uar
    monkeypatch.setattr(uar, "get_orchestrator", lambda cfg=None: orch)
    app = FastAPI()
    uar.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={}))
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return app, store


def test_revoke_telegram_happy_path(tmp_path, monkeypatch):
    orch = _FakeOrch()
    app, store = _acct_app(tmp_path, monkeypatch, orch)
    _seed(store)
    c = TestClient(app)
    r = c.post("/api/platforms/telegram/8244899900/message-op", json={
        "chat_key": "5433982810", "target_id": "5001", "op": "revoke"})
    d = r.json()
    assert d["ok"] is True and d["revoked"] == 1
    assert orch.calls == [("telegram", "8244899900", "5433982810",
                           ["5001"], True)]
    # M-1 C #219 起：revoked 行不进 AI 口径（默认 include_deleted=True 剔除），UI 口径仍给（灰显）
    assert [m for m in store.list_recent_messages(CID, limit=50)
            if m["platform_msg_id"] == "5001"] == []
    row = [m for m in store.list_recent_messages(CID, limit=50, include_deleted=False)
           if m["platform_msg_id"] == "5001"][0]
    assert row["revoked"] == 1


def test_revoke_rejects_inbound_message(tmp_path, monkeypatch):
    """服务端强制「只撤自己发出的」：入站消息（客户的话）绝不能被撤。"""
    orch = _FakeOrch()
    app, store = _acct_app(tmp_path, monkeypatch, orch)
    _seed(store)
    c = TestClient(app)
    d = c.post("/api/platforms/telegram/8244899900/message-op", json={
        "chat_key": "5433982810", "target_id": "9001", "op": "revoke"}).json()
    assert d["ok"] is False and d["reason"] == "not_own_message"
    assert orch.calls == []            # 编排器一次都没被叫到


def test_revoke_batch_mixed_targets(tmp_path, monkeypatch):
    """批量里混入别人的消息 → 只撤自己的，skipped 如实回传。"""
    orch = _FakeOrch({"ok": True, "deleted": 2})
    app, store = _acct_app(tmp_path, monkeypatch, orch)
    _seed(store)
    c = TestClient(app)
    d = c.post("/api/platforms/telegram/8244899900/message-op", json={
        "chat_key": "5433982810", "target_ids": ["5001", "9001", "5002"],
        "op": "revoke"}).json()
    assert d["ok"] is True and d["revoked"] == 2
    assert d["skipped"] == ["9001"]
    assert orch.calls[0][3] == ["5001", "5002"]


def test_revoke_no_worker_reason_passthrough(tmp_path, monkeypatch):
    orch = _FakeOrch({"ok": False, "reason": "no_worker"})
    app, store = _acct_app(tmp_path, monkeypatch, orch)
    _seed(store)
    c = TestClient(app)
    d = c.post("/api/platforms/telegram/8244899900/message-op", json={
        "chat_key": "5433982810", "target_id": "5001", "op": "revoke"}).json()
    assert d["ok"] is False and d["reason"] == "no_worker"
    row = [m for m in store.list_recent_messages(CID, limit=50)
           if m["platform_msg_id"] == "5001"][0]
    assert not row["revoked"]          # 失败绝不本地装撤回


def test_revoke_unsupported_platform_and_op(tmp_path, monkeypatch):
    orch = _FakeOrch()
    app, store = _acct_app(tmp_path, monkeypatch, orch)
    _seed(store, "line:acct:777")
    c = TestClient(app)
    assert c.post("/api/platforms/messenger/m1/message-op", json={
        "chat_key": "x", "target_id": "1", "op": "revoke",
    }).json()["reason"] == "unsupported_platform"
    assert c.post("/api/platforms/line/acct/message-op", json={
        "chat_key": "777", "target_id": "1", "op": "edit", "text": "hi",
    }).json()["reason"] == "unsupported_op"


# ── 5) worker / 编排器单元 ─────────────────────────────────────────────────

async def test_tg_worker_delete_messages():
    from src.integrations.account_orchestrator import TelegramProtocolWorker

    calls = {}

    class _FakeClient:
        async def delete_messages(self, chat_id, message_ids, revoke=True):
            calls["args"] = (chat_id, list(message_ids), revoke)
            return 2

    w = TelegramProtocolWorker({"account_id": "a"}, {})
    w.client = _FakeClient()
    res = await w.delete_messages("5433982810", ["5001", "5002"], revoke=True)
    assert res == {"ok": True, "deleted": 2}
    assert calls["args"] == (5433982810, [5001, 5002], True)
    # 非数字 id 全部剔除 → bad_ids（不打平台）
    res2 = await w.delete_messages("x", ["abc"])
    assert res2 == {"ok": False, "reason": "bad_ids"}


async def test_line_worker_delete_messages():
    from src.integrations.account_orchestrator import LineProtocolWorker

    sent, boom = [], {"first": True}

    class _FakeLine:
        def unsend_message(self, mid):
            if boom["first"]:
                boom["first"] = False
                raise RuntimeError("expired")
            sent.append(mid)

    w = LineProtocolWorker({"account_id": "l1"}, {})
    w.client = _FakeLine()
    res = await w.delete_messages("777", ["m1", "m2"])
    assert res == {"ok": True, "deleted": 1} and sent == ["m2"]
    boom["first"] = True
    res2 = await w.delete_messages("777", ["m3"])
    assert res2["ok"] is False and "expired" in res2["reason"]


async def test_orchestrator_delete_dispatch():
    from src.integrations.account_orchestrator import (
        AccountOrchestrator, account_key,
    )

    class _W:
        async def delete_messages(self, chat_key, ids, *, revoke=True):
            return {"ok": True, "deleted": len(ids)}

    orch = AccountOrchestrator.__new__(AccountOrchestrator)
    orch._managed = {
        account_key("telegram", "a"): SimpleNamespace(state="running", worker=_W()),
        account_key("telegram", "b"): SimpleNamespace(state="running",
                                                      worker=object()),
    }
    ok = await orch.delete_messages("telegram", "a", "77", ["1", "2"])
    assert ok == {"ok": True, "deleted": 2}
    # worker 无该能力 / 账号不在辖内 → no_worker（不抛）
    miss = await orch.delete_messages("telegram", "b", "77", ["1"])
    assert miss == {"ok": False, "reason": "no_worker"}
    gone = await orch.delete_messages("telegram", "c", "77", ["1"])
    assert gone == {"ok": False, "reason": "no_worker"}


# ── 6) P2：回收站（软删可查可恢复）+ 删除/撤回审计读数 ─────────────────────

def test_list_deleted_messages_store(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    mids = _mids(s)
    s.delete_messages_local(CID, mids[-2:], deleted_by="boss")
    items = s.list_deleted_messages(CID)
    assert len(items) == 2
    assert {r["message_id"] for r in items} == set(mids[-2:])
    assert all(r["deleted_at"] > 0 and r["deleted_by"] == "boss" for r in items)
    # 恢复后回收站清空（误清空的主管级后悔药闭环）
    s.restore_messages_local(CID, mids[-2:])
    assert s.list_deleted_messages(CID) == []


def test_recycle_route_role_gate_and_meta(tmp_path):
    app, store = _msgops_app(tmp_path, with_session=True)
    _seed(store)
    mids = _mids(store)
    store.delete_messages_local(CID, [mids[0]], deleted_by="boss")

    @app.post("/test-login")
    async def _login(request: Request):
        request.session["role"] = request.query_params.get("role", "agent")
        return {"ok": True}

    c = TestClient(app)
    c.post("/test-login?role=agent")
    assert c.get("/api/unified-inbox/messages/deleted",
                 params={"conversation_id": CID}).status_code == 403
    # meta 对 agent 如实回 recycle_bin=False（前端据此不渲染入口）
    assert c.get("/api/unified-inbox/message-ops/meta").json()["recycle_bin"] is False
    c.post("/test-login?role=admin")
    assert c.get("/api/unified-inbox/message-ops/meta").json()["recycle_bin"] is True
    d = c.get("/api/unified-inbox/messages/deleted",
              params={"conversation_id": CID}).json()
    assert d["ok"] is True and len(d["items"]) == 1
    assert d["items"][0]["message_id"] == mids[0]
    assert d["items"][0]["deleted_by"] == "boss"


def test_msg_ops_stats_route(tmp_path):
    from src.ops.ops_events import get_ops_event_store

    oes = get_ops_event_store()          # conftest 已把审计库隔离到 tmp
    assert oes is not None
    oes.record(kind="msg_revoke", platform="telegram", account_id="a",
               reason="ok", detail="cid=telegram:a:1;by=katie;n=2;fail=0")
    oes.record(kind="msg_revoke", platform="telegram", account_id="a",
               reason="no_worker", detail="cid=telegram:a:2;by=katie;n=0;fail=1")
    oes.record(kind="conv_clear", platform="telegram", account_id="a",
               reason="ok", detail="cid=telegram:a:1;by=boss;n=9")
    app, _ = _msgops_app(tmp_path)
    d = TestClient(app).get("/api/admin/msg-ops-stats").json()
    assert d["ok"] is True
    rv = d["counts"]["msg_revoke"]
    assert rv["total"] == 2 and rv["ok"] == 1
    assert rv["by_reason"] == {"no_worker": 1}
    assert d["counts"]["conv_clear"]["total"] == 1
    # recent 的 detail 已服务端结构化（前端不各自拆串）
    ev = [e for e in d["recent"]
          if e["kind"] == "msg_revoke" and e["reason"] == "ok"][0]
    assert ev["cid"] == "telegram:a:1" and ev["by"] == "katie" and ev["n"] == "2"


def test_restore_route_audited(tmp_path):
    from src.ops.ops_events import get_ops_event_store

    app, store = _msgops_app(tmp_path)
    _seed(store)
    mids = _mids(store)
    c = TestClient(app)
    c.post("/api/unified-inbox/messages/delete",
           json={"conversation_id": CID, "message_ids": [mids[0]]})
    c.post("/api/unified-inbox/messages/restore",
           json={"conversation_id": CID, "message_ids": [mids[0]]})
    oes = get_ops_event_store()
    kinds = {e["kind"] for e in
             oes.recent_kinds(["msg_restore", "msg_delete_local"], limit=10)}
    assert kinds == {"msg_restore", "msg_delete_local"}


# ── 7) 双向清空（both_sides：TG 整段历史 delete-for-both）──────────────────

async def test_tg_delete_full_history_paginates():
    """raw DeleteHistory 服务端按 offset 分页——一次调用删不完必须续删到 0。"""
    from src.integrations.telegram_companion_worker import tg_delete_full_history

    calls = []

    class _FakeClient:
        async def resolve_peer(self, target):
            calls.append(("resolve", target))
            return SimpleNamespace(user_id=target)   # 非 InputPeerChannel

        async def invoke(self, fn):
            calls.append(("invoke", fn.max_id, fn.revoke))
            # 首轮回 offset>0（还有剩），次轮 offset=0（清完）
            n = sum(1 for c in calls if c[0] == "invoke")
            return SimpleNamespace(pts_count=40 if n == 1 else 7,
                                   offset=100 if n == 1 else 0)

    res = await tg_delete_full_history(_FakeClient(), "5433982810", revoke=True)
    assert res == {"ok": True, "deleted": 47}
    assert calls[0] == ("resolve", 5433982810)       # 数字 chat_key 转 int
    invokes = [c for c in calls if c[0] == "invoke"]
    assert len(invokes) == 2
    assert all(mid == 0 and rv is True for _, mid, rv in invokes)


async def test_tg_delete_full_history_rejects_channel():
    """超级群/频道是 channels.deleteHistory 语义（对他人无 revoke）→ 如实拒绝，
    绝不静默降级成只删自己。"""
    from pyrogram.raw.types import InputPeerChannel

    from src.integrations.telegram_companion_worker import tg_delete_full_history

    class _FakeClient:
        async def resolve_peer(self, target):
            return InputPeerChannel(channel_id=1, access_hash=1)

        async def invoke(self, fn):                  # pragma: no cover
            raise AssertionError("channel 不该走到 invoke")

    res = await tg_delete_full_history(_FakeClient(), "-1001234")
    assert res == {"ok": False, "reason": "unsupported_chat_type"}


async def test_orchestrator_delete_history_dispatch():
    from src.integrations.account_orchestrator import (
        AccountOrchestrator, account_key,
    )

    class _W:
        async def delete_history(self, chat_key, *, revoke=True):
            return {"ok": True, "deleted": 9}

    orch = AccountOrchestrator.__new__(AccountOrchestrator)
    orch._managed = {
        account_key("telegram", "a"): SimpleNamespace(state="running", worker=_W()),
        account_key("telegram", "b"): SimpleNamespace(state="running",
                                                      worker=object()),
    }
    ok = await orch.delete_history("telegram", "a", "77")
    assert ok == {"ok": True, "deleted": 9}
    # worker 无该能力 / 账号不在辖内 → no_worker（与 delete_messages 同语义）
    assert (await orch.delete_history("telegram", "b", "77"))["reason"] == "no_worker"
    assert (await orch.delete_history("telegram", "c", "77"))["reason"] == "no_worker"


class _FakeHistOrch:
    def __init__(self, res=None):
        self.calls = []
        self.res = res if res is not None else {"ok": True, "deleted": 12}

    async def delete_history(self, platform, account_id, chat_key, *, revoke=True):
        self.calls.append((platform, account_id, chat_key, revoke))
        return self.res


def _msgops_app_with_orch(tmp_path, monkeypatch, orch):
    import src.web.routes.unified_inbox_msgops_routes as umr
    monkeypatch.setattr(umr, "get_orchestrator", lambda cfg=None: orch)
    monkeypatch.setattr(umr, "ensure_builtin_workers", lambda cfg=None: None)
    return _msgops_app(tmp_path)


def test_clear_both_sides_success(tmp_path, monkeypatch):
    from src.ops.ops_events import get_ops_event_store

    orch = _FakeHistOrch()
    app, store = _msgops_app_with_orch(tmp_path, monkeypatch, orch)
    _seed(store)
    d = TestClient(app).post("/api/unified-inbox/conversations/clear",
                             json={"conversation_id": CID,
                                   "both_sides": True}).json()
    assert d["ok"] is True and d["cleared"] == 3
    assert d["both_sides"] is True and d["remote_deleted"] == 12
    assert orch.calls == [("telegram", "8244899900", "5433982810", True)]
    # 本地也清了
    assert store.list_recent_messages(CID, limit=50,
                                      include_deleted=False) == []
    # 审计 conv_clear_remote 成功记账
    evs = get_ops_event_store().recent_kinds(["conv_clear_remote"], limit=5)
    assert evs and evs[0]["reason"] == "ok" and "n=12" in evs[0]["detail"]


def test_clear_both_sides_remote_fail_keeps_local(tmp_path, monkeypatch):
    """顺序不变量：远端失败 → 本地一条不动（半成功=坐席无法察觉的撒谎）。"""
    from src.ops.ops_events import get_ops_event_store

    orch = _FakeHistOrch({"ok": False, "reason": "unsupported_chat_type"})
    app, store = _msgops_app_with_orch(tmp_path, monkeypatch, orch)
    _seed(store)
    d = TestClient(app).post("/api/unified-inbox/conversations/clear",
                             json={"conversation_id": CID,
                                   "both_sides": True}).json()
    assert d["ok"] is False and d["reason"] == "unsupported_chat_type"
    assert len(store.list_recent_messages(CID, limit=50,
                                          include_deleted=False)) == 3
    evs = get_ops_event_store().recent_kinds(["conv_clear_remote"], limit=5)
    assert evs and evs[0]["reason"] == "unsupported_chat_type"
    assert "fail=1" in evs[0]["detail"]


def test_clear_both_sides_unsupported_platform(tmp_path, monkeypatch):
    """非 TG 会话点了 both_sides（旧前端/手打 API）→ 如实拒绝，编排器不被叫。"""
    orch = _FakeHistOrch()
    app, store = _msgops_app_with_orch(tmp_path, monkeypatch, orch)
    line_cid = "line:acct:777"
    _seed(store, line_cid)
    d = TestClient(app).post("/api/unified-inbox/conversations/clear",
                             json={"conversation_id": line_cid,
                                   "both_sides": True}).json()
    assert d["ok"] is False and d["reason"] == "remote_unsupported_platform"
    assert orch.calls == []
    assert len(store.list_recent_messages(line_cid, limit=50,
                                          include_deleted=False)) == 3


def test_clear_without_both_sides_untouched(tmp_path, monkeypatch):
    """默认路径（不带 both_sides）绝不碰编排器——旧行为零漂移。"""
    orch = _FakeHistOrch()
    app, store = _msgops_app_with_orch(tmp_path, monkeypatch, orch)
    _seed(store)
    d = TestClient(app).post("/api/unified-inbox/conversations/clear",
                             json={"conversation_id": CID}).json()
    assert d["ok"] is True and d["cleared"] == 3
    assert d["both_sides"] is False and d["remote_deleted"] == 0
    assert orch.calls == []


def test_meta_advertises_clear_remote(tmp_path):
    app, _ = _msgops_app(tmp_path)
    d = TestClient(app).get("/api/unified-inbox/message-ops/meta").json()
    assert d["clear_remote_platforms"] == {"telegram": True}


def test_revoke_audits_success_and_failure(tmp_path, monkeypatch):
    """成败双记账（P2 前只记成功=失败率观测盲区）：reason='ok' / 机器码。"""
    from src.ops.ops_events import get_ops_event_store

    orch = _FakeOrch()
    app, store = _acct_app(tmp_path, monkeypatch, orch)
    _seed(store)
    c = TestClient(app)
    c.post("/api/platforms/telegram/8244899900/message-op", json={
        "chat_key": "5433982810", "target_id": "5001", "op": "revoke"})
    orch.res = {"ok": False, "reason": "no_worker"}
    c.post("/api/platforms/telegram/8244899900/message-op", json={
        "chat_key": "5433982810", "target_id": "5002", "op": "revoke"})
    oes = get_ops_event_store()
    evs = oes.recent_kinds(["msg_revoke"], limit=10)
    assert sorted(e["reason"] for e in evs) == ["no_worker", "ok"]
    fail_ev = [e for e in evs if e["reason"] == "no_worker"][0]
    assert "n=0" in fail_ev["detail"] and "fail=1" in fail_ev["detail"]
