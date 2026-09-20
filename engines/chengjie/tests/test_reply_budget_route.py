# -*- coding: utf-8 -*-
"""预算横幅/救济路由薄测试（peer_bot_guard P2，2026-08-04）。

守卫与台账的行为门禁在 test_peer_bot_guard_budget.py；这里只钉路由契约：
GET /automation 捎带 budget 段（前端横幅数据源）、POST relief 当日生效、
无持久层如实 503。夹具复用 stage2 的最小 app 构造。
"""
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.peer_bot_guard import today_key
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def _client(inbox_store=None, config=None):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth, templates=_Templates())
    if inbox_store is not None:
        app.state.inbox_store = inbox_store
    if config is not None:
        app.state.config_manager = SimpleNamespace(config=config)
    return TestClient(app)


def _guard_cfg(budget=2):
    return {"inbox": {"peer_bot_guard": {
        "enabled": True, "daily_reply_budget": budget}}}


def test_automation_get_carries_budget_disabled_when_guard_off(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store)   # 无 config_manager → 守卫视为关
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              "&account_id=a&chat_key=k")
    assert r.status_code == 200
    body = r.json()
    assert "budget" in body
    assert body["budget"] and body["budget"]["enabled"] is False
    store.close()


def test_budget_exhausted_then_relief_roundtrip(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store, config=_guard_cfg(budget=2))
    cid = "telegram:a:k"
    day = today_key()
    store.bump_auto_reply(cid, day)
    store.bump_auto_reply(cid, day)
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              "&account_id=a&chat_key=k")
    b = r.json()["budget"]
    assert b["enabled"] and b["used"] == 2 and b["exhausted"]
    # 救济：当日解除
    r2 = c.post("/api/unified-inbox/reply-budget/relief", json={
        "platform": "telegram", "account_id": "a", "chat_key": "k"})
    assert r2.status_code == 200
    b2 = r2.json()["budget"]
    assert b2["relieved"] and not b2["exhausted"]
    # GET 复读同一状态（横幅消失的数据源）
    r3 = c.get("/api/unified-inbox/automation?platform=telegram"
               "&account_id=a&chat_key=k")
    assert r3.json()["budget"]["exhausted"] is False
    store.close()


def test_relief_revoke_roundtrip(tmp_path):
    """P1 后悔药：同一端点 ``revoke:true`` 撤销今日豁免，预算判定立即恢复。"""
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store, config=_guard_cfg(budget=2))
    cid = "telegram:a:k"
    day = today_key()
    store.bump_auto_reply(cid, day)
    store.bump_auto_reply(cid, day)
    c.post("/api/unified-inbox/reply-budget/relief", json={
        "platform": "telegram", "account_id": "a", "chat_key": "k"})
    assert store.get_auto_reply_ledger(cid)["relief_day"] == day
    r = c.post("/api/unified-inbox/reply-budget/relief", json={
        "platform": "telegram", "account_id": "a", "chat_key": "k",
        "revoke": True})
    assert r.status_code == 200
    b = r.json()["budget"]
    # 撤销后：豁免消失、触顶恢复、计数未被动（观测口径保留）
    assert not b["relieved"] and b["exhausted"] and b["used"] == 2
    store.close()


def test_relief_requires_store():
    c = _client(inbox_store=None, config=_guard_cfg())
    r = c.post("/api/unified-inbox/reply-budget/relief", json={
        "platform": "telegram", "account_id": "a", "chat_key": "k"})
    assert r.status_code == 503


def test_relief_validates_params(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store, config=_guard_cfg())
    r = c.post("/api/unified-inbox/reply-budget/relief", json={
        "platform": "", "chat_key": ""})
    assert r.status_code == 400
    store.close()


# ── P1-198：账号在线状态随 /automation 捎带（会话头「账号已停用」红条数据源） ──

def test_automation_get_carries_account_status(tmp_path):
    """注册表有该号 → account 段带 status/mode；无该号 → account=None。

    198 事故第二课回归钉：账号被手动停用（status=offline）后会话界面
    此前零痕迹——现在开会话即可见红条。
    """
    from src.integrations.account_registry import get_account_registry
    reg = get_account_registry()   # conftest 已隔离到临时库
    reg.upsert("telegram", "acct-off", mode="protocol", status="offline")
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store)
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              "&account_id=acct-off&chat_key=k")
    assert r.status_code == 200
    acct = r.json().get("account")
    assert acct and acct["status"] == "offline" and acct["mode"] == "protocol"
    # 注册表没有的账号（主协议号/纯 RPA）→ None，前端不显横幅
    r2 = c.get("/api/unified-inbox/automation?platform=telegram"
               "&account_id=nobody&chat_key=k")
    assert r2.json().get("account") is None
    store.close()
