# -*- coding: utf-8 -*-
"""十三期：转派实时通知事件（conversation_assigned）。"""


def test_assign_publishes_event(app, client, config_dir, tmp_path):
    from src.inbox.models import InboxConversation
    from src.inbox.store import InboxStore
    from src.integrations.shared.event_bus import get_event_bus
    from src.utils.web_user_store import ROLE_AGENT, WebUserStore

    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="tg:a:9", platform="telegram",
        account_id="a", chat_key="9", display_name="客户9", last_ts=1000.0,
    ))
    app.state.inbox_store = store

    ustore = WebUserStore(config_dir / "web_users.db")
    if not ustore.get_user("op_carol"):
        ustore.create_user("op_carol", "pw-123456", ROLE_AGENT)
    client.get("/login")
    client.post("/login", data={"username": "op_carol", "password": "pw-123456"},
                follow_redirects=True)
    client.headers.update({"X-CSRF-Token": client.cookies.get("csrf_token", "")})

    r = client.post("/api/workspace/batch/assign",
                    json={"conversation_ids": ["tg:a:9"], "agent_id": "amy"})
    assert r.status_code == 200 and r.json().get("ok") is True, r.text[:200]

    evts = [e for e in get_event_bus().recent_events(limit=30)
            if e.get("type") == "conversation_assigned"]
    assert evts, "缺 conversation_assigned 事件"
    d = evts[-1]["data"]
    assert d.get("to_agent") == "amy"
    assert d.get("count") == 1
    assert d.get("conversation_id") == "tg:a:9"


def test_assigned_event_registered_for_sse_and_notif():
    from src.web.routes.unified_inbox_realtime_routes import (
        _NOTIF_EVENT_TYPES,
        _SSE_EVENT_TYPES,
    )
    assert "conversation_assigned" in _SSE_EVENT_TYPES
    assert "conversation_assigned" in _NOTIF_EVENT_TYPES
