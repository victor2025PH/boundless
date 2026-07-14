# -*- coding: utf-8 -*-
"""
Phase 5 unit tests — all logic that can be tested without physical devices.

Run: pytest tests/test_phase5_unit.py -v
"""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════
# 1. Conversation FSM
# ═══════════════════════════════════════════════════════════════════

class TestConversationFSM:
    """Test the conversation state machine logic."""

    def test_all_states_defined(self):
        from src.workflow.conversation_fsm import ConvState
        expected = {"idle", "greeting", "qualifying", "pitching",
                    "negotiating", "converted", "dormant", "rejected"}
        assert {s.value for s in ConvState} == expected

    def test_default_config_covers_key_states(self):
        from src.workflow.conversation_fsm import DEFAULT_FSM_CONFIG, ConvState
        for state in [ConvState.GREETING, ConvState.QUALIFYING,
                      ConvState.PITCHING, ConvState.NEGOTIATING, ConvState.DORMANT]:
            cfg = DEFAULT_FSM_CONFIG[state]
            assert cfg.follow_up_hours > 0
            assert cfg.max_follow_ups >= 1
            assert len(cfg.follow_up_templates) >= 1

    def test_greeting_config(self):
        from src.workflow.conversation_fsm import DEFAULT_FSM_CONFIG, ConvState
        cfg = DEFAULT_FSM_CONFIG[ConvState.GREETING]
        assert cfg.follow_up_hours == 24
        assert cfg.max_follow_ups == 3
        assert cfg.next_state_on_reply == "qualifying"
        assert "interested" in cfg.escalate_on_intent
        assert cfg.escalate_on_intent["negative"] == "rejected"

    def test_intent_to_state_mapping(self):
        from src.workflow.conversation_fsm import DEFAULT_FSM_CONFIG, ConvState
        # "meeting" intent should fast-track to negotiating from greeting
        assert DEFAULT_FSM_CONFIG[ConvState.GREETING].escalate_on_intent["meeting"] == "negotiating"
        # "interested" in pitching should go to negotiating
        assert DEFAULT_FSM_CONFIG[ConvState.PITCHING].escalate_on_intent["interested"] == "negotiating"

    def test_state_guidance_returns_text(self):
        from unittest.mock import MagicMock
        from src.app_automation.tiktok import TikTokAutomation
        mock_instance = MagicMock(spec=TikTokAutomation)
        mock_instance._active_country = ""
        mock_instance._get_state_guidance = TikTokAutomation._get_state_guidance.__get__(mock_instance)
        for state in ["greeting", "qualifying", "pitching", "negotiating", "dormant"]:
            guidance = mock_instance._get_state_guidance(state)
            assert len(guidance) > 20, f"Guidance for {state} too short"
        assert mock_instance._get_state_guidance("") == ""
        assert mock_instance._get_state_guidance("unknown") == ""

    def test_check_all_follow_ups_returns_list(self):
        from src.workflow.conversation_fsm import check_all_follow_ups
        result = check_all_follow_ups("tiktok", max_leads=5)
        assert isinstance(result, list)

    def test_conversation_summary_missing_lead(self):
        from src.workflow.conversation_fsm import get_conversation_summary
        result = get_conversation_summary(999999, "tiktok")
        assert result["state"] == "idle"


# ═══════════════════════════════════════════════════════════════════
# 2. Multi-language Message Rewriter
# ═══════════════════════════════════════════════════════════════════

class TestMultiLanguageRewriter:
    """Test message rewriter's multi-language support."""

    def test_language_system_prompts_exist(self):
        from src.ai.message_rewriter import SYSTEM_PROMPT_LANG, BATCH_SYSTEM_LANG
        assert "{language}" in SYSTEM_PROMPT_LANG
        assert "{language}" in BATCH_SYSTEM_LANG
        assert "{count}" in BATCH_SYSTEM_LANG

    def test_offline_rewrite_with_language(self):
        from src.ai.message_rewriter import MessageRewriter, RewriterConfig
        rw = MessageRewriter(config=RewriterConfig(offline_mode=True))
        result = rw.rewrite("Hello {name}!", {"name": "Marco"},
                            platform="tiktok", target_language="italian")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_pregenerate_with_language(self):
        from src.ai.message_rewriter import MessageRewriter, RewriterConfig
        rw = MessageRewriter(config=RewriterConfig(offline_mode=True))
        count = rw.pregenerate("Test message", count=3,
                               platform="tiktok", target_language="italian")
        assert count == 3

    def test_platform_tone_includes_tiktok(self):
        from src.ai.message_rewriter import RewriterConfig
        cfg = RewriterConfig()
        assert "tiktok" in cfg.platform_tone
        assert "emoji" in cfg.platform_tone["tiktok"].lower()

    def test_pool_key_differs_by_platform(self):
        from src.ai.message_rewriter import MessageRewriter
        k1 = MessageRewriter._pool_key("hello", "telegram")
        k2 = MessageRewriter._pool_key("hello", "tiktok")
        assert k1 != k2


# ═══════════════════════════════════════════════════════════════════
# 3. Conversion Funnel
# ═══════════════════════════════════════════════════════════════════

class TestConversionFunnel:
    """Test funnel analytics logic."""

    def test_funnel_structure(self):
        from src.leads.store import get_leads_store
        store = get_leads_store()
        funnel = store.get_conversion_funnel("tiktok", 30)

        assert "funnel" in funnel
        assert "rates" in funnel
        assert "engagement" in funnel
        assert "status_distribution" in funnel

        stages = funnel["funnel"]
        for key in ["discovered", "followed", "follow_back",
                     "chatted", "replied", "qualified", "converted"]:
            assert key in stages
            assert isinstance(stages[key], int)

    def test_funnel_rates_are_floats(self):
        from src.leads.store import get_leads_store
        store = get_leads_store()
        funnel = store.get_conversion_funnel("tiktok", 7)
        for rate_name, rate_val in funnel["rates"].items():
            assert isinstance(rate_val, float), f"{rate_name} is not float"

    def test_daily_funnel_length(self):
        from src.leads.store import get_leads_store
        store = get_leads_store()
        daily = store.get_daily_funnel("tiktok", 5)
        assert len(daily) == 5
        for day in daily:
            assert "date" in day
            assert "followed" in day

    def test_funnel_empty_platform(self):
        from src.leads.store import get_leads_store
        store = get_leads_store()
        funnel = store.get_conversion_funnel("nonexistent_platform", 7)
        assert funnel["funnel"]["discovered"] == 0


# ═══════════════════════════════════════════════════════════════════
# 4. Adaptive Compliance + Recovery
# ═══════════════════════════════════════════════════════════════════

class TestAdaptiveCompliance:
    """Test risk scoring and recovery mode."""

    def _fresh_ac(self):
        from src.behavior.adaptive_compliance import AdaptiveCompliance
        return AdaptiveCompliance()

    def test_clean_device_is_low_risk(self):
        ac = self._fresh_ac()
        profile = ac.get_risk_profile("CLEAN")
        assert profile["risk_score"] == 0.0
        assert profile["risk_level"] == "low"
        assert profile["multiplier"] == 1.0
        assert profile["recovering"] is False

    def test_success_keeps_low_risk(self):
        ac = self._fresh_ac()
        for _ in range(10):
            ac.record_outcome("GOOD", "follow", success=True)
        assert ac.get_risk_profile("GOOD")["risk_level"] in ("low", "medium")

    def test_failures_increase_risk(self):
        ac = self._fresh_ac()
        for _ in range(5):
            ac.record_outcome("MIX", "follow", True)
        for _ in range(3):
            ac.record_outcome("MIX", "follow", False)
        assert ac.get_risk_profile("MIX")["risk_score"] > 0.2

    def test_many_failures_trigger_high_risk(self):
        ac = self._fresh_ac()
        for _ in range(8):
            ac.record_outcome("BAD", "follow", False)
        profile = ac.get_risk_profile("BAD")
        assert profile["risk_level"] in ("high", "critical")
        assert profile["multiplier"] <= 0.4

    def test_adjusted_limit_scales_down(self):
        ac = self._fresh_ac()
        assert ac.get_adjusted_limit("CLEAN", 20) == 20
        for _ in range(10):
            ac.record_outcome("RISKY", "follow", False)
        assert ac.get_adjusted_limit("RISKY", 20) < 20

    def test_adjusted_cooldown_scales_up(self):
        ac = self._fresh_ac()
        for _ in range(10):
            ac.record_outcome("SLOW", "follow", False)
        assert ac.get_adjusted_cooldown("SLOW", 10.0) > 10.0

    # Recovery tests

    def test_no_recovery_at_start(self):
        ac = self._fresh_ac()
        assert not ac.is_recovering("FRESH")

    def test_recovery_triggered_by_failures(self):
        ac = self._fresh_ac()
        for _ in range(10):
            ac.record_outcome("FAIL", "follow", False)
        assert ac.is_recovering("FAIL")

    def test_recovery_blocks_sensitive_actions(self):
        ac = self._fresh_ac()
        ac.force_recovery("BLOCKED", "test")
        assert ac.should_skip("BLOCKED", "follow") is True
        assert ac.should_skip("BLOCKED", "send_dm") is True
        assert ac.should_skip("BLOCKED", "comment") is True
        assert ac.should_skip("BLOCKED", "browse_feed") is False
        assert ac.should_skip("BLOCKED", "like") is False

    def test_recovery_warmup_params(self):
        ac = self._fresh_ac()
        ac.force_recovery("WARMUP", "test")
        params = ac.get_recovery_warmup_params("WARMUP")
        assert params["phase"] == "cold_start"
        assert params["like_probability"] == 0.05
        assert params["comment_post_prob"] == 0.0
        assert params["is_recovery"] is True

    def test_recovery_exits_after_sessions(self):
        ac = self._fresh_ac()
        ac.force_recovery("PROG", "test")
        for _ in range(3):
            ac.record_recovery_session("PROG")
        assert not ac.is_recovering("PROG")

    def test_risk_decays_during_recovery(self):
        ac = self._fresh_ac()
        for _ in range(8):
            ac.record_outcome("DECAY", "follow", False)
        initial = ac.get_risk_profile("DECAY")["risk_score"]
        for _ in range(3):
            ac.record_recovery_session("DECAY")
        final = ac.get_risk_profile("DECAY")["risk_score"]
        assert final < initial

    def test_force_recovery_and_exit(self):
        ac = self._fresh_ac()
        ac.force_recovery("MANUAL", "test")
        assert ac.is_recovering("MANUAL")
        ac.force_exit_recovery("MANUAL")
        assert not ac.is_recovering("MANUAL")

    def test_profile_shows_recovery_info(self):
        ac = self._fresh_ac()
        assert ac.get_risk_profile("NORM")["recovery"] is None
        ac.force_recovery("REC", "test")
        profile = ac.get_risk_profile("REC")
        assert profile["recovering"] is True
        assert profile["recovery"]["sessions_required"] == 3


# ═══════════════════════════════════════════════════════════════════
# 5. Geo-IP Check (Logic Only)
# ═══════════════════════════════════════════════════════════════════

class TestGeoCheck:
    """Test geo-IP validation logic (no network calls)."""

    def test_ip_validation(self):
        from src.behavior.geo_check import _is_valid_ip
        assert _is_valid_ip("1.2.3.4")
        assert _is_valid_ip("192.168.1.1")
        assert _is_valid_ip("0.0.0.0")
        assert _is_valid_ip("255.255.255.255")
        assert not _is_valid_ip("256.0.0.1")
        assert not _is_valid_ip("abc")
        assert not _is_valid_ip("1.2.3")
        assert not _is_valid_ip("1.2.3.4.5")
        assert not _is_valid_ip("")

    def test_country_aliases(self):
        from src.behavior.geo_check import COUNTRY_ALIASES
        assert "it" in COUNTRY_ALIASES["italy"]
        assert "de" in COUNTRY_ALIASES["germany"]
        assert "fr" in COUNTRY_ALIASES["france"]
        assert "es" in COUNTRY_ALIASES["spain"]
        assert "br" in COUNTRY_ALIASES["brazil"]

    def test_geo_result_dataclass(self):
        from src.behavior.geo_check import GeoCheckResult
        r = GeoCheckResult(
            device_id="TEST",
            public_ip="1.2.3.4",
            detected_country="Italy",
            detected_country_code="IT",
            expected_country="italy",
            matches=True,
        )
        assert r.matches
        assert r.vpn_detected is False


# ═══════════════════════════════════════════════════════════════════
# 6. Geo Strategy Config
# ═══════════════════════════════════════════════════════════════════

class TestGeoStrategy:
    """Test geographic content strategy loading."""

    def test_load_italy(self):
        from src.app_automation.tiktok import get_geo_strategy
        geo = get_geo_strategy("italy")
        assert geo["language"] == "italian"
        assert geo["timezone"] == "Europe/Rome"
        assert len(geo["hashtags"]["popular"]) >= 5
        assert len(geo["comments"]["generic"]) >= 3

    def test_load_germany(self):
        from src.app_automation.tiktok import get_geo_strategy
        geo = get_geo_strategy("germany")
        assert geo["language"] == "german"
        assert "#deutschland" in geo["hashtags"]["popular"]

    def test_load_unknown_country_fallback(self):
        from src.app_automation.tiktok import get_geo_strategy
        geo = get_geo_strategy("unknown_country")
        assert "engagement" in geo

    def test_geo_comments(self):
        from src.app_automation.tiktok import get_geo_comments
        comments = get_geo_comments("italy", "generic")
        assert len(comments) >= 3
        assert any("🇮🇹" in c for c in comments)

    def test_geo_hashtags(self):
        from src.app_automation.tiktok import get_geo_hashtags
        tags = get_geo_hashtags("italy", "popular")
        assert "#italia" in tags


# ═══════════════════════════════════════════════════════════════════
# 7. A/B Testing
# ═══════════════════════════════════════════════════════════════════

class TestABTesting:
    """Test A/B experiment framework."""

    @pytest.fixture(autouse=True)
    def _init_db(self):
        from src.host.database import init_db
        init_db()

    def test_create_experiment(self):
        from src.host.ab_testing import ABTestStore
        ab = ABTestStore()
        exp_id = ab.create("test_exp_unit", "test",
                           variants=["a", "b", "c"])
        assert len(exp_id) > 0

    def test_assign_is_deterministic(self):
        from src.host.ab_testing import ABTestStore
        ab = ABTestStore()
        ab.create("det_test", "test", variants=["x", "y"])
        v1 = ab.assign("det_test", device_id="DEV01")
        v2 = ab.assign("det_test", device_id="DEV01")
        assert v1 == v2  # same device → same variant

    def test_different_devices_can_get_different_variants(self):
        from src.host.ab_testing import ABTestStore
        ab = ABTestStore()
        ab.create("split_test", "test", variants=["alpha", "beta"])
        variants_seen = set()
        for i in range(20):
            v = ab.assign("split_test", device_id=f"DEV_{i:04d}")
            variants_seen.add(v)
        assert len(variants_seen) == 2  # both variants should appear

    def test_record_and_analyze(self):
        import uuid
        from src.host.ab_testing import ABTestStore
        ab = ABTestStore()
        name = f"analyze_{uuid.uuid4().hex[:8]}"
        ab.create(name, "test", variants=["ctrl", "var"])
        for _ in range(5):
            ab.record(name, "ctrl", "sent")
        for _ in range(3):
            ab.record(name, "ctrl", "reply_received")
        ab.record(name, "var", "sent")
        results = ab.analyze(name)
        assert results["ctrl"]["sent"] == 5
        assert results["ctrl"]["reply_received"] == 3
        assert results["ctrl"]["reply_rate"] == 0.6

    def test_best_variant(self):
        from src.host.ab_testing import ABTestStore
        ab = ABTestStore()
        ab.create("best_test", "test", variants=["good", "bad"])
        for _ in range(10):
            ab.record("best_test", "good", "sent")
            ab.record("best_test", "bad", "sent")
        for _ in range(8):
            ab.record("best_test", "good", "reply_received")
        for _ in range(2):
            ab.record("best_test", "bad", "reply_received")
        best = ab.best_variant("best_test", metric="reply_received")
        assert best == "good"

    def test_end_experiment(self):
        from src.host.ab_testing import ABTestStore
        ab = ABTestStore()
        ab.create("end_test", "test")
        ab.end_experiment("end_test")
        exps = ab.list_experiments(status="completed")
        names = [e["name"] for e in exps]
        assert "end_test" in names


# ═══════════════════════════════════════════════════════════════════
# 8. DeviceStateStore
# ═══════════════════════════════════════════════════════════════════

class TestDeviceStateStore:
    """Test device state management."""

    @pytest.fixture(autouse=True)
    def _init_db(self):
        from src.host.database import init_db
        init_db()

    def test_init_device(self):
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device("TEST_INIT")
        assert ds.get_phase("TEST_INIT") == "cold_start"
        assert ds.can_follow("TEST_INIT") is False

    def test_set_get_int(self):
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.set("TEST_INT", "counter", 42)
        assert ds.get_int("TEST_INT", "counter") == 42

    def test_increment(self):
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.set("TEST_INC", "val", 10)
        result = ds.increment("TEST_INC", "val", 5)
        assert result == 15

    def test_device_summary(self):
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device("TEST_SUM")
        summary = ds.get_device_summary("TEST_SUM")
        assert "phase" in summary
        assert "algorithm_score" in summary
        assert "recovery_active" in summary

    def test_algorithm_learning_score(self):
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.record_feed_analysis("TEST_ALGO", target_videos=8, total_videos=20)
        score = ds.get_algorithm_learning_score("TEST_ALGO")
        assert 0.3 <= score <= 0.5

    def test_seed_quality(self):
        from src.host.device_state import get_device_state_store
        # 与共享 data/openclaw.db 中历史 seed 隔离，避免 he 区下其它账号更高 hit_rate 抢首条
        country = f"italy_ut_{uuid.uuid4().hex[:12]}"
        ds = get_device_state_store("tiktok")
        ds.record_seed_quality("@good_seed", country,
                               checked=20, followed=15, hit_rate=0.75)
        ds.record_seed_quality("@bad_seed", country,
                               checked=20, followed=2, hit_rate=0.10)
        best = ds.get_best_seeds(country, top_n=5)
        assert len(best) >= 2
        assert best[0]["username"] == "@good_seed"


# ═══════════════════════════════════════════════════════════════════
# 9. Task Types & Schemas
# ═══════════════════════════════════════════════════════════════════

class TestSchemas:
    """Test task type definitions."""

    def test_tiktok_task_types(self):
        from src.host.schemas import TaskType
        tiktok_types = [t for t in TaskType if t.value.startswith("tiktok_")]
        assert len(tiktok_types) >= 10
        names = {t.value for t in tiktok_types}
        assert "tiktok_warmup" in names
        assert "tiktok_follow" in names
        assert "tiktok_chat" in names
        assert "tiktok_check_inbox" in names
        assert "tiktok_follow_up" in names
        assert "tiktok_send_dm" in names

    def test_task_create_model(self):
        from src.host.schemas import TaskCreate, TaskType
        task = TaskCreate(
            type=TaskType.TIKTOK_WARMUP,
            device_id="TEST",
            params={"phase": "cold_start", "target_country": "italy"},
        )
        assert task.type == TaskType.TIKTOK_WARMUP
        assert task.params["target_country"] == "italy"


# ═══════════════════════════════════════════════════════════════════
# 10. Compliance Guard
# ═══════════════════════════════════════════════════════════════════

class TestComplianceGuard:
    """Test rate limiting logic."""

    def test_tiktok_limits_defined(self):
        from src.behavior.compliance_guard import DEFAULT_LIMITS
        tiktok = DEFAULT_LIMITS["tiktok"]
        assert "follow" in tiktok.actions
        assert "send_dm" in tiktok.actions
        assert tiktok.actions["follow"].hourly == 8
        assert tiktok.actions["follow"].daily == 30
        assert tiktok.actions["send_dm"].hourly == 5

    def test_check_allows_first_action(self):
        from src.behavior.compliance_guard import ComplianceGuard
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            guard = ComplianceGuard(db_path=db_path)
            assert guard.check("tiktok", "follow") is True
        finally:
            os.unlink(db_path)

    def test_quota_exceeded_on_overflow(self, monkeypatch):
        """2026-05-04 Stage G.2: monkeypatch time.sleep 跳过 _check_cooldown
        的真等待. test 意图是验 quota 超限 raise QuotaExceeded, 但 check()
        先调 _check_cooldown(time.sleep(N)) → 阻塞 N 秒触发 pytest --timeout
        卡死. cooldown 行为已由 test_cooldown_blocks_too_soon 单独验证, 这里
        只关心 quota 边界."""
        import time as _time
        monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

        from src.behavior.compliance_guard import ComplianceGuard, QuotaExceeded
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            guard = ComplianceGuard(db_path=db_path)
            for _ in range(8):
                guard.record("tiktok", "follow", account="test")
            with pytest.raises(QuotaExceeded):
                guard.check("tiktok", "follow", account="test")
        finally:
            os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════════
# 11. AutoReply (extra_context support)
# ═══════════════════════════════════════════════════════════════════

class TestAutoReply:
    """Test AutoReply engine interface."""

    def test_generate_reply_accepts_extra_context(self):
        import inspect
        from src.ai.auto_reply import AutoReply
        sig = inspect.signature(AutoReply.generate_reply)
        assert "extra_context" in sig.parameters

    def test_persona_to_system_prompt(self):
        from src.ai.auto_reply import Persona
        p = Persona(
            name="Test",
            description="test bot",
            language="italian",
            tone="casual",
            response_style="brief",
            knowledge="You are helpful.",
            platform="tiktok",
        )
        prompt = p.to_system_prompt()
        assert "tiktok" in prompt.lower() or "Test" in prompt


# ═══════════════════════════════════════════════════════════════════
# 12. Schedule Config
# ═══════════════════════════════════════════════════════════════════

class TestScheduleConfig:
    """Test schedule YAML configuration."""

    def test_schedules_parse(self):
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "tiktok_schedules.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        schedules = config["schedules"]
        assert len(schedules) >= 10

    def test_all_schedules_have_required_fields(self):
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "tiktok_schedules.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        for s in config["schedules"]:
            assert "name" in s, f"Missing name in schedule"
            assert "cron" in s, f"Missing cron in {s.get('name')}"
            assert "task_type" in s, f"Missing task_type in {s.get('name')}"

    def test_follow_up_schedules_exist(self):
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "tiktok_schedules.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        follow_up = [s for s in config["schedules"]
                     if s["task_type"] == "tiktok_follow_up"]
        assert len(follow_up) >= 2

    def test_smart_schedule_configs(self):
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "tiktok_schedules.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        for s in config["schedules"]:
            ss = s.get("params", {}).get("smart_schedule")
            if ss:
                assert "timezone" in ss
                assert "activity_window" in ss
