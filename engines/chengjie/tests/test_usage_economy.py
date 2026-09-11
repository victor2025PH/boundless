"""B9 经济档 overlay：默认恒等；economy / 钱包降级才压帽。不撕 A 线四档只抬不压。"""
from __future__ import annotations

from src.ai import context_depth as cd
from src.ai import usage_economy as ue
from src.ai.language_rule import skip_output_lang_block
from src.companion.deep_persona import build_deep_persona_block
from src.companion.goals import profile_fill as pf


def test_off_is_identity():
    cfg = {"ai": {}}
    assert ue.resolve(cfg).on is False
    assert ue.cap_history(40, cfg) == 40
    assert ue.cap_prompt_budget(12000, cfg) == 12000
    assert ue.cap_prompt_budget(0, cfg) == 0
    assert ue.cap_memory(16, 3000, cfg) == (16, 3000)
    assert ue.cap_extract_daily(12, cfg) == 12
    assert ue.persona_detail(cfg, "full") == "full"
    assert ue.skip_deep_persona(cfg) is False


def test_explicit_economy_caps():
    cfg = {"ai": {"usage_mode": "economy"}}
    assert ue.resolve(cfg).on is True
    assert ue.cap_history(40, cfg) == 4
    assert ue.cap_history(0, cfg) == 0
    assert ue.cap_prompt_budget(32000, cfg) == 6000
    assert ue.cap_memory(16, 3000, cfg) == (4, 600)
    assert ue.cap_extract_daily(12, cfg) == 3
    assert ue.cap_verbatim_rounds(20, cfg) == 2
    assert ue.cap_verbatim_msgs(40, cfg) == 4
    assert ue.persona_detail(cfg, "full") == "compact"
    assert ue.skip_deep_persona(cfg) is True


def test_context_depth_standard_unchanged_when_economy_off():
    cfg = {"ai": {"context_depth": "standard"}}
    assert cd.history_limit(cfg, 10) == 10
    assert cd.prompt_budget(cfg, 12000) == 12000
    assert cd.memory_limits(cfg, 8, 1200) == (8, 1200)


def test_context_depth_deep_then_economy_caps():
    cfg = {"ai": {"context_depth": "deep", "usage_mode": "lite"}}
    assert cd.history_limit(cfg, 10) == 4          # 先抬到 40 再压到 4
    assert cd.prompt_budget(cfg, 12000) == 6000
    assert cd.memory_limits(cfg, 8, 1200) == (4, 600)
    assert cd.verbatim_rounds(cfg, 5) == 2
    assert cd.verbatim_msgs(cfg, 10) == 4


def test_degrade_turns_economy_on(monkeypatch):
    monkeypatch.setattr("src.licensing.token_ledger.should_degrade_action",
                        lambda *a, **k: True)
    assert ue.resolve({"ai": {}}).on is True
    assert ue.resolve({"ai": {"economy_on_degrade": False}}).on is False


def test_skip_output_lang_only_when_flag_set():
    assert skip_output_lang_block({}) is False
    assert skip_output_lang_block({"_lang_rule_emitted": True}) is True


def test_deep_persona_skipped_in_economy(monkeypatch):
    monkeypatch.setattr(ue, "skip_deep_persona", lambda cfg=None: True)
    from datetime import datetime, timezone
    persona = {"id": "lin", "life_arc": {"theme": "T", "beats": ["便利店忙"]}}
    out = build_deep_persona_block(
        persona, now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        cfg={"enabled": True, "life_line": True})
    assert out == ""


def test_usage_mode_i18n_and_template_wired():
    from src.web.i18n_packs.reply_settings_page import EN, ZH
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src/web/templates/reply_settings.html"
            ).read_text(encoding="utf-8")
    for k in ("rps_usage_mode", "rps_usage_mode_full", "rps_usage_mode_economy",
              "rps_usage_mode_hint"):
        assert ZH.get(k) and EN.get(k), k
    assert 'id="rps-usage-mode"' in html
    assert 'path: "ai.usage_mode"' in html
    assert 'el: "rps-usage-mode"' in html


def test_describe_overlay_and_not_a_fifth_tier():
    assert "economy" not in cd.TIER_KEYS
    off = ue.describe({"ai": {}})
    assert off["on"] is False and not off["reason"]
    on = ue.describe({"ai": {"usage_mode": "economy"}})
    assert on["on"] is True and on["reason"] == "explicit"
    assert on["history_cap"] == 4 and on["skip_deep_persona"] is True


def test_extract_daily_respects_economy():
    cfg = {"companion": {"goals": {"profile_llm": {"per_conv_daily": 12}}},
           "ai": {"usage_mode": "economy"}}
    assert pf._per_conv_daily_cap(cfg) == 3
    cfg_off = {"companion": {"goals": {"profile_llm": {"per_conv_daily": 12}}}}
    assert pf._per_conv_daily_cap(cfg_off) == 12
