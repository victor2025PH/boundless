# -*- coding: utf-8 -*-
"""#91-A 幻觉断言轨门禁（实施90 二批）：回忆类断言接地锁的安全不变量。

与 crisis/persona 评测同族——纯函数常驻门禁：现编断言召回必须 100%、
合法回忆零误伤；探测器有效性自证（篡改守卫必 FAIL，评测不是摆设）。
"""
from __future__ import annotations

from src.eval.recall_claim_eval import _GOLDEN, evaluate_recall_claim_guard


def test_recall_claim_eval_all_green():
    out = evaluate_recall_claim_guard()
    assert out["passed"], out["failures"]
    assert out["block_recall"] >= 1.0
    assert out["false_alarms"] == 0


def test_recall_claim_eval_detects_broken_guard(monkeypatch):
    """探测器自证：守卫被废（恒放行）时评测必须 FAIL。"""
    import src.ai.outbound_text_guard as g

    monkeypatch.setattr(
        g, "strip_hallucinated_recall",
        lambda text, **kw: (text, []))
    out = evaluate_recall_claim_guard()
    assert not out["passed"]
    assert any(f["kind"] == "missed" for f in out["failures"])


def test_golden_covers_both_directions():
    """金标必须两面都有（只有必拦例=测不出误伤；只有放行例=测不出漏拦）。"""
    kinds = {bool(r["expect_block"]) for r in _GOLDEN}
    assert kinds == {True, False}
    assert sum(1 for r in _GOLDEN if r["expect_block"]) >= 3
    assert sum(1 for r in _GOLDEN if not r["expect_block"]) >= 3


# ── #110 共同经历叙事锁（实施91，0831 原图 880 四连实锤）────────────────────

def test_shared_past_eval_all_green():
    from src.eval.recall_claim_eval import evaluate_shared_past_guard
    out = evaluate_shared_past_guard()
    assert out["passed"], out["failures"]
    assert out["block_recall"] >= 1.0
    assert out["false_alarms"] == 0


def test_shared_past_eval_detects_broken_guard(monkeypatch):
    import src.ai.outbound_text_guard as g
    from src.eval.recall_claim_eval import evaluate_shared_past_guard

    monkeypatch.setattr(
        g, "strip_ungrounded_shared_past",
        lambda text, **kw: (text, []))
    out = evaluate_shared_past_guard()
    assert not out["passed"]
    assert any(f["kind"] == "missed" for f in out["failures"])


def test_shared_past_golden_covers_both_directions():
    from src.eval.recall_claim_eval import _SHARED_PAST_GOLDEN
    kinds = {bool(r["expect_block"]) for r in _SHARED_PAST_GOLDEN}
    assert kinds == {True, False}
    assert sum(1 for r in _SHARED_PAST_GOLDEN if r["expect_block"]) >= 4
    assert sum(1 for r in _SHARED_PAST_GOLDEN if not r["expect_block"]) >= 4


def test_shared_past_stage_gate_semantics():
    """初识期命中即拦（无需语料）；深阶段无语料不动手（缺料不乱杀）。"""
    from src.ai.outbound_text_guard import strip_ungrounded_shared_past
    fab = "还记得我们一起去那家海鲜店吗"
    out, hits = strip_ungrounded_shared_past(
        fab, user_texts=[], memory_text="", relationship_stage="initial")
    assert hits, "初识期共同过去叙事必须被拦"
    out2, hits2 = strip_ungrounded_shared_past(
        fab, user_texts=[], memory_text="", relationship_stage="steady")
    assert not hits2 and out2 == fab, "深阶段无语料应保守放行"


def test_shared_past_wired_into_outbound_guard():
    """接线钉：apply_outbound_text_guard 必须消费 shared_past 档 +
    skill_manager 必须透传 relationship_stage。"""
    import inspect

    from src.ai.outbound_text_guard import apply_outbound_text_guard
    out, meta = apply_outbound_text_guard(
        "还记得我们一起去那家海鲜店吗",
        user_texts=["随便聊聊"], memory_text="",
        relationship_stage="initial")
    assert meta.get("shared_past_hits"), "出稿口未接 #110 共同经历锁"

    from src.skills.skill_manager import SkillManager
    src = inspect.getsource(SkillManager._apply_outbound_text_guard)
    assert "relationship_stage" in src, "skill_manager 未透传关系阶段（#110）"


def test_goal_discipline_bans_fabricated_history():
    """#110 修向③：goal agenda 两档纪律行必须带「禁虚构历史铺垫」钉子。"""
    from src.companion.goals.context_block import (
        _DISCIPLINE, _DISCIPLINE_SPRINT)
    for d in (_DISCIPLINE, _DISCIPLINE_SPRINT):
        assert "编造共同经历" in d and "虚构过去" in d
