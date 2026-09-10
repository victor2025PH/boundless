# -*- coding: utf-8 -*-
"""Q-2 A（#263）：commitment_guard 五类意图 × 三语 + 误伤 + PHBRBW 金标。"""
from __future__ import annotations

import pytest

from src.inbox.commitment_guard import (
    CLAIM_KINDS, detect_commitment, detect_commitment_claim,
    detect_self_blame_repromise, meeting_policy_of, refuse_candidates,
)
from src.inbox.drafts import keyword_risk_hits, keyword_risk_level


# 五类 × 三语 × 2
_HIT = [
    # meet
    ("wanna come over this weekend?", "en", "meet"),
    ("can we meet up Saturday?", "en", "meet"),
    ("要不要出来见面？", "zh", "meet"),
    ("我去你家还是你上门？", "zh", "meet"),
    ("今週末会いたいんだけど", "ja", "meet"),
    ("会いましょうよ？", "ja", "meet"),
    # contact
    ("Just need your address", "en", "contact"),
    ("what's your phone number?", "en", "contact"),
    ("把你地址发我", "zh", "contact"),
    ("加个微信呗，你微信号多少", "zh", "contact"),
    ("住所教えて、届けたい", "ja", "contact"),
    ("電話番号くれる？", "ja", "contact"),
    # media
    ("send me a pic?", "en", "media"),
    ("can we video call tonight?", "en", "media"),
    ("发张照片来看看", "zh", "media"),
    ("打个视频呗", "zh", "media"),
    ("写真送って", "ja", "media"),
    ("ビデオ通話しない？", "ja", "media"),
    # gift
    ("can I send you a gift?", "en", "gift"),
    ("mail you a package, need your address", "en", "gift"),
    ("我想寄给你一个礼物，收货地址给我", "zh", "gift"),
    ("给你寄点东西好不好", "zh", "gift"),
    ("プレゼント送りたいんだけど", "ja", "gift"),
    ("贈り物を送ってもいい？", "ja", "gift"),
    # money
    ("can you send me money on cash app?", "en", "money"),
    ("wire me some money please", "en", "money"),
    ("转账给我一点行不行", "zh", "money"),
    ("借点钱应急，打钱给我", "zh", "money"),
    ("お金貸してくれない？", "ja", "money"),
    ("送金してほしい", "ja", "money"),
]


@pytest.mark.parametrize("text,lang,kind", _HIT)
def test_detect_commitment_five_kinds_trilingual(text, lang, kind):
    assert detect_commitment(text, lang) == kind, (text, detect_commitment(text, lang))


_MISS = [
    "我今天见了老板",
    "I met my boss today",
    "what's your email address",
    "let's have hotpot this weekend",
    "monday is a bank holiday here",
    "you look sexy tonight",
]


@pytest.mark.parametrize("text", _MISS)
def test_false_positives_are_not_commitment(text):
    assert detect_commitment(text) is None, (text, detect_commitment(text))
    assert keyword_risk_level(text) is None, (text, keyword_risk_level(text))


def test_phbrbw_outbound_tea_is_meet_claim():
    """PHBRBW 23:47 起草句：出站答应见面。"""
    t = "Saturday noon sounds lovely, I'll make sure to have some fresh tea ready"
    assert detect_commitment_claim(t) == "meet"
    level, hits = keyword_risk_hits(t)
    assert level == "high"
    assert any("commitment:meet" in h for h in hits)


def test_phbrbw_inbound_address_is_contact():
    assert detect_commitment("Just need your address") == "contact"
    level, hits = keyword_risk_hits("Just need your address")
    assert level == "high"
    assert any("commitment:contact" in h for h in hits)


def test_xbgpbn_self_blame_repromise():
    t = ("Ah, you're right, I completely forgot to send those, my bad. "
         "I'll make sure to grab them for you later today, promise")
    assert detect_self_blame_repromise(t) is True
    assert detect_self_blame_repromise("I promise I'm not a robot") is False


def test_claim_kinds_table_unifies_p3_media():
    names = [k for k, _, _ in CLAIM_KINDS]
    assert names == ["commitment_claim", "self_blame_repromise", "media_claim"]


def test_meeting_policy_defaults_never():
    assert meeting_policy_of(None) == ("never", 3)
    assert meeting_policy_of({}) == ("never", 3)
    assert meeting_policy_of({"boundaries": {}}) == ("never", 3)
    p, n = meeting_policy_of({"boundaries": {"meeting_policy": "after_months:6"}})
    assert (p, n) == ("after_months", 6)
    assert meeting_policy_of({"boundaries": {"meeting_policy": "handoff"}})[0] == "handoff"


def test_refuse_candidates_are_refusals_not_acceptances():
    for kind in ("meet", "contact", "money"):
        cands = refuse_candidates(kind, "en", "soft", n=3)
        assert 2 <= len(cands) <= 3
        blob = " ".join(cands).lower()
        assert "see you saturday" not in blob
        assert "my address is" not in blob
        assert "sounds lovely" not in blob


class _KvStore:
    def __init__(self):
        self.kv, self.mode, self.created_at = {}, "auto_ai", 0.0

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True

    def get_conversation(self, cid):
        return {"created_at": self.created_at}

    def list_recent_messages(self, cid, limit=30):
        return []

    def get_automation_mode(self, cid):
        return self.mode

    def set_automation_mode(self, cid, mode, *, source=""):
        self.mode = mode


def test_handle_inbound_refuse_sent_then_second_insist():
    from src.inbox.commitment_guard import handle_inbound

    store = _KvStore()
    r1 = handle_inbound("wanna come over Saturday?", conversation_id="c1",
                        store=store, lang="en")
    assert r1["decision"] == "refuse_sent" and r1["kind"] == "meet"
    assert r1["text"]
    assert 2 <= len(r1["candidates"]) <= 3
    assert "see you" not in r1["text"].lower()
    r2 = handle_inbound("come over this weekend", conversation_id="c1",
                        store=store, lang="en")
    assert r2["decision"] == "second_insist"
    assert r2["text"]


def test_handle_inbound_handoff_policy_does_not_auto_accept():
    from src.inbox.commitment_guard import handle_inbound

    store = _KvStore()
    r = handle_inbound(
        "Just need your address", conversation_id="c2", store=store, lang="en",
        persona={"boundaries": {"meeting_policy": "handoff"}})
    assert r["decision"] == "handoff" and r["kind"] == "contact"
    assert "address is" not in (r["text"] or "").lower()


def test_handle_inbound_photos_delegate_p3_but_video_still_refused():
    from src.inbox.commitment_guard import handle_inbound

    store = _KvStore()
    persona = {"capabilities": {"photos": True}, "boundaries": {"meeting_policy": "never"}}
    r = handle_inbound("send me a pic?", conversation_id="c3", store=store,
                       persona=persona, photos_ok=True, lang="en")
    assert r["decision"] == "delegate_p3"
    r2 = handle_inbound("can we video call tonight?", conversation_id="c3",
                        store=store, persona=persona, photos_ok=True, lang="en")
    assert r2["decision"] == "refuse_sent" and r2["kind"] == "media"


def test_evaluate_inbound_refuse_sent_skips_risk_hold_handoff_sets():
    """首次 refuse_sent 不挂 risk_hold（否则 Q-3 worker 闸会取消本条 pacing）；
    handoff 才 set。"""
    from src.inbox.commitment_guard import evaluate_inbound
    from src.inbox import risk_hold

    store = _KvStore()
    r1 = evaluate_inbound(store, {"conversation_id": "c-hold"},
                          "wanna come over Saturday?",
                          kind="meet", automation_mode="auto_ai", lang="en")
    assert r1["decision"] == "refuse_sent"
    assert risk_hold.active(store, "c-hold") is None
    store2 = _KvStore()
    r2 = evaluate_inbound(
        store2, {"conversation_id": "c-hf"}, "Just need your address",
        kind="contact", automation_mode="auto_ai", lang="en",
        persona={"boundaries": {"meeting_policy": "handoff"}})
    assert r2["decision"] == "handoff"
    assert (risk_hold.active(store2, "c-hf") or "").startswith("commitment")


def test_evaluate_inbound_second_insist_sets_hold_and_pauses_auto():
    from src.inbox.commitment_guard import evaluate_inbound, handle_inbound
    from src.inbox import risk_hold

    store = _KvStore()
    conv = {"conversation_id": "c-ins"}
    handle_inbound("wanna come over Saturday?", conversation_id="c-ins",
                   store=store, lang="en")
    r = evaluate_inbound(store, conv, "come over this weekend",
                         kind="meet", automation_mode="auto_ai", lang="en")
    assert r["decision"] == "second_insist"
    assert (risk_hold.active(store, "c-ins") or "").startswith("commitment")
    assert store.mode == "review"


def test_drafts_policy_decide_passes_conversation_id():
    src = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "src" / "inbox" / "drafts.py").read_text(encoding="utf-8")
    assert src.count("conversation_id=conv_id, store=self._store") >= 1
    assert "conversation_id=_conv, store=self._store" in src
    assert src.count("conversation_id=str(draft.get(\"conversation_id\") or \"\"), store=self._store") >= 2
    assert "evaluate_inbound" in src and "commitment_alt:" in src
    assert "from .autosend_policy import decide as policy_decide" in src
