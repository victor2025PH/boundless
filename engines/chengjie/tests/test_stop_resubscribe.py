"""START 重新订阅 + 坐席手动解冻（compliance.resubscribe）回归。

只用临时库，不发任何消息：验证客户本人整句 START 才解冻、只解 STOP 类冻结、
别的账号的停联不动、两条口都在 stop_gate_audit 留痕。
"""
from __future__ import annotations

import pytest

PEER = "tg:user:42"
WA_PEER = "wa:user:15550000001"


@pytest.fixture()
def store(tmp_path):
    from src.inbox.store import InboxStore
    from src.integrations import protocol_bridge
    from src.integrations.shared import official_stop_gate as osg
    st = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: st)
    osg.reset_for_tests()
    yield st
    osg.reset_for_tests()
    protocol_bridge.register_inbox_store_getter(None)


def _audit_actions(store, cid=""):
    from src.inbox.account_blocklist import get_blocklist
    return [r.get("action") for r in get_blocklist(store).audit_rows(conversation_id=cid)]


@pytest.mark.parametrize("text,hit", [
    ("START", "start"), ("start!", "start"), ("/start", "/start"), ("/start@MyBot", "/start"),
    ("/start ref_abc", "/start"), ("Unstop", "unstop"), ("重新订阅", "重新订阅"),
])
def test_resubscribe_hit_whole_message(text, hit):
    from src.compliance.resubscribe import resubscribe_hit
    assert resubscribe_hit(text) == hit


@pytest.mark.parametrize("text", [
    "", "don't start", "start again later?", "when do we start the trial", "/stop", "stop",
    "please start sending me deals every hour of every day from now on okay",
])
def test_resubscribe_hit_rejects_non_whole(text):
    from src.compliance.resubscribe import resubscribe_hit
    assert resubscribe_hit(text) == ""


def test_start_after_stop_unfreezes_and_audits(store):
    from src.inbox.normalizer import conv_id
    from src.inbox.stop_contact import frozen_reason
    from src.integrations.shared import official_stop_gate as osg
    g = osg.inbound_gate("telegram", "bot1", PEER, "STOP", store=store)
    assert g["action"] == "stopped"
    assert osg.is_stopped("telegram", "bot1", PEER, store=store)
    g2 = osg.inbound_gate("telegram", "bot1", PEER, "/start", store=store)
    assert g2["action"] == "resubscribed"
    assert g2["still_stopped"] == ""
    cid = conv_id("telegram", "bot1", PEER)
    assert frozen_reason(store, cid) == ""
    assert osg.is_stopped("telegram", "bot1", PEER, store=store) == ""
    assert not osg.memory_stopped("telegram", "bot1", PEER)
    assert osg.outbound_gate("telegram", "bot1", PEER, text="hi", store=store) == (False, "")
    assert "resubscribed" in _audit_actions(store, cid)
    # 之后的普通消息照常放行
    assert osg.inbound_gate("telegram", "bot1", PEER, "hello", store=store)["action"] == "pass"


def test_start_when_not_stopped_is_plain_pass(store):
    from src.integrations.shared import official_stop_gate as osg
    assert osg.inbound_gate("telegram", "bot1", PEER, "/start", store=store)["action"] == "pass"
    assert "resubscribed" not in _audit_actions(store)


def test_start_disabled_keeps_frozen(store):
    from src.integrations.shared import official_stop_gate as osg
    osg.inbound_gate("whatsapp", "pn1", WA_PEER, "STOP", store=store)
    g = osg.inbound_gate("whatsapp", "pn1", WA_PEER, "START", store=store, allow_resubscribe=False)
    assert g["action"] == "frozen"
    assert osg.is_stopped("whatsapp", "pn1", WA_PEER, store=store)


def test_start_does_not_lift_other_freeze_reasons(store):
    from src.compliance.resubscribe import resubscribe
    from src.inbox.normalizer import conv_id
    from src.inbox.stop_contact import freeze_conversation, frozen_reason
    cid = conv_id("whatsapp", "pn1", WA_PEER)
    freeze_conversation(store, platform="whatsapp", account_id="pn1", chat_key=WA_PEER,
                        reason="self_harm", hits=["x"])
    was = frozen_reason(store, cid)
    if not was or was == "stop_contact":
        pytest.skip("freeze_conversation 不支持非 STOP 原因的冻结形态")
    r = resubscribe(store, platform="whatsapp", account_id="pn1", peer=WA_PEER, text="START")
    assert r["action"] == "refused"
    assert frozen_reason(store, cid) == was
    assert "resub_refused" in _audit_actions(store, cid)


def test_start_only_lifts_this_account(store):
    from src.integrations.shared import official_stop_gate as osg
    osg.inbound_gate("whatsapp", "pnA", WA_PEER, "STOP", store=store)
    osg.inbound_gate("whatsapp", "pnB", WA_PEER, "STOP", store=store)
    g = osg.inbound_gate("whatsapp", "pnA", WA_PEER, "START", store=store)
    assert g["action"] == "resubscribed"
    # 同一客户在 pnB 上的停联仍在 → 统一闸跨账号视角如实报告、出站仍拦
    assert g["still_stopped"] in ("blocklist_peer", "blocklist_phone")
    assert osg.is_stopped("whatsapp", "pnB", WA_PEER, store=store)


def test_agent_unfreeze_requires_actor_and_note(store):
    from src.compliance.resubscribe import agent_unfreeze
    from src.inbox.normalizer import conv_id
    from src.integrations.shared import official_stop_gate as osg
    osg.inbound_gate("telegram", "bot1", PEER, "STOP", store=store)
    cid = conv_id("telegram", "bot1", PEER)
    assert agent_unfreeze(store, cid, actor="", note="x")["error"] == "actor_required"
    assert agent_unfreeze(store, cid, actor="agent:a", note="  ")["error"] == "note_required"
    assert agent_unfreeze(store, "bad", actor="agent:a", note="x")["error"] == "bad_conversation"
    assert osg.is_stopped("telegram", "bot1", PEER, store=store)


def test_agent_unfreeze_unfreezes_and_audits(store):
    from src.compliance.resubscribe import agent_unfreeze
    from src.inbox.account_blocklist import get_blocklist
    from src.inbox.normalizer import conv_id
    from src.integrations.shared import official_stop_gate as osg
    osg.inbound_gate("telegram", "bot1", PEER, "STOP", store=store)
    cid = conv_id("telegram", "bot1", PEER)
    r = agent_unfreeze(store, cid, actor="agent:alice", note="customer re-consented by phone")
    assert r["ok"] is True
    assert osg.is_stopped("telegram", "bot1", PEER, store=store) == ""
    rows = [x for x in get_blocklist(store).audit_rows(conversation_id=cid) if x.get("action") == "unfrozen"]
    assert rows and rows[0]["path"].startswith("agent_unfreeze:agent:alice")
    assert rows[0]["hit"] == "customer re-consented by phone"[:40]
    # 再解一次：已不在冻结态
    assert agent_unfreeze(store, cid, actor="agent:alice", note="again")["error"] == "not_frozen"


def test_official_callers_pass_allow_resubscribe_switch():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "integrations"
    for name in ("telegram_bot_official.py", "whatsapp_cloud.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "allow_resubscribe=" in src, name
