"""P0-协作闭环：@提及进通知历史（离线补看）+ 读取侧按人过滤。

链路：conv_note 进 SSE 通知队列白名单（realtime routes）→ 队列全员共享 →
GET /api/workspace/notifications 只回「@ 了当前坐席」的 conv_note（别人的提及
不出现在我的铃铛历史）；其余类型（inbox_message 等）不受过滤影响。
身份候选集兼容两套历史口径（user_id||username 与 user_name||username）。
"""

import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.web.routes.unified_inbox_batch_notif_routes import (
    _mention_targets_me,
    _session_identities,
    register_batch_notif_routes,
)
from src.web.routes.unified_inbox_realtime_routes import _NOTIF_EVENT_TYPES


def _evt(etype, data):
    return {"type": etype, "data": data, "_notif_ts": int(time.time() * 1000)}


def _build():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    def api_auth(request: Request):
        return True

    register_batch_notif_routes(app, api_auth=api_auth)

    @app.post("/__login")
    async def _login(request: Request):
        body = await request.json()
        request.session.clear()
        for k, v in body.items():
            request.session[k] = v
        return {"ok": True}

    app.state.notif_queue = [
        _evt("inbox_message", {"conversation_id": "c1", "preview": "hi"}),
        _evt("conv_note", {
            "conversation_id": "c1", "note_id": "n-alice",
            "agent_name": "Bob", "body": "@alice 看下这单",
            "mentions": ["alice"],
        }),
        _evt("conv_note", {
            "conversation_id": "c2", "note_id": "n-bob",
            "agent_name": "Alice", "body": "@bob 交接给你",
            "mentions": ["bob"],
        }),
    ]
    return TestClient(app)


def test_conv_note_is_in_notif_queue_whitelist():
    # SSE 写队列白名单必须包含 conv_note，否则离线坐席永远补不到提及
    assert "conv_note" in _NOTIF_EVENT_TYPES


def test_history_filters_mentions_per_agent():
    client = _build()

    client.post("/__login", json={"username": "alice"})
    d = client.get("/api/workspace/notifications").json()
    types = [(n["type"], (n.get("data") or {}).get("note_id")) for n in d["notifications"]]
    assert ("inbox_message", None) == (types[0][0], None) or any(
        t == "inbox_message" for t, _ in types
    )
    note_ids = {nid for t, nid in types if t == "conv_note"}
    assert note_ids == {"n-alice"}, f"alice 只应看到 @她 的那条，实际 {note_ids}"

    client.post("/__login", json={"username": "bob"})
    d = client.get("/api/workspace/notifications").json()
    note_ids = {
        (n.get("data") or {}).get("note_id")
        for n in d["notifications"] if n["type"] == "conv_note"
    }
    assert note_ids == {"n-bob"}


def test_history_without_identity_hides_all_mentions():
    client = _build()
    client.post("/__login", json={})  # 空 session（无身份）
    d = client.get("/api/workspace/notifications").json()
    assert all(n["type"] != "conv_note" for n in d["notifications"])
    # 非 conv_note 类型不受影响
    assert any(n["type"] == "inbox_message" for n in d["notifications"])


def test_identity_set_covers_both_conventions():
    """user_id（presence/@建议口径）与 username（注解作者口径）任一命中都算 @ 到我。"""
    class _Req:
        def __init__(self, sess):
            self.scope = {"session": sess}
            self.session = sess

    req = _Req({"user_id": 7, "username": "alice", "display_name": "阿丽"})
    ids = _session_identities(req)
    assert {"7", "alice", "阿丽"} <= ids

    evt_by_uid = {"data": {"mentions": ["7"]}}
    evt_by_name = {"data": {"mentions": ["alice"]}}
    evt_other = {"data": {"mentions": ["bob"]}}
    assert _mention_targets_me(evt_by_uid, ids)
    assert _mention_targets_me(evt_by_name, ids)
    assert not _mention_targets_me(evt_other, ids)
    assert not _mention_targets_me(evt_other, set())
