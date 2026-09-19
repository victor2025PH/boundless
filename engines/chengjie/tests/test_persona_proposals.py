# -*- coding: utf-8 -*-
"""人设补丁提案纯函数：闸门 / 完善度 fill / 退役钉住 / merge 补丁。"""
from src.utils.persona_manager import PersonaManager, profile_rev
from src.utils.persona_proposals import (
    FILLABLE_FIELDS,
    build_apply_patch,
    collect_drafts,
    collect_fill_drafts,
    collect_retire_drafts,
    is_hard_lock,
    is_writable_proposal,
)


def test_hard_lock_blocks_identity_allows_retire_pin():
    assert is_hard_lock("name")
    assert is_hard_lock("age")
    assert is_hard_lock("gender")
    assert is_hard_lock("identity.deny_ai")
    assert is_hard_lock("boundaries.adult_policy")
    assert is_hard_lock("boundaries.retired_facts", "fill")
    assert not is_hard_lock("boundaries.retired_facts", "retire_pin")
    assert not is_hard_lock("background")
    assert not is_writable_proposal("fill", "age", "fill")
    assert not is_writable_proposal("fill", "name", "fill")
    assert is_writable_proposal("fill", "background", "fill")
    assert is_writable_proposal("retire", "boundaries.retired_facts", "retire_pin")


def test_fill_drafts_skip_name_age_role():
    drafts = collect_fill_drafts({"id": "p", "name": ""})
    fields = {d["field"] for d in drafts}
    assert "name" not in fields
    assert "age" not in fields
    assert "role" not in fields
    assert "background" in fields
    assert "context.specific_memories" in fields
    assert all(d["kind"] == "fill" and d["proposed"] is None for d in drafts)
    assert all(d["field"] in FILLABLE_FIELDS for d in drafts)


def test_rich_persona_has_no_fill_drafts_for_filled_fields():
    persona = {
        "name": "Claire", "role": "designer",
        "personality": {"style": "warm", "traits": ["dry"], "quirks": "coffee",
                        "temperament": "even"},
        "background": "Los Angeles.",
        "context": {"hobbies": ["sewing"], "specific_memories": ["a", "b", "c", "d", "e"],
                    "emotional_triggers": {"positive": ["sun"]}},
        "appearance": "brown hair",
        "tastes": {"likes": ["linen"]},
        "tags": ["female", "30", "american"],
        "names": {"english": "Claire"},
    }
    fields = {d["field"] for d in collect_fill_drafts(persona)}
    assert "background" not in fields
    assert "tastes" not in fields


def test_build_apply_patch_appends_memories_and_does_not_clobber():
    persona = {"context": {"specific_memories": ["old"]}}
    patch = build_apply_patch(persona, "context.specific_memories", "fill", "new")
    assert patch["context"]["specific_memories"] == ["old", "new"]
    merged = PersonaManager.deep_merge_profile(persona, patch)
    assert merged["context"]["specific_memories"] == ["old", "new"]


def test_build_apply_patch_background_appends_when_present():
    persona = {"background": "Born in LA."}
    patch = build_apply_patch(persona, "background", "fill", "Works in apparel.")
    assert patch["background"].startswith("Born in LA.")
    assert "Works in apparel." in patch["background"]


def test_retire_drafts_from_conflicts():
    persona = {
        "name": "X",
        "tastes": {"likes": ["撸猫", "猫咖"]},
        "boundaries": {"retired_facts": [
            {"text": "不再认领猫", "added": "2026-08-04", "terms": ["猫"]},
        ]},
    }
    drafts = collect_retire_drafts(persona)
    assert drafts
    d = drafts[0]
    assert d["kind"] == "retire" and d["action"] == "retire_pin"
    assert d["proposed"]["terms"] == ["猫"]
    patch = build_apply_patch(persona, d["field"], d["action"], d["proposed"])
    texts = [e["text"] for e in patch["boundaries"]["retired_facts"]]
    assert "不再认领猫" in texts
    assert any("猫" in t for t in texts if t != "不再认领猫")


def test_collect_drafts_combines_and_rev_stable():
    persona = {"name": "A"}
    drafts = collect_drafts(persona)
    assert any(d["kind"] == "fill" for d in drafts)
    r1 = profile_rev(persona)
    r2 = profile_rev(persona)
    assert r1 == r2 and r1
