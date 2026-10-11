"""AI 接回补答（resume_catchup）：接管期间客户最后一句没人回 → 接回时补拟一稿。

钉住：
1. 最后一条是客户入站 → 按当前档位调 auto_draft_cb（skip_companion_yield=True、触发源 takeover_rearm）；
2. 最后一条是出站 / 群 / 过期 / 已有 pending 稿 / manual / 无回调 → 不补；
3. watchdog 自动接回与坐席下拉切回两个入口都接上。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from src.inbox import draft_trigger
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.resume_catchup import MAX_AGE_SEC, catchup_on_resume
from src.inbox.store import InboxStore
from src.inbox.takeover_rearm import record_agent_takeover


def _seed(store, ck="900", *, msgs, chat_type="private"):
    cid = f"telegram:acc1:{ck}"
    last = msgs[-1]
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform="telegram", account_id="acc1",
                          chat_key=ck, display_name=ck, chat_type=chat_type,
                          last_text=last[1], last_ts=last[2]),
        [InboxMessage(conversation_id=cid, direction=d, text=t, ts=ts,
                      platform_msg_id=f"m{i}") for i, (d, t, ts) in enumerate(msgs)],
    )
    return cid


def _state(calls):
    def cb(conv, text, *, skip_companion_yield=False):
        calls.append((conv, text, skip_companion_yield, draft_trigger.peek(conv["conversation_id"])))
    return SimpleNamespace(auto_draft_cb=cb)


def test_dispatches_when_customer_spoke_last(tmp_path):
    store = InboxStore(tmp_path / "i.db")
    now = time.time()
    cid = _seed(store, msgs=[("out", "你好，我是小界", now - 120), ("in", "你们是做什么的", now - 60)])
    calls = []
    res = catchup_on_resume(_state(calls), store, cid, mode="auto_ai", by="test", now=now)
    assert res == {"dispatched": True, "reason": "dispatched"}
    conv, text, skip_yield, reason = calls[0]
    assert conv == {"conversation_id": cid, "platform": "telegram",
                    "account_id": "acc1", "chat_key": "900"}
    assert text == "你们是做什么的" and skip_yield is True and reason == "takeover_rearm"
    draft_trigger._reset_for_tests()
    store.close()


def test_skips_when_not_applicable(tmp_path):
    store = InboxStore(tmp_path / "i.db")
    now = time.time()
    calls = []
    st = _state(calls)
    c_out = _seed(store, "1", msgs=[("in", "在吗", now - 90), ("out", "在的", now - 30)])
    c_grp = _seed(store, "-2", msgs=[("in", "群里有人吗", now - 30)], chat_type="group")
    c_old = _seed(store, "3", msgs=[("in", "昨天的问题", now - MAX_AGE_SEC - 60)])
    c_ok = _seed(store, "4", msgs=[("in", "多少钱", now - 30)])
    assert catchup_on_resume(st, store, c_out, mode="auto_ai", now=now)["reason"] == "last_is_outbound"
    assert catchup_on_resume(st, store, c_grp, mode="auto_ai", now=now)["reason"] == "group"
    assert catchup_on_resume(st, store, c_old, mode="auto_ai", now=now)["reason"] == "stale"
    assert catchup_on_resume(st, store, c_ok, mode="manual", now=now)["reason"] == "not_applicable"
    assert catchup_on_resume(SimpleNamespace(), store, c_ok, mode="auto_ai",
                             now=now)["reason"] == "no_draft_cb"
    assert calls == []
    store.close()


def test_skips_when_pending_draft_exists(tmp_path):
    store = InboxStore(tmp_path / "i.db")
    now = time.time()
    cid = _seed(store, msgs=[("in", "多少钱", now - 30)])
    store.conversations_with_pending_drafts = lambda ids: {cid}
    calls = []
    res = catchup_on_resume(_state(calls), store, cid, mode="review", now=now)
    assert res["reason"] == "has_pending_draft" and calls == []
    store.close()


def test_watchdog_rearm_triggers_catchup(tmp_path):
    from src.inbox.health_watchdog import HealthWatchdog

    store = InboxStore(tmp_path / "i.db")
    now = time.time()
    cid = _seed(store, msgs=[("out", "开场白", now - 3600), ("in", "你是谁", now - 1800)])
    store.set_automation_mode(cid, "auto_ai", source="human")
    record_agent_takeover(store, cid)
    calls = []
    app = SimpleNamespace(state=SimpleNamespace(inbox_store=store, auto_draft_cb=_state(calls).auto_draft_cb))
    cfg = {"inbox": {"takeover_rearm": {"enabled": True, "after_minutes": 30},
                     "auto_draft": {"automation_mode": "auto_ai"}}}
    wd = HealthWatchdog(app=app, config_manager=SimpleNamespace(config=cfg))
    wd._check_takeover_rearm(now=now + 31 * 60)
    assert store.get_automation_mode_meta(cid)["mode"] == "auto_ai"
    assert len(calls) == 1 and calls[0][1] == "你是谁"
    draft_trigger._reset_for_tests()
    store.close()


def test_mode_select_route_catchup_only_from_manual(tmp_path):
    from tests.test_takeover_rearm import _client

    c, store = _client(tmp_path)
    now = time.time()
    cid = _seed(store, msgs=[("out", "开场白", now - 120), ("in", "你是谁", now - 60)])
    calls = []
    c.app.state.auto_draft_cb = _state(calls).auto_draft_cb
    record_agent_takeover(store, cid)
    payload = {"platform": "telegram", "account_id": "acc1", "chat_key": "900", "mode": "auto_ai"}
    r = c.post("/api/unified-inbox/automation", json=payload)
    assert r.status_code == 200
    assert r.json()["catchup"] == {"dispatched": True, "reason": "dispatched"}
    assert len(calls) == 1 and calls[0][1] == "你是谁"
    # 已在全自动再选一次全自动：不是「从人工接回」，不补
    r2 = c.post("/api/unified-inbox/automation", json=payload)
    assert r2.json()["catchup"] is None and len(calls) == 1
    draft_trigger._reset_for_tests()
    store.close()
