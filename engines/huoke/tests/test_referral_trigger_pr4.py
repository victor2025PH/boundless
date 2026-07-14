# -*- coding: utf-8 -*-
"""PR-4 测试: 引流触发器扩展 (trigger keywords / rejection cooldown / persona config).

跟 tests/test_referral_gate.py 既有 30 个测试并存, 不重复覆盖 hard_block /
hard_allow / soft_score 等老路径.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from src.ai import referral_gate as gate


# ── fixtures ─────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def reload_yaml_caches():
    """每测开始时重 load yaml (避免 cache 污染)."""
    gate.reload_persona_strategies_for_tests()
    gate.reload_trigger_keywords_for_tests()
    yield
    gate.reload_persona_strategies_for_tests()
    gate.reload_trigger_keywords_for_tests()


# ── trigger keyword 命中 → hard_allow ────────────────────────────────
def test_jp_trigger_keyword_hits_hard_allow():
    """日语 'LINE教えて' 命中 → 直接 hard_allow 即使聊天轮数不够."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 1}},  # 只聊了 1 轮
        incoming_text="LINE教えて欲しい",
        persona_key="jp_female_midlife",
    )
    assert d.refer is True
    assert d.level == "hard_allow"
    assert any("关键词命中" in r for r in d.reasons)


def test_jp_rejection_keyword_hits_hard_block():
    """日语 'LINEはやらない' 命中 → 直接 hard_block."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        incoming_text="LINEはやらないので",
        persona_key="jp_female_midlife",
    )
    assert d.refer is False
    assert d.level == "hard_block"
    assert any("拒绝引流关键词命中" in r for r in d.reasons)


def test_no_contact_blocks_even_if_keyword_hits():
    """has_contact=False 时即使关键词命中也不引."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=False,
        incoming_text="LINE教えて",
        persona_key="jp_female_midlife",
    )
    assert d.refer is False
    assert d.level == "hard_block"


def test_rejection_takes_precedence_over_trigger():
    """同时命中拒绝 + 触发词时, 拒绝优先."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        incoming_text="LINE教えて欲しいけど興味ない",  # 含 LINE 教えて + 興味ない
        persona_key="jp_female_midlife",
    )
    assert d.refer is False
    assert d.level == "hard_block"
    assert any("拒绝" in r for r in d.reasons)


def test_default_persona_uses_en_keywords():
    """default persona (无 persona_key) 用 en 词典, jp 词不命中."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 5, "peer_reply_count": 3}},
        incoming_text="LINE教えて",  # 日语词, 但 default trigger lang=en
        persona_key=None,
    )
    # 不该被 trigger keyword 命中 (lang=en); 走正常 soft_score 路径
    assert d.level in ("soft_pass", "soft_fail")


# ── rejection_cooldown_days hard_block ───────────────────────────────
def test_rejection_cooldown_active_blocks():
    """客户拒绝 3 天前, jp_female_midlife cooldown=7, 还在冷却中."""
    now = _dt.datetime(2026, 4, 26, 12, 0, 0)
    rejected_3d_ago = (now - _dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = gate.should_refer(
        intent="referral_ask",  # 即使主动要也 block
        has_contact=True,
        memory_ctx={
            "profile": {"total_turns": 10, "peer_reply_count": 5},
            "referral_rejected_at": rejected_3d_ago,
        },
        persona_key="jp_female_midlife",
        now=now,
    )
    assert d.refer is False
    assert d.level == "hard_block"
    assert any("拒绝冷却" in r for r in d.reasons)


def test_rejection_cooldown_expired_allows():
    """客户拒绝 8 天前, cooldown=7 已过 → 不再 block."""
    now = _dt.datetime(2026, 4, 26, 12, 0, 0)
    rejected_8d_ago = (now - _dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = gate.should_refer(
        intent="referral_ask",
        has_contact=True,
        memory_ctx={
            "profile": {"total_turns": 10},
            "referral_rejected_at": rejected_8d_ago,
        },
        persona_key="jp_female_midlife",
        now=now,
    )
    assert d.refer is True
    assert d.level == "hard_allow"


def test_default_persona_no_rejection_cooldown():
    """default persona (rejection_cooldown_days=0) 不启用拒绝冷却."""
    now = _dt.datetime(2026, 4, 26, 12, 0, 0)
    rejected_1d_ago = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = gate.should_refer(
        intent="referral_ask",
        has_contact=True,
        memory_ctx={
            "profile": {"total_turns": 5},
            "referral_rejected_at": rejected_1d_ago,
        },
        persona_key=None,
        now=now,
    )
    # default 不看 referral_rejected_at, intent=referral_ask 直接 hard_allow
    assert d.refer is True
    assert d.level == "hard_allow"


# ── persona config 加载 ──────────────────────────────────────────────
def test_load_persona_config_jp_female_midlife():
    """jp_female_midlife: min_turns=7, rejection_cooldown_days=7."""
    cfg = gate.load_persona_config("jp_female_midlife")
    assert cfg["min_turns"] == 7
    assert cfg["rejection_cooldown_days"] == 7
    assert cfg["bot_persona"] == "jp_caring_male"
    assert cfg["channel_priority"] == ["line"]
    assert cfg["trigger_keywords_lang"] == "ja"


def test_load_persona_config_unknown_persona_falls_back_to_default():
    """未知 persona 走 yaml.default."""
    cfg = gate.load_persona_config("nonexistent_persona")
    # default 段定义在 yaml: min_turns=3
    assert cfg["min_turns"] == 3
    # bot_persona 是 default 的空串 (yaml 里覆盖了 module DEFAULT)
    assert cfg.get("bot_persona") == ""


def test_load_persona_config_none_returns_default():
    """persona_key=None 也走 default."""
    cfg = gate.load_persona_config(None)
    assert cfg["min_turns"] == 3


# ── jp_female_midlife min_turns=7 行为 ───────────────────────────────
def test_jp_persona_requires_7_turns_in_soft_score():
    """jp_female_midlife min_turns=7, 聊 5 轮还不够 (在 soft_score 路径)."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 5, "peer_reply_count": 3}},
        ref_score=0.6,
        persona_key="jp_female_midlife",
    )
    # total_turns=5 < min_turns=7, 不给 +1; ref_score=0.6 给 +1; peer_reply=3 给 +1
    # 总分 = 2 < threshold 3, refuse
    assert d.refer is False
    assert d.level == "soft_fail"


def test_jp_persona_passes_at_7_turns():
    """jp_female_midlife: 聊 7 轮, peer_reply 3, ref_score 0.6 → 3 分通过."""
    d = gate.should_refer(
        intent="smalltalk",
        has_contact=True,
        memory_ctx={"profile": {"total_turns": 7, "peer_reply_count": 3}},
        ref_score=0.6,
        persona_key="jp_female_midlife",
    )
    # turns≥7: +1, ref_score>0.5: +1, peer_replies≥2: +1 = 3 ≥ threshold 3
    assert d.refer is True
    assert d.level == "soft_pass"


# ── 关键词命中 helpers (单元) ────────────────────────────────────────
def test_hits_trigger_keyword_ja():
    assert gate.hits_trigger_keyword("LINE教えて欲しい", lang="ja") is True
    assert gate.hits_trigger_keyword("もっと話したいな", lang="ja") is True
    assert gate.hits_trigger_keyword("普通的对话内容", lang="ja") is False


def test_hits_trigger_keyword_en():
    assert gate.hits_trigger_keyword("Can you give me your line id?", lang="en") is True
    assert gate.hits_trigger_keyword("just chatting", lang="en") is False


def test_hits_rejection_keyword_ja():
    assert gate.hits_rejection_keyword("LINEはやらないので", lang="ja") is True
    assert gate.hits_rejection_keyword("結構です", lang="ja") is True
    assert gate.hits_rejection_keyword("興味ないかな", lang="ja") is True
    assert gate.hits_rejection_keyword("こんにちは", lang="ja") is False


def test_empty_text_no_hit():
    assert gate.hits_trigger_keyword("", lang="ja") is False
    assert gate.hits_rejection_keyword("", lang="ja") is False


# ── chat_brain bot_persona 注入 ──────────────────────────────────────
def test_bot_persona_identity_jp_caring_male():
    """jp_caring_male 调用应返回日本男性关爱身份块."""
    from src.ai.chat_brain import _bot_persona_identity
    block = _bot_persona_identity("jp_caring_male")
    assert "日本中年男性" in block
    assert "温柔" in block
    assert "倾听" in block


def test_bot_persona_identity_unknown_returns_fallback():
    from src.ai.chat_brain import _bot_persona_identity
    block = _bot_persona_identity("nonexistent")
    # 通用 fallback (跟原 prompt 对齐)
    assert "友好" in block or "真实" in block


def test_bot_persona_identity_none_returns_fallback():
    from src.ai.chat_brain import _bot_persona_identity
    block = _bot_persona_identity(None)
    assert "友好" in block or "真实" in block


def test_chat_brain_referral_stage_includes_jp_caring_male_block():
    """stage=referral + bot_persona=jp_caring_male 时, prompt 应含日本男性调性提示."""
    from src.ai.chat_brain import ChatBrain, UserProfile
    from unittest.mock import MagicMock
    brain = ChatBrain.__new__(ChatBrain)
    brain._llm = MagicMock()
    brain._memory = MagicMock()
    brain._profiles = {}
    profile = UserProfile(username="test")
    prompt = brain._build_system_prompt(
        stage="referral",
        profile=profile,
        platform="messenger",
        target_language="ja",
        contact_info="line_id_xyz",
        source="",
        bot_persona="jp_caring_male",
    )
    # 身份块
    assert "日本中年男性" in prompt
    # 引流话术调性指引
    assert "日本男性关爱口吻" in prompt
    assert "命令式" in prompt or "肉麻" not in prompt  # 关键约束
