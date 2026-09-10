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
