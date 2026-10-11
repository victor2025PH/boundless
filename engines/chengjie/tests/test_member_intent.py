"""群成员意向分：人设关键词 → 排队先后 + 排除词不开口。"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.companion.group_member_opener import build_opener_context
from src.companion.group_member_outreach import (
    OutreachPolicy,
    build_preview,
    enqueue_today,
    select_candidates,
)
from src.companion.group_members_store import OUTREACH_QUEUED, GroupMembersStore
from src.companion.member_intent import (
    INTENT_MAX,
    INTENT_NEGATIVE,
    intent_score,
    intent_terms,
)

POLICY = OutreachPolicy(cap=8, min_gap_sec=1500)
TERMS = {"positive": ["客服", "自动回复", "translate"], "negative": ["博彩"]}


def _row(**kw):
    base = {
        "group_id": "-100", "user_id": "1", "username": "neo", "first_name": "Neo",
        "last_name": "", "is_admin": False, "is_bot": False, "spoke": True,
        "last_spoke_ts": 100.0, "group_title": "G", "source_account_id": "accA",
        "hash_account_id": "accA", "access_hash": "555", "score": 90,
        "outreach_state": "none", "extracted_at": 1.0, "last_msg_text": "",
    }
    base.update(kw)
    return base


def test_intent_score_hits_cap_and_negative():
    assert intent_score("客服太累了，想找个自动回复", TERMS) == (50, ["客服", "自动回复"])
    assert intent_score("Can it TRANSLATE?", TERMS) == (25, ["translate"])
    assert intent_score("今天天气不错", TERMS) == (0, [])
    assert intent_score("博彩客服招人", TERMS) == (INTENT_NEGATIVE, ["博彩"])
    many = {"positive": list("abcdef"), "negative": []}
    assert intent_score("abcdef", many)[0] == INTENT_MAX
    assert intent_score("", TERMS) == (0, []) and intent_score("客服", None) == (0, [])


def test_intent_terms_from_persona_and_config():
    persona = {"sales": {"intent_keywords": ["客服", " Auto  Reply ", "客服"],
                         "intent_negative": "博彩，菠菜"}}
    cfg = {"companion": {"group_members": {"intent_keywords": ["翻译"]}}}
    t = intent_terms(persona, cfg)
    assert t == {"positive": ["客服", "auto reply", "翻译"], "negative": ["博彩", "菠菜"]}
    assert intent_terms(None, None) == {"positive": [], "negative": []}
    assert intent_terms({"sales": "bad"}, {"companion": "bad"}) == {"positive": [], "negative": []}


def test_select_candidates_ranks_intent_first_and_skips_negative():
    members = [
        _row(user_id="1", last_msg_text="早上好", last_spoke_ts=999.0),
        _row(user_id="2", last_msg_text="客服回不过来", last_spoke_ts=1.0, username=""),
        _row(user_id="3", last_msg_text="客服+自动回复怎么搞", last_spoke_ts=2.0),
        _row(user_id="4", last_msg_text="博彩推广找客服", last_spoke_ts=1000.0),
    ]
    got = [m["user_id"] for m in select_candidates(
        members, account_id="accA", slots=10, touched=set(), intent=TERMS)]
    assert got == ["3", "2", "1"]
    plain = [m["user_id"] for m in select_candidates(
        members, account_id="accA", slots=10, touched=set())]
    assert plain[0] == "4" and set(plain) == {"1", "2", "3", "4"}


def test_preview_and_enqueue_carry_intent():
    st = GroupMembersStore(":memory:")
    now = 2_000_000.0
    st.record_members([_row(user_id="1", last_msg_text="闲聊"),
                       _row(user_id="2", last_msg_text="想要客服自动回复"),
                       _row(user_id="3", last_msg_text="博彩")])
    pv = build_preview(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=30,
                       intent=TERMS)
    assert pv["intent_configured"] is True
    assert [c["user_id"] for c in pv["candidates"]] == ["2", "1"]
    assert pv["candidates"][0]["intent"] == 50 and pv["candidates"][0]["intent_hits"] == ["客服", "自动回复"]
    assert "access_hash" not in pv["candidates"][0]
    r = enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=OutreachPolicy(cap=5),
                      age_days=0, intent=TERMS)
    assert r["ok"]
    assert st.get_member("-100", "2")["outreach_state"] == OUTREACH_QUEUED
    assert st.get_member("-100", "3")["outreach_state"] == "none"
    pv2 = build_preview(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=30)
    assert pv2["intent_configured"] is False


def test_opener_context_exposes_persona_intent(monkeypatch):
    import src.ai.persona_voice as pv
    import src.utils.persona_manager as pm_mod

    persona = {"id": "s", "name": "S", "sales": {"intent_keywords": ["客服"]}}
    monkeypatch.setattr(pv, "resolve_effective_persona", lambda *a, **k: ("s", ""))

    class _PM:
        def get_persona_by_id(self, pid):
            return persona

        def normalize_profile_shape(self, p):
            return p

        def resolve_spoken_name(self, p):
            return p.get("name")

    monkeypatch.setattr(pm_mod.PersonaManager, "get_instance", classmethod(lambda cls: _PM()))
    ctx = build_opener_context({"companion": {"group_members": {"intent_negative": ["博彩"]}}},
                               "telegram", "accA")
    assert ctx["intent"] == {"positive": ["客服"], "negative": ["博彩"]}


def test_wujie_sales_pack_has_intent_keywords():
    p = Path(__file__).parent.parent / "config" / "persona_packs" / "wujie_sales.yaml"
    persona = yaml.safe_load(p.read_text(encoding="utf-8"))["persona"]
    t = intent_terms(persona)
    assert len(t["positive"]) >= 20 and "客服" in t["positive"] and "博彩" in t["negative"]
    assert intent_score("我们客服每天消息太多回不过来", t)[0] >= 50
