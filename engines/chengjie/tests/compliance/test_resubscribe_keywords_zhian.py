"""START 重新订阅缺省词表收紧（智安 2026-10-08，蛋博士拍板）。

单独的「订阅」「subscribe」「/subscribe」不再解冻已停联会话——客户问产品订阅和重新同意接收分不开，
误解冻 = 停联后继续发（红线①）。只留整句 START / /start 与明确表示恢复接收的说法。只用临时库，不发消息。
"""
from __future__ import annotations

import pytest

PEER = "tg:user:77"
WA_PEER = "wa:user:15550000077"


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


@pytest.mark.parametrize("text", [
    "订阅", "订阅？", "订阅!", "subscribe", "Subscribe.", "SUBSCRIBE", "/subscribe", "/subscribe@MyBot",
    "/subscribe ref_x",
])
def test_ambiguous_subscribe_is_not_resubscribe(text):
    from src.compliance.resubscribe import resubscribe_hit
    assert resubscribe_hit(text) == ""


@pytest.mark.parametrize("text,hit", [
    ("START", "start"), ("/start", "/start"), ("/start@MyBot", "/start"), ("Unstop", "unstop"),
    ("resubscribe", "resubscribe"), ("/resubscribe", "/resubscribe"), ("yes, start", "yes start"),
    ("重新订阅", "重新订阅"), ("恢复订阅。", "恢复订阅"), ("重新开始接收", "重新开始接收"),
])
def test_explicit_resubscribe_still_hits(text, hit):
    from src.compliance.resubscribe import resubscribe_hit
    assert resubscribe_hit(text) == hit


def test_default_table_never_contains_ambiguous_words():
    from src.compliance.resubscribe import AMBIGUOUS_NOT_RESUBSCRIBE, DEFAULT_RESUBSCRIBE_KEYWORDS
    kws = {k.strip().lower() for k in DEFAULT_RESUBSCRIBE_KEYWORDS}
    for w in AMBIGUOUS_NOT_RESUBSCRIBE:
        assert w not in kws, w
    assert "start" in kws and "/start" in kws


@pytest.mark.parametrize("platform,acct,peer,word", [
    ("telegram", "bot1", PEER, "/subscribe"),
    ("telegram", "bot1", PEER, "订阅"),
    ("whatsapp", "pn1", WA_PEER, "subscribe"),
])
def test_stopped_conversation_stays_frozen_on_ambiguous_word(store, platform, acct, peer, word):
    from src.integrations.shared import official_stop_gate as osg
    assert osg.inbound_gate(platform, acct, peer, "STOP", store=store)["action"] == "stopped"
    g = osg.inbound_gate(platform, acct, peer, word, store=store)
    assert g["action"] == "frozen", g
    assert osg.is_stopped(platform, acct, peer, store=store)
    blocked, _ = osg.outbound_gate(platform, acct, peer, text="hi", store=store)
    assert blocked is True


def test_resubscribe_api_returns_none_for_ambiguous(store):
    from src.compliance.resubscribe import resubscribe
    from src.integrations.shared import official_stop_gate as osg
    osg.inbound_gate("whatsapp", "pn1", WA_PEER, "STOP", store=store)
    r = resubscribe(store, platform="whatsapp", account_id="pn1", peer=WA_PEER, text="订阅")
    assert r["action"] == "none"
    assert osg.is_stopped("whatsapp", "pn1", WA_PEER, store=store)
