# -*- coding: utf-8 -*-
"""P5 `src/ai/referral_gate.py` 单元测试 — 统一引流决策闸。

覆盖 3 层决策:
  * hard_block: no contact / should_block_referral / cooldown (+ referral_ask 覆盖)
  * hard_allow: intent=referral_ask / buying
  * soft score: 5 档累加,阈值通过
  * _parse_iso 容错
  * GateDecision.to_dict() 序列化
"""
from __future__ import annotations

import datetime as _dt
import pytest


# ─── hard_block 层 ───────────────────────────────────────────────────────────

class TestHardBlock:
    def test_no_contact_blocks(self):
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="buying", has_contact=False)
        assert d.refer is False
        assert d.level == "hard_block"
        assert any("has_contact" in r for r in d.reasons)

    def test_should_block_referral_from_memory(self):
        from src.ai.referral_gate import should_refer
        ctx = {"should_block_referral": True, "profile": {"total_turns": 10}}
        d = should_refer(intent="interest", has_contact=True, memory_ctx=ctx)
        assert d.refer is False
        assert d.level == "hard_block"

    def test_referral_ask_bypasses_should_block(self):
        """对方主动要联系方式时,即使上次引流未回仍要回。"""
        from src.ai.referral_gate import should_refer
        ctx = {"should_block_referral": True, "profile": {}}
        d = should_refer(intent="referral_ask", has_contact=True,
                         memory_ctx=ctx)
        assert d.refer is True
        assert d.level == "hard_allow"

    def test_cooldown_blocks_when_recent(self):
        from src.ai.referral_gate import should_refer
        now = _dt.datetime(2026, 4, 23, 12, 0, 0)
        recent_iso = (now - _dt.timedelta(minutes=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        ctx = {"profile": {"last_referral_at": recent_iso}}
        d = should_refer(
            intent="interest", has_contact=True, memory_ctx=ctx,
            config={"refer_cooldown_hours": 1}, now=now,
        )
        assert d.refer is False
        assert d.level == "hard_block"
        assert any("冷却" in r for r in d.reasons)

    def test_cooldown_expired_does_not_block(self):
        from src.ai.referral_gate import should_refer
        now = _dt.datetime(2026, 4, 23, 12, 0, 0)
        old_iso = (now - _dt.timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        ctx = {"profile": {"last_referral_at": old_iso,
                           "total_turns": 5, "peer_reply_count": 3}}
        d = should_refer(
            intent="interest", has_contact=True, memory_ctx=ctx,
            ref_score=0.7, lead_score=70,
            config={"refer_cooldown_hours": 1}, now=now,
        )
        assert d.level != "hard_block"

    def test_referral_ask_bypasses_cooldown(self):
        from src.ai.referral_gate import should_refer
        now = _dt.datetime(2026, 4, 23, 12, 0, 0)
        recent_iso = (now - _dt.timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        ctx = {"profile": {"last_referral_at": recent_iso}}
        d = should_refer(
            intent="referral_ask", has_contact=True, memory_ctx=ctx,
            config={"refer_cooldown_hours": 1}, now=now,
        )
        assert d.refer is True
        assert d.level == "hard_allow"

    def test_cooldown_zero_disables_check(self):
        from src.ai.referral_gate import should_refer
        now = _dt.datetime(2026, 4, 23, 12, 0, 0)
        recent_iso = (now - _dt.timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        ctx = {"profile": {"last_referral_at": recent_iso,
                           "total_turns": 4, "peer_reply_count": 3}}
        d = should_refer(
            intent="interest", has_contact=True, memory_ctx=ctx,
            ref_score=0.8, lead_score=70,
            config={"refer_cooldown_hours": 0}, now=now,
        )
        assert d.level != "hard_block"


# ─── hard_allow 层 ──────────────────────────────────────────────────────────

class TestHardAllow:
    def test_intent_referral_ask(self):
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="referral_ask", has_contact=True)
        assert d.refer is True
        assert d.level == "hard_allow"
        assert any("referral_ask" in r for r in d.reasons)

    def test_intent_buying(self):
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="buying", has_contact=True)
        assert d.refer is True
        assert d.level == "hard_allow"

    def test_no_contact_blocks_even_buying(self):
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="buying", has_contact=False)
        # hard_block 优先级高于 hard_allow
        assert d.refer is False
        assert d.level == "hard_block"


# ─── soft score 层 ──────────────────────────────────────────────────────────

class TestSoftScore:
    def test_cold_start_score_zero(self):
        """无任何信号 → score=0 不通过。"""
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="smalltalk", has_contact=True)
        assert d.refer is False
        assert d.level == "soft_fail"
        assert d.score == 0

    def test_all_five_signals_pass(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {"total_turns": 5, "peer_reply_count": 3}}
        # 显式 override min_lead_score=60 激活 lead_score 信号 (A 反馈后
        # DEFAULT=0 禁用,需手动打开才数 5 档)
        d = should_refer(
            intent="interest", has_contact=True,
            ref_score=0.8, lead_score=75, memory_ctx=ctx,
            config={"min_lead_score": 60},
        )
        assert d.refer is True
        assert d.level == "soft_pass"
        assert d.score == 5

    def test_default_min_lead_score_disabled(self):
        """DEFAULT_CONFIG.min_lead_score=0 时 lead_score 信号应被禁用。"""
        from src.ai.referral_gate import should_refer, DEFAULT_CONFIG
        assert DEFAULT_CONFIG["min_lead_score"] == 0
        ctx = {"profile": {"total_turns": 5, "peer_reply_count": 3}}
        d = should_refer(
            intent="interest", has_contact=True,
            ref_score=0.8, lead_score=99, memory_ctx=ctx,
        )
        # 4 个信号命中 (turns/intent/ref/replies),lead_score 被禁用不算
        assert d.score == 4
        assert not any("lead_score" in r for r in d.reasons)
        assert d.refer is True  # 4 ≥ threshold=3

    def test_exact_threshold_passes(self):
        """score == threshold 应通过。"""
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {"total_turns": 5, "peer_reply_count": 3}}
        d = should_refer(
            intent="interest",  # +1
            has_contact=True,
            ref_score=0.6,  # +1
            lead_score=10,  # NOT
            memory_ctx=ctx,  # total_turns +1, peer_reply_count +1 = 4
        )
        # total_turns (+1), intent=interest (+1), ref_score>0.5 (+1),
        # peer_reply_count (+1) = 4 ≥ 3
        assert d.refer is True
        assert d.level == "soft_pass"

    def test_below_threshold_fails(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {"total_turns": 1}}
        d = should_refer(
            intent="smalltalk", has_contact=True,
            ref_score=0.3, lead_score=20, memory_ctx=ctx,
        )
        assert d.refer is False
        assert d.level == "soft_fail"
        # No signals fire: total_turns<3, intent=smalltalk, ref_score<0.5,
        # lead_score<60, peer_reply_count=0
        assert d.score == 0

    def test_custom_threshold_lower(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {"total_turns": 5}}
        d = should_refer(
            intent="smalltalk", has_contact=True, memory_ctx=ctx,
            config={"score_threshold": 1},
        )
        # total_turns +1 ≥ threshold=1
        assert d.refer is True

    def test_custom_min_turns(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {"total_turns": 2}}
        d = should_refer(
            intent="interest", has_contact=True, memory_ctx=ctx,
            ref_score=0.6, lead_score=65,
            config={"min_turns": 10, "min_lead_score": 60},
            # min_turns=10 → total_turns=2 不中; min_lead_score=60 激活
        )
        # interest +1, ref +1, lead +1 = 3 (total_turns 不中,peer_reply_count=0)
        assert d.score == 3
        assert d.refer is True  # 3 >= 3

    def test_ref_score_boundary(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {}}
        # ref_score=0.5 不足 (> 0.5 才通过)
        d = should_refer(
            intent="smalltalk", has_contact=True, memory_ctx=ctx,
            ref_score=0.5,
        )
        assert d.score == 0
        # 0.51 通过
        d2 = should_refer(
            intent="smalltalk", has_contact=True, memory_ctx=ctx,
            ref_score=0.51,
        )
        assert d2.score == 1

    def test_custom_min_lead_score(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {}}
        d = should_refer(
            intent="smalltalk", has_contact=True, memory_ctx=ctx,
            lead_score=45, config={"min_lead_score": 40},
        )
        assert d.score == 1

    def test_bad_ref_score_type_does_not_crash(self):
        from src.ai.referral_gate import should_refer
        ctx = {"profile": {}}
        d = should_refer(
            intent="smalltalk", has_contact=True, memory_ctx=ctx,
            ref_score=None,  # type: ignore — robustness
        )
        assert isinstance(d.score, int)


# ─── _parse_iso ──────────────────────────────────────────────────────────────

class TestParseIso:
    def test_z_format(self):
        from src.ai.referral_gate import _parse_iso
        assert _parse_iso("2026-04-23T10:30:00Z") == \
            _dt.datetime(2026, 4, 23, 10, 30, 0)

    def test_space_format(self):
        from src.ai.referral_gate import _parse_iso
        assert _parse_iso("2026-04-23 10:30:00") == \
            _dt.datetime(2026, 4, 23, 10, 30, 0)

    def test_empty_returns_none(self):
        from src.ai.referral_gate import _parse_iso
        assert _parse_iso("") is None
        assert _parse_iso(None) is None

    def test_bogus_returns_none(self):
        from src.ai.referral_gate import _parse_iso
        assert _parse_iso("hello world") is None

    def test_isoformat_variant(self):
        from src.ai.referral_gate import _parse_iso
        # fromisoformat 可认 YYYY-MM-DDTHH:MM:SS
        assert _parse_iso("2026-04-23T10:30:00") == \
            _dt.datetime(2026, 4, 23, 10, 30, 0)


# ─── GateDecision 序列化 ────────────────────────────────────────────────────

class TestGateDecisionDict:
    def test_to_dict_roundtrip(self):
        from src.ai.referral_gate import GateDecision
        d = GateDecision(refer=True, level="hard_allow", score=5,
                         threshold=3, reasons=["a", "b"])
        out = d.to_dict()
        assert out == {
            "refer": True, "level": "hard_allow", "score": 5,
            "threshold": 3, "reasons": ["a", "b"],
        }

    def test_default_reasons_is_empty_list(self):
        from src.ai.referral_gate import GateDecision
        d = GateDecision(refer=False, level="soft_fail")
        assert d.reasons == []


# ─── 端到端决策矩阵 ─────────────────────────────────────────────────────────

class TestEndToEndMatrix:
    """验证 3 层优先级: hard_block > hard_allow > soft_*"""

    def test_no_contact_beats_buying_intent(self):
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="buying", has_contact=False)
        assert d.level == "hard_block"

    def test_should_block_beats_soft_score(self):
        from src.ai.referral_gate import should_refer
        ctx = {"should_block_referral": True,
               "profile": {"total_turns": 99, "peer_reply_count": 99}}
        d = should_refer(intent="interest", has_contact=True, memory_ctx=ctx,
                         ref_score=0.99, lead_score=99)
        assert d.level == "hard_block"

    def test_buying_beats_soft_score(self):
        """buying 硬通过,即使其他信号不够也该引。"""
        from src.ai.referral_gate import should_refer
        d = should_refer(intent="buying", has_contact=True)  # 零其他信号
        assert d.refer is True
        assert d.level == "hard_allow"
