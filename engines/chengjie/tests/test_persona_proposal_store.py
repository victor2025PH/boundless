# -*- coding: utf-8 -*-
from src.utils.persona_proposal_store import PersonaProposalStore


def test_insert_pending_dedupes_and_status_flow():
    s = PersonaProposalStore(":memory:")
    row = {
        "persona_id": "claire",
        "kind": "fill",
        "field": "background",
        "action": "fill",
        "dedupe_key": "fill:background",
        "current": None,
        "proposed": None,
        "rationale": "缺口",
        "confidence": 0.4,
        "evidence": [{"source": "completeness"}],
        "base_rev": "abc",
        "needs_operator": True,
    }
    a = s.insert_pending(row)
    b = s.insert_pending(row)
    assert a and b and a["id"] == b["id"]
    assert s.pending_count("claire") == 1
    s.set_status("claire", a["id"], "rejected", reviewed_by="admin")
    assert s.get("claire", a["id"])["status"] == "rejected"
    assert s.pending_count("claire") == 0
    c = s.insert_pending(row)
    assert c["id"] != a["id"]


def test_mark_stale_outdated():
    s = PersonaProposalStore(":memory:")
    s.insert_pending({
        "persona_id": "p", "kind": "fill", "field": "tastes",
        "action": "fill", "dedupe_key": "fill:tastes", "base_rev": "old",
    })
    assert s.mark_stale_outdated("p", "new") == 1
    assert s.list("p", status="pending") == []
    assert s.list("p", status="stale")[0]["field"] == "tastes"
