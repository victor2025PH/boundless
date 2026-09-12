# -*- coding: utf-8 -*-
"""上下文/记忆深度四档（ai.context_depth）门禁。

钉住：standard/缺席 = 零行为变化；深档只抬地板不压手调值；策略 context_rounds=0 仍尊重；
UI 白名单枚举与模块档位一致；本机无限制另按端点封顶（不在本文件）。
"""
from __future__ import annotations

import pytest

from src.ai import context_depth as cd


# ── 纯函数 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (None, "standard"), ("", "standard"), ("standard", "standard"), ("标准", "standard"),
    ("deep", "deep"), ("深度", "deep"), ("MAX", "max"), ("最大", "max"),
    ("ultra", "ultra"), ("超大", "ultra"), ("1M", "ultra"), ("garbage", "standard"),
])
def test_normalize_tier(raw, expect):
    assert cd.normalize_tier(raw) == expect


def test_absent_or_standard_is_zero_change():
    for cfg in ({}, {"ai": {}}, {"ai": {"context_depth": "standard"}}, None):
        if cfg is None:
            continue
        assert cd.prompt_budget(cfg, 12000) == 12000
        assert cd.prompt_budget(cfg, 0) == 0                      # 0=不裁 保持
        assert cd.history_limit(cfg, 10) == 10
        assert cd.history_limit(cfg, 10, strategy_rounds=2) == 2
        assert cd.verbatim_rounds(cfg, 5) == 5
        assert cd.verbatim_msgs(cfg, 10) == 10
        assert cd.memory_limits(cfg, 8, 1200) == (8, 1200)
        assert cd.history_fetch_limit(cfg, 30) == 30


def test_deep_tiers_lift_floor_but_never_lower():
    deep = {"ai": {"context_depth": "deep"}}
    assert cd.prompt_budget(deep, 12000) == 32000
    assert cd.prompt_budget(deep, 50000) == 50000                # 手调更大值不被压回
    assert cd.prompt_budget(deep, 0) == 0                        # 不裁 仍不裁
    assert cd.history_limit(deep, 10) == 40
    assert cd.history_limit(deep, 10, strategy_rounds=2) == 40   # 策略只抬不压
    assert cd.history_limit(deep, 10, strategy_rounds=0) == 0    # 0 = 本策略刻意不带历史
    assert cd.verbatim_rounds(deep, 5) == 20
    assert cd.verbatim_msgs(deep, 10) == 40
    assert cd.memory_limits(deep, 8, 1200) == (16, 3000)
    assert cd.history_fetch_limit(deep, 30) == 60

    ultra = {"ai": {"context_depth": "超大"}}
    assert cd.resolve(ultra).key == "ultra"
    assert cd.prompt_budget(ultra, 12000) == 900_000
    assert cd.history_limit(ultra, 10) == 1000
    assert cd.history_fetch_limit(ultra, 30) == 1100 >= cd.history_limit(ultra, 10)


def test_tiers_monotonic():
    keys = list(cd.TIER_KEYS)
    vals = [cd.TIERS[k] for k in keys[1:]]
    for a, b in zip(vals, vals[1:]):
        assert a.prompt_budget_tokens < b.prompt_budget_tokens
        assert a.history_msgs < b.history_msgs
        assert a.memory_items <= b.memory_items
        assert a.history_fetch >= a.history_msgs
    assert cd.TIERS["ultra"].prompt_budget_tokens <= 1_000_000
    assert tuple(cd.TIER_KEYS) == ("standard", "deep", "max", "ultra")


def test_compress_threshold_leaves_headroom():
    assert cd.compress_threshold(5, 8) == 8          # legacy 已够
    assert cd.compress_threshold(20, 8) == 26        # 20 + max(3, 6)
    assert cd.compress_threshold(40, 16) == 53


def test_config_manager_object_and_runtime_config(monkeypatch):
    class CM:
        config = {"ai": {"context_depth": "max"}}
    assert cd.resolve(CM()).key == "max"
    import src.compliance.runtime as rt
    monkeypatch.setattr(rt, "_PROVIDER", lambda: {"ai": {"context_depth": "deep"}})
    assert cd.resolve(None).key == "deep"
    assert cd.history_fetch_limit(None, 30) == 60


def test_settings_choices_synced():
    from src.inbox.reply_pacing_settings import (
        CONTEXT_DEPTH_CHOICES, FIELDS, USAGE_MODE_CHOICES)
    assert tuple(CONTEXT_DEPTH_CHOICES) == tuple(cd.TIER_KEYS)
    f = FIELDS["ai.context_depth"]
    assert f["type"] == "enum" and f["default"] == cd.DEFAULT_TIER and f["hot"] is True
    um = FIELDS["ai.usage_mode"]
    assert um["type"] == "enum" and um["default"] == "full" and um["hot"] is True
    assert tuple(USAGE_MODE_CHOICES) == ("full", "economy")
    assert "lite" not in USAGE_MODE_CHOICES          # 不与深度档混成第五档
    from src.inbox.reply_pacing_settings import field_meta
    assert field_meta()["ai.context_depth"]["choices"] == list(cd.TIER_KEYS)


def test_ui_cloud_four_tiers_and_picker_lan_fill():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    rps = (root / "src/web/templates/reply_settings.html").read_text(encoding="utf-8")
    inbox = (root / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    block = rps.split('id="rps-ctx-depth"', 1)[1].split("</select>", 1)[0]
    assert 'value="standard"' in block and 'value="deep"' in block
    assert 'value="max"' in block and 'value="ultra"' in block
    assert "(ch.depths||['','standard','deep','max','ultra'])" in inbox
    assert "inbox.mp.depth_fill_sub" in inbox and "inbox.mp.depth_follow_sub_lan" in inbox
    assert "约 128k" in rps and "约 900k" in rps
    # 旧进程两档白名单：按 GET meta.choices 藏 deep/ultra，防保存被拒
    assert "function rpsSyncCtxDepthChoices" in rps
    assert "rpsSyncCtxDepthChoices(meta)" in rps
    assert "function rpsCtxDepthCoerce" in rps


def test_settings_sanitize_accepts_tier_and_rejects_junk():
    from src.inbox.reply_pacing_settings import sanitize_patch
    clean, errors = sanitize_patch({"ai.context_depth": "ultra"})
    assert not errors and clean["ai.context_depth"] == "ultra"
    clean, errors = sanitize_patch({"ai.context_depth": "max"})
    assert not errors and clean["ai.context_depth"] == "max"
    _, errors = sanitize_patch({"ai.context_depth": "gigantic"})
    assert errors
    clean, errors = sanitize_patch({"ai.usage_mode": "economy"})
    assert not errors and clean["ai.usage_mode"] == "economy"
    _, errors = sanitize_patch({"ai.usage_mode": "ultra"})
    assert errors


def test_describe_has_all_tiers_and_cost_hint():
    d = cd.describe({"ai": {"context_depth": "deep"}})
    assert d["current"] == "deep" and d["label_zh"] == "深度"
    assert [t["key"] for t in d["tiers"]] == list(cd.TIER_KEYS)
    assert "economy" not in cd.TIER_KEYS
    assert all(t["cost_hint_cny_per_turn_max"] > 0 for t in d["tiers"])
    assert d["economy"]["on"] is False
    d2 = cd.describe({"ai": {"context_depth": "deep", "usage_mode": "economy"}})
    assert d2["current"] == "deep"          # 档位键不被经济档改写
    assert d2["economy"]["on"] is True and d2["economy"]["reason"] == "explicit"
    assert d2["economy"]["history_cap"] == 4 and d2["economy"]["skip_deep_persona"] is True


# ── 消费点联动 ───────────────────────────────────────────────────────────

class _CM:
    def __init__(self, ai=None):
        self.config = {"ai": dict(ai or {})}

    def get(self, key, default=None):
        return self.config.get(key, default)


def _client(ai_cfg):
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)
    c.config = _CM(ai_cfg)
    c._prompt_budget_tokens = 12000
    return c


def test_apply_prompt_budget_uses_tier():
    c = _client({"context_depth": "deep"})
    seen = {}

    def _fake_trim(msgs, budget):
        seen["budget"] = budget
        return msgs, {"before": 1, "after": 1, "hist": 0, "fewshot": 0, "inject_chars": 0, "over": 0}
    c._trim_prompt_to_budget = _fake_trim
    c._apply_prompt_budget([{"role": "user", "content": "hi"}], {})
    assert seen["budget"] == 32000
    c.config = _CM({})
    c._apply_prompt_budget([{"role": "user", "content": "hi"}], {})
    assert seen["budget"] == 12000


def test_build_contents_history_follows_tier(monkeypatch):
    # google.genai 不一定装了：用最小替身钉住「按档位切历史」这一件事
    import types as _pytypes
    import src.ai.ai_client as mod

    class _Part:
        def __init__(self, text=""):
            self.text = text

    class _Content:
        def __init__(self, role="", parts=None):
            self.role, self.parts = role, list(parts or [])
    monkeypatch.setattr(mod, "types", _pytypes.SimpleNamespace(Content=_Content, Part=_Part),
                        raising=False)
    c = _client({"context_depth": "deep"})
    c.max_conversation_history = 10
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(60)]
    out = c._build_contents("now", {}, hist)
    # Gemini contents：历史 + 当前一条
    assert len(out) == 41
    c.config = _CM({})
    assert len(c._build_contents("now", {}, hist)) == 11
    # 策略 0 轮 → 只有当前一条
    c.config = _CM({"context_depth": "ultra"})
    assert len(c._build_contents("now", {}, hist, context_rounds_override=0)) == 1
