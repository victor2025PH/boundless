# -*- coding: utf-8 -*-
"""Q-2 F（#263）：commitment_gate 20+10 + A–D 回归钉桩。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_commitment_gate_invite20_photo10():
    from scripts.commitment_gate import INVITE_CASES, PHOTO_CASES, run_gate
    assert len(INVITE_CASES) == 20 and len(PHOTO_CASES) == 10
    res = run_gate()
    assert res["ok"], res


def test_companion_prompt_three_refusal_lines():
    txt = (ROOT / "domains" / "conversion" / "prompts" / "system_companion.txt").read_text(
        encoding="utf-8")
    assert "refuse the first time" in txt
    assert "If they ask again, still refuse" in txt
    assert "Do not explain why you did not do it before" in txt
    assert "never policy" in txt and "no time promise" in txt
    sales = (ROOT / "domains" / "conversion" / "prompts" / "system_prompt.txt").read_text(
        encoding="utf-8")
    assert "REAL-WORLD ASKS" not in sales


def test_ad_coverage_detect_policy_inbound_outbound():
    """A–D 钉桩：五类检测 / 默认 never / 入站处置 / 出站 CLAIM_KINDS 改写。"""
    from src.inbox.commitment_guard import (
        CLAIM_KINDS, apply_claim_rewrites, detect_commitment, handle_inbound,
        meeting_policy_of,
    )
    assert detect_commitment("wanna come over this weekend?", "en") == "meet"
    assert detect_commitment("Just need your address", "en") == "contact"
    assert meeting_policy_of(None)[0] == "never"
    r = handle_inbound("wanna come over Saturday?", lang="en")
    assert r["decision"] == "refuse_sent"
    names = [k for k, _, _ in CLAIM_KINDS]
    assert names == ["commitment_claim", "self_blame_repromise", "media_claim"]
    out, rep = apply_claim_rewrites(
        "Saturday noon sounds lovely, I'll make sure to have some fresh tea ready",
        lang="en")
    assert "commitment_claim" in (rep.get("hits") or [])
    assert "sounds lovely" not in out.lower()
