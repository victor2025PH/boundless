# -*- coding: utf-8 -*-
"""撤销设定行为验证门禁（P3 期，2026-08-04）。

确定性验证（retire-verify）证「prompt 干净」，本卷证「模型行为顺从」：
挑衅题（通用日常题=原始事故触发面 + 假记忆攻击题=生产实录）× 出站守卫同源
判分（persona_guard.find_violations）。门禁重点：判分语义绝不说反——
认领=不及格、否定澄清=通过且如实标 mentioned、空回答=不及格（宁红勿绿）、
无关守卫命中不改本卷分数（只观察）。全程假 chat_fn，零真实 LLM。
"""
from src.utils.persona_quiz import (
    build_retired_quiz,
    run_retired_quiz,
    score_retired_answers,
)

_P = {
    "id": "lin_xiaoyu",
    "name": "小雨",
    "role": "大学生",
    "boundaries": {"retired_facts": [
        {"text": "养猫（已删）", "added": "2026-08-03", "terms": ["猫"]},
    ]},
}


# ── 出题 ─────────────────────────────────────────────────────────────────────

def test_build_quiz_shape_and_determinism():
    quiz = build_retired_quiz(_P)
    # 2 道通用 + 每锚词 2 道定向
    assert len(quiz) == 4
    kinds = [q["kind"] for q in quiz]
    assert kinds[:2] == ["generic", "generic"]
    assert kinds[2:] == ["targeted", "targeted"]
    assert all("猫" in q["q"] for q in quiz if q["kind"] == "targeted")
    # 原始事故触发面必须在卷（「在干嘛」类日常题）
    assert any("在干嘛" in q["q"] for q in quiz)
    # 假记忆攻击题必须在卷（生产实录：「我记得你上次说过你的X」）
    assert any("我记得你上次" in q["q"] for q in quiz)
    assert quiz == build_retired_quiz(_P)          # 同档案同卷


def test_build_quiz_empty_without_terms():
    assert build_retired_quiz({"name": "x"}) == []
    legacy = {"name": "x", "boundaries": {"retired_facts": ["纯文案无锚词"]}}
    assert build_retired_quiz(legacy) == []


def test_build_quiz_caps_terms():
    p = {"name": "x", "boundaries": {"retired_facts": [
        {"text": f"t{i}", "terms": [f"词{i}"]} for i in range(8)
    ]}}
    quiz = build_retired_quiz(p, max_terms=2)
    targeted_terms = {q["term"] for q in quiz if q["kind"] == "targeted"}
    assert targeted_terms == {"词0", "词1"}


# ── 判分 ─────────────────────────────────────────────────────────────────────

def _score_one(answer: str):
    quiz = [{"q": "在干嘛呢？", "term": "", "kind": "generic"}]
    return score_retired_answers(_P, quiz, [answer])["items"][0]


def test_claim_fails():
    it = _score_one("刚下课啦。我家猫今天特别黏人。")
    assert it["pass"] is False
    assert it["violations"], "认领句必须给出命中片段"
    assert it["mentioned"] is True


def test_denial_passes_and_marked_mentioned():
    """否定澄清是钉子要求的正确行为——判成不及格=自己打自己。"""
    it = _score_one("我没有养猫呀，你是不是记成别人啦哈哈")
    assert it["pass"] is True
    assert it["violations"] == []
    assert it["mentioned"] is True


def test_unrelated_answer_passes():
    it = _score_one("刚下课，在图书馆写作业呢")
    assert it["pass"] is True and it["mentioned"] is False


def test_empty_answer_fails():
    """LLM 调不通=什么都没验证到，宁红勿绿。"""
    it = _score_one("")
    assert it["pass"] is False


def test_other_guard_hits_observed_not_scored():
    """无关守卫命中（如 forbidden_phrases）只观察不改本卷分数。"""
    p = dict(_P)
    p["speaking"] = {"forbidden_phrases": ["宝子"]}
    quiz = [{"q": "在干嘛呢？", "term": "", "kind": "generic"}]
    it = score_retired_answers(p, quiz, ["宝子我在图书馆自习呢"])["items"][0]
    assert it["pass"] is True                     # 撤销卷面不受牵连
    assert it["other_violations"], "无关命中要如实附在 other_violations"


def test_summary_counts():
    quiz = build_retired_quiz(_P)
    answers = ["我家猫可乖了"] + ["在写作业"] * (len(quiz) - 1)
    rep = score_retired_answers(_P, quiz, answers)
    assert rep["total"] == len(quiz)
    assert rep["failed"] == 1 and rep["passed"] == len(quiz) - 1
    assert 0 < rep["score"] < 100


# ── 端到端（假 chat_fn）──────────────────────────────────────────────────────

def test_run_retired_quiz_end_to_end():
    calls = []

    def chat_fn(system, user, timeout):
        calls.append((system, user))
        assert "已作废的旧设定" in system          # 真实人设 prompt（含钉子）在卷
        if "我记得你上次" in user:
            return "哈哈我家猫是叫毛豆呀"          # 假记忆攻击得手 → 该题必须红
        return "我没有养猫呀，你记岔了吧"

    stages = []
    rep = run_retired_quiz(_P, chat_fn,
                           on_stage=lambda s, p: stages.append((s, p)))
    assert rep["total"] == 4 and rep["failed"] == 1
    bad = [it for it in rep["items"] if not it["pass"]]
    assert len(bad) == 1 and "我记得你上次" in bad[0]["q"]
    assert len(calls) == 4
    assert stages and stages[0][0] == "retired_1"
    assert rep["persona_name"] == "小雨"


def test_run_retired_quiz_survives_chat_exception():
    def chat_fn(system, user, timeout):
        raise RuntimeError("llm down")

    rep = run_retired_quiz(_P, chat_fn)
    assert rep["total"] == 4
    assert rep["failed"] == 4                     # 全空答=全红，绝不炸
    assert rep["score"] == 0


def test_run_retired_quiz_empty_without_terms():
    rep = run_retired_quiz({"name": "x"}, lambda *a: "hi")
    assert rep["total"] == 0 and rep["items"] == []
