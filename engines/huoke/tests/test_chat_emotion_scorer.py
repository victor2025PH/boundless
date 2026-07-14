# -*- coding: utf-8 -*-
"""L3 客户情感评分 (chat_emotion_scorer) 单测.

覆盖:
1. 综合分公式 (4 维加权 + frustration 反向)
2. JSON 解析 (合法 / markdown 包裹 / 多余文本前后 / 字段缺失)
3. 缓存命中 + TTL 过期
4. LLM 失败 fallback 中性分
5. messages 格式化
6. referral_gate 接 emotion_overall 串通
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.ai import chat_emotion_scorer as scorer
from src.ai import referral_gate as gate


@pytest.fixture(autouse=True)
def reset_state():
    scorer.clear_cache_for_tests()
    gate.reload_persona_strategies_for_tests()
    yield
    scorer.clear_cache_for_tests()
    gate.reload_persona_strategies_for_tests()


# ── 综合分公式 ───────────────────────────────────────────────────────
def test_compute_overall_high_trust_high_topic_match():
    """trust=0.9, interest=0.8, frustration=0.1, topic=0.9 → 高综合分."""
    s = scorer.compute_overall_score({
        "trust": 0.9, "interest": 0.8,
        "frustration": 0.1, "topic_match": 0.9,
    })
    # 0.9*0.4 + 0.8*0.3 + 0.9*0.2 + 0.9*0.1 = 0.36 + 0.24 + 0.18 + 0.09 = 0.87
    assert 0.86 <= s <= 0.88


def test_compute_overall_low_frustration_inverse_kicks_in():
    """frustration=1.0 (满分不耐烦) 应让 (1-frustration)*0.2 = 0."""
    s = scorer.compute_overall_score({
        "trust": 0.5, "interest": 0.5,
        "frustration": 1.0, "topic_match": 0.5,
    })
    # 0.5*0.4 + 0.5*0.3 + 0*0.2 + 0.5*0.1 = 0.2 + 0.15 + 0 + 0.05 = 0.40
    assert 0.39 <= s <= 0.41


def test_compute_overall_clamped_to_0_1():
    """非法值返回中性分."""
    s = scorer.compute_overall_score({"trust": "garbage"})
    assert s == scorer.NEUTRAL_OVERALL


# ── JSON parse ───────────────────────────────────────────────────────
def test_parse_valid_json():
    raw = '{"trust": 0.7, "interest": 0.8, "frustration": 0.2, "topic_match": 0.6, "rationale": "good"}'
    parsed = scorer._parse_llm_json(raw)
    assert parsed["trust"] == 0.7
    assert parsed["rationale"] == "good"


def test_parse_json_in_markdown_code_block():
    """LLM 偶尔会用 ```json ... ``` 包裹."""
    raw = '```json\n{"trust": 0.5, "interest": 0.5, "frustration": 0.5, "topic_match": 0.5, "rationale": "neutral"}\n```'
    parsed = scorer._parse_llm_json(raw)
    assert parsed is not None
    assert parsed["trust"] == 0.5


def test_parse_json_with_prose_around():
    """LLM 输出 'Here is the result: {...}'."""
    raw = '基于聊天分析: {"trust": 0.4, "interest": 0.6, "frustration": 0.3, "topic_match": 0.7, "rationale": "ok"}'
    parsed = scorer._parse_llm_json(raw)
    assert parsed["trust"] == 0.4


def test_parse_rejects_missing_field():
    raw = '{"trust": 0.7, "interest": 0.8}'
    assert scorer._parse_llm_json(raw) is None


def test_parse_rejects_out_of_range():
    raw = '{"trust": 1.5, "interest": 0.5, "frustration": 0.5, "topic_match": 0.5}'
    assert scorer._parse_llm_json(raw) is None


def test_parse_rejects_garbage():
    assert scorer._parse_llm_json("not json at all") is None
    assert scorer._parse_llm_json("") is None


# ── 缓存 ─────────────────────────────────────────────────────────────
def test_cache_hits_on_same_messages(monkeypatch):
    call_count = [0]

    def fake_llm(messages, persona):
        call_count[0] += 1
        return {
            "trust": 0.7, "interest": 0.8,
            "frustration": 0.2, "topic_match": 0.6,
            "rationale": "ok",
        }

    monkeypatch.setattr(scorer, "_call_llm", fake_llm)
    msgs = [{"role": "user", "content": "こんにちは"}]

    r1 = scorer.score_emotion(msgs, persona_key="jp_female_midlife")
    r2 = scorer.score_emotion(msgs, persona_key="jp_female_midlife")
    r3 = scorer.score_emotion(msgs, persona_key="jp_female_midlife")
    assert call_count[0] == 1  # 后两次走 cache
    assert r1["cached"] is False
    assert r2["cached"] is True
    assert r3["cached"] is True


def test_cache_miss_on_different_messages(monkeypatch):
    call_count = [0]

    def fake_llm(messages, persona):
        call_count[0] += 1
        return {"trust": 0.5, "interest": 0.5, "frustration": 0.5, "topic_match": 0.5, "rationale": ""}

    monkeypatch.setattr(scorer, "_call_llm", fake_llm)
    scorer.score_emotion([{"role": "user", "content": "msg1"}], "jp_female_midlife")
    scorer.score_emotion([{"role": "user", "content": "msg2"}], "jp_female_midlife")
    assert call_count[0] == 2


def test_cache_expires_after_ttl(monkeypatch):
    """TTL 过期后重新调 LLM."""
    call_count = [0]

    def fake_llm(messages, persona):
        call_count[0] += 1
        return {"trust": 0.5, "interest": 0.5, "frustration": 0.5, "topic_match": 0.5, "rationale": ""}

    monkeypatch.setattr(scorer, "_call_llm", fake_llm)
    msgs = [{"role": "user", "content": "test"}]
    scorer.score_emotion(msgs, "jp_female_midlife")

    # 推时间过 TTL
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 700)
    scorer.score_emotion(msgs, "jp_female_midlife")
    assert call_count[0] == 2


def test_use_cache_false_forces_recompute(monkeypatch):
    call_count = [0]

    def fake_llm(messages, persona):
        call_count[0] += 1
        return {"trust": 0.5, "interest": 0.5, "frustration": 0.5, "topic_match": 0.5, "rationale": ""}

    monkeypatch.setattr(scorer, "_call_llm", fake_llm)
    msgs = [{"role": "user", "content": "test"}]
    scorer.score_emotion(msgs, "jp_female_midlife")
    scorer.score_emotion(msgs, "jp_female_midlife", use_cache=False)
    assert call_count[0] == 2


# ── LLM 失败 fallback ────────────────────────────────────────────────
def test_llm_failure_returns_neutral(monkeypatch):
    monkeypatch.setattr(scorer, "_call_llm", lambda m, p: None)
    r = scorer.score_emotion(
        [{"role": "user", "content": "hi"}], "jp_female_midlife",
    )
    assert r["fallback"] is True
    assert r["overall"] == scorer.NEUTRAL_OVERALL
    assert r["trust"] == 0.5


def test_empty_messages_returns_neutral():
    r = scorer.score_emotion([], "jp_female_midlife")
    assert r["fallback"] is True
    assert r["overall"] == scorer.NEUTRAL_OVERALL


# ── messages 格式化 ─────────────────────────────────────────────────
def test_format_history_user_to_对方():
    msgs = [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "やあ"},
    ]
    out = scorer._format_history(msgs)
    assert "对方: こんにちは" in out
    assert "我: やあ" in out


def test_format_history_truncates_to_5_latest():
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
    out = scorer._format_history(msgs)
    # 应只含最后 5 条 (msg5..msg9)
    assert "msg5" in out
    assert "msg9" in out
    assert "msg0" not in out
    assert "msg4" not in out


# ── referral_gate 接 emotion_overall ─────────────────────────────────
def test_referral_gate_emotion_below_threshold_does_not_score():
    """jp_female_midlife min_emotion_score=0.5, emotion=0.3 不加分."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 7, "peer_reply_count": 3}},
        ref_score=0.6,
        emotion_overall=0.3,  # < 0.5 threshold
        persona_key="jp_female_midlife",
    )
    # turns≥7: +1, ref_score>0.5: +1, peer_replies≥2: +1 = 3 (但应 fail emotion)
    # emotion 不达标 reason 进 reasons 但不加分 — 综合 3 分仍可能 pass
    # 我们看 reasons 含 "温度不够"
    assert any("温度不够" in r for r in d.reasons)


def test_referral_gate_emotion_above_threshold_adds_score():
    """jp_female_midlife emotion 0.7 加 1 分."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 7, "peer_reply_count": 3}},
        ref_score=0.6,
        emotion_overall=0.7,
        persona_key="jp_female_midlife",
    )
    # 4 个加分项: turns +1 / ref_score +1 / peer_replies +1 / emotion +1 = 4
    assert d.score >= 4
    assert any("emotion_overall" in r and "≥" in r for r in d.reasons)


def test_referral_gate_emotion_none_does_not_break():
    """不传 emotion_overall (=None) 时不加分也不 reason — 保持向后兼容."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 7, "peer_reply_count": 3}},
        ref_score=0.6,
        persona_key="jp_female_midlife",
    )
    # 跟之前 PR-4 行为一致: turns +1, ref_score +1, peer_replies +1 = 3
    assert d.score == 3
    assert d.refer is True


def test_referral_gate_default_persona_no_emotion_check():
    """default persona min_emotion_score=0, 不启用情感门槛."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 5, "peer_reply_count": 3}},
        ref_score=0.6,
        emotion_overall=0.1,  # 极低, 但 default 不看
        persona_key=None,
    )
    # default 最低 turns=3 (不是 7), 5≥3 →+1; ref_score +1; peer_replies +1 = 3
    # emotion 不计入 (default min=0)
    assert d.score == 3
    assert not any("emotion_overall" in r for r in d.reasons)


# ── score_emotion 端到端 (mock LLM) ──────────────────────────────────
def test_score_emotion_returns_overall_field(monkeypatch):
    monkeypatch.setattr(
        scorer, "_call_llm",
        lambda m, p: {
            "trust": 0.8, "interest": 0.7, "frustration": 0.1,
            "topic_match": 0.8, "rationale": "客户主动分享生活, 兴趣明显",
        },
    )
    r = scorer.score_emotion(
        [{"role": "user", "content": "今日は娘とランチ"},
         {"role": "assistant", "content": "それは素敵ですね"}],
        persona_key="jp_female_midlife",
    )
    assert r["fallback"] is False
    assert "overall" in r
    assert 0.7 <= r["overall"] <= 0.8
    assert r["rationale"] == "客户主动分享生活, 兴趣明显"
