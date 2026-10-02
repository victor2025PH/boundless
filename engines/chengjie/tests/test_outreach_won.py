"""同群开口 → 成交归因：目标台账 done(order:/manual:) 挂回开口统计。"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.companion.goals.service import settle_order_ref
from src.companion.goals.store import GoalStore
from src.companion.group_member_outreach import attach_won, note_inbound_reply
from src.companion.group_members_store import (
    OUTREACH_NONE,
    OUTREACH_QUEUED,
    OUTREACH_SENT,
    GroupMembersStore,
    configure_group_members_store,
    reset_group_members_store,
)


def _member(uid, gid="-100", title="跨境群", acct="accA"):
    return {"group_id": gid, "user_id": uid, "username": f"u{uid}", "first_name": f"N{uid}",
            "spoke": True, "is_admin": False, "group_title": title, "source_account_id": acct,
            "hash_account_id": acct, "access_hash": "12345", "extracted_at": 1.0}


def _sent_and_replied(st, uid, now, *, gid="-100", acct="accA", variant="echo", reply=True):
    assert st.cas_outreach(gid, uid, expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                           account_id=acct, now=now)
    st.set_opener(gid, uid, "开场", "ai", variant=variant)
    assert st.cas_outreach(gid, uid, expect_states=(OUTREACH_QUEUED,), new_state=OUTREACH_SENT,
                           account_id=acct, now=now)
    if reply:
        assert note_inbound_reply({"platform": "telegram", "account_id": acct, "chat_key": uid,
                                   "direction": "in", "text": "在", "ts": now + 60}, st) == 1


def _goal(gs, acct, uid, now):
    return gs.create_goal(conversation_id=f"telegram:{acct}:{uid}", platform="telegram",
                          account_id=acct, chat_key=uid, template="acquire_and_convert",
                          deadline_days=10, now=now)


def _seed(now):
    st = GroupMembersStore(":memory:")
    st.record_members([_member("1"), _member("2"), _member("3", gid="-200", title="外贸群"),
                       _member("4")])
    _sent_and_replied(st, "1", now - 3600, variant="echo")
    _sent_and_replied(st, "2", now - 3600, variant="ai_intro")
    _sent_and_replied(st, "3", now - 3600, gid="-200", variant="ask")
    _sent_and_replied(st, "4", now - 3600, reply=False)
    gs = GoalStore(":memory:")
    for uid in ("1", "2", "4"):
        _goal(gs, "accA", uid, now - 3000)
    assert settle_order_ref(gs, ref="telegram:accA:1", order_id="BX-1", plan="team", now=now)["updated"]
    g2 = gs.find_active_goal(conversation_id="telegram:accA:2")
    gs.update_goal_fields(g2["goal_id"], status="done", done_at=now, result="manual:agent")
    assert settle_order_ref(gs, ref="telegram:accA:4", order_id="BX-4", plan="team", now=now)["updated"]
    return st, gs


def test_won_chats_and_contacted_replies_shapes():
    now = time.time()
    st, gs = _seed(now)
    rows = st.contacted_replies(now - 86400, "accA")
    assert {r["user_id"] for r in rows} == {"1", "2", "3"}
    assert all("access_hash" not in r for r in rows)
    assert st.contacted_replies(now - 86400, "accB") == []
    won = gs.won_chats("telegram", ["1", "2", "3", "4", ""])
    assert set(won) == {("accA", "1"), ("accA", "2"), ("accA", "4")}
    assert won[("accA", "2")]["manual"] is True and won[("accA", "1")]["result"].startswith("order:team")
    assert gs.won_chats("telegram", []) == {} and gs.won_chats("whatsapp", ["1"]) == {}


def test_attach_won_funnel_variant_group_and_recent():
    now = time.time()
    st, gs = _seed(now)
    stats = st.outreach_stats(now - 86400, "accA")
    contacted = st.contacted_replies(now - 86400, "accA")
    attach_won(stats, contacted, gs.won_chats("telegram", [m["user_id"] for m in contacted]))
    w = stats["won"]
    # 4 买了但没回过话（不在 contacted）→ 不算开口的功劳
    assert w["total"] == 2 and w["manual"] == 1 and stats["funnel"]["won"] == 2
    assert w["rate"] == pytest.approx(2 / stats["funnel"]["sent"])
    bv = {r["key"]: r["won"] for r in stats["by_variant"]}
    assert bv["echo"] == 1 and bv["ai_intro"] == 1 and bv["ask"] == 0
    assert w["by_group"] == [{"group_id": "-100", "group_title": "跨境群", "replied": 2, "won": 2}]
    assert {x["user_id"] for x in w["recent"]} == {"1", "2"}
    assert "access_hash" not in str(w)


def test_attach_won_ignores_deals_before_outreach():
    contacted = [{"outreach_account_id": "a", "user_id": "9", "outreach_at": 1000.0,
                  "group_id": "g", "group_title": "G", "opener_variant": ""}]
    stats = {"funnel": {"sent": 1}, "by_variant": [{"key": "-", "sent": 1}]}
    attach_won(stats, contacted, {("a", "9"): {"done_at": 500.0, "result": "order:x", "manual": False}})
    assert stats["won"]["total"] == 0 and stats["by_variant"][0]["won"] == 0
    attach_won(stats, contacted, {("a", "9"): {"done_at": 1500.0, "result": "order:x", "manual": False}})
    assert stats["won"]["total"] == 1 and stats["by_variant"][0]["won"] == 1


class _CM:
    config = {"companion": {"group_members": {"enabled": True, "outreach_hours": [0, 24]}}}


@pytest.fixture()
def client_with_goals(monkeypatch):
    reset_group_members_store()
    st = configure_group_members_store(":memory:")
    now = time.time()
    st.record_members([_member("1"), _member("2")])
    _sent_and_replied(st, "1", now - 3600)
    _sent_and_replied(st, "2", now - 3600, variant="ask")
    gs = GoalStore(":memory:")
    _goal(gs, "accA", "1", now - 3000)
    settle_order_ref(gs, ref="telegram:accA:1", order_id="BX-1", plan="team", now=now)
    from src.companion.goals import service as goal_svc
    state = {"on": True}
    monkeypatch.setattr(goal_svc, "goals_enabled", lambda cfg: state["on"])
    monkeypatch.setattr(goal_svc, "get_configured_store", lambda cfg, path=None: gs)
    from src.web.routes.group_members_routes import register_group_members_routes
    app = FastAPI()

    async def _auth():
        return True

    register_group_members_routes(app, auth_dep=_auth, audit_store=None, config_manager=_CM())
    with TestClient(app) as c:
        yield c, state
    reset_group_members_store()


def test_stats_route_reports_won_or_null_when_goals_off(client_with_goals):
    c, state = client_with_goals
    s = c.get("/api/tg-members/outreach/stats?account_id=accA&days=7").json()
    assert s["won"]["total"] == 1 and s["funnel"]["won"] == 1
    assert {r["key"]: r["won"] for r in s["by_variant"]} == {"echo": 1, "ask": 0}
    assert "12345" not in str(s)
    state["on"] = False
    s = c.get("/api/tg-members/outreach/stats?account_id=accA&days=7").json()
    assert s["won"] is None and "won" not in s["funnel"]
