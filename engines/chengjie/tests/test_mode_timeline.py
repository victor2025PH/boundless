# -*- coding: utf-8 -*-
"""档位变更时间线门禁（P2 2026-08-09）。

conversation_settings 只存最新态，「谁在什么时候把档位改成什么」此前不可追溯
（.198 接管钉死 27h 的排障只能靠猜）。automation_mode_log 只记**真实跃迁**：

1. mode/source 变了才落行——接管重刷时间戳、bootstrap 幂等重写零历史噪音
   （否则坐席每发一条手动消息就多一行，表被灌爆）；
2. bulk 直写路径同步补记；
3. 读侧新→旧 + limit；why-no-reply 响应携带 mode_history 段。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.inbox.takeover_rearm import record_agent_takeover, sweep_takeover_rearm

_REARM_ON = {"inbox": {
    "takeover_rearm": {"enabled": True, "after_minutes": 30},
    "auto_draft": {"automation_mode": "auto_ai"},
}}


def _log(store, cid):
    return store.list_automation_mode_log(cid)


def test_transitions_logged_change_only(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:1"
    store.set_automation_mode(cid, "auto_ai", source="human")
    assert len(_log(store, cid)) == 1
    # 同 mode 同 source 幂等重写 → 不落行
    store.set_automation_mode(cid, "auto_ai", source="human")
    assert len(_log(store, cid)) == 1
    # 接管 → 落行（manual + takeover_from:auto_ai）
    record_agent_takeover(store, cid)
    rows = _log(store, cid)
    assert len(rows) == 2
    assert rows[0]["mode"] == "manual"
    assert rows[0]["source"] == "takeover_from:auto_ai"
    assert rows[0]["prev_mode"] == "auto_ai"
    # 连续接管只刷新时间戳（mode/source 均不变）→ 不落行
    record_agent_takeover(store, cid)
    assert len(_log(store, cid)) == 2
    store.close()


def test_rearm_transition_logged(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:2"
    store.set_automation_mode(cid, "auto_ai", source="human")
    record_agent_takeover(store, cid)
    with store._lock:
        store._conn.execute(
            "UPDATE conversation_settings SET updated_at=? WHERE conversation_id=?",
            (time.time() - 3600, cid))
        store._conn.commit()
    sweep_takeover_rearm(store, _REARM_ON)
    rows = _log(store, cid)
    assert rows[0]["mode"] == "auto_ai" and rows[0]["source"] == "rearm"
    assert rows[0]["prev_mode"] == "manual"
    # 完整故事线：human 开自动 → 接管转手动 → 自动接回（新→旧）
    assert [r["source"] for r in rows] == ["rearm", "takeover_from:auto_ai", "human"]
    store.close()


def test_bulk_downgrade_logged(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    for i in range(3):
        store.set_automation_mode(f"c:{i}", "auto_ai", source="human")
    store.bulk_set_automation_mode("auto_ai", "review", source="bulk")
    for i in range(3):
        rows = _log(store, f"c:{i}")
        assert rows[0]["mode"] == "review" and rows[0]["source"] == "bulk"
        assert rows[0]["prev_mode"] == "auto_ai"
    store.close()


def test_log_order_and_limit(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:3"
    modes = ["auto_ai", "review", "manual", "auto_ai"]
    for m in modes:
        store.set_automation_mode(cid, m, source="human")
    rows = store.list_automation_mode_log(cid, limit=2)
    assert len(rows) == 2
    assert rows[0]["mode"] == "auto_ai" and rows[1]["mode"] == "manual"
    store.close()


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def test_why_no_reply_carries_mode_history(tmp_path):
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

    app = FastAPI()
    register_unified_inbox_routes(
        app, page_auth=lambda request: True, api_auth=lambda request: True,
        templates=_Templates())
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    app.state.config_manager = SimpleNamespace(config=_REARM_ON)
    c = TestClient(app)
    cid = "telegram:a:400"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key="400", chat_type="private", display_name="N"))
    store.set_automation_mode(cid, "auto_ai", source="human")
    record_agent_takeover(store, cid)
    r = c.get("/api/unified-inbox/why-no-reply?platform=telegram"
              "&account_id=a&chat_key=400")
    body = r.json()
    hist = body.get("mode_history") or []
    assert len(hist) == 2
    assert hist[0]["source"].startswith("takeover")
    assert hist[1]["source"] == "human"
    store.close()
