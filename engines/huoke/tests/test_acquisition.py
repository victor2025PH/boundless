"""
Tests for Phase 6C: TikTok + Twitter modules, Acquisition Pipeline, EventBus integration.

Covers:
  - TikTokAutomation class structure and methods
  - TwitterAutomation class structure and methods
  - AcquisitionPipeline: loading, discover, warmup, engage
  - AcquisitionWorkflow YAML parsing
  - EscalationRule handling
  - Cross-platform EventBus coordination
  - Default acquisition workflow YAML
  - API endpoints for acquisition and events
"""

import json
import os
import time
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import yaml


# ── TikTok Module Tests ──────────────────────────────────────────────────

class TestTikTokAutomation:
    """Tests for TikTokAutomation module structure."""

    def test_import(self):
        from src.app_automation.tiktok import TikTokAutomation, PACKAGE_TRILL
        assert PACKAGE_TRILL == "com.ss.android.ugc.trill"

    def test_class_attributes(self):
        from src.app_automation.tiktok import TikTokAutomation
        assert TikTokAutomation.PLATFORM == "tiktok"
        assert TikTokAutomation.PACKAGE == "com.ss.android.ugc.trill"

    def test_inherits_base(self):
        from src.app_automation.tiktok import TikTokAutomation
        from src.app_automation.base_automation import BaseAutomation
        assert issubclass(TikTokAutomation, BaseAutomation)

    def test_has_browse_feed(self):
        from src.app_automation.tiktok import TikTokAutomation
        assert hasattr(TikTokAutomation, 'browse_feed')
        assert callable(getattr(TikTokAutomation, 'browse_feed'))

    def test_has_search_and_collect_leads(self):
        from src.app_automation.tiktok import TikTokAutomation
        assert hasattr(TikTokAutomation, 'search_and_collect_leads')

    def test_has_send_dm(self):
        from src.app_automation.tiktok import TikTokAutomation
        assert hasattr(TikTokAutomation, 'send_dm')

    def test_has_smart_follow(self):
        from src.app_automation.tiktok import TikTokAutomation
        assert hasattr(TikTokAutomation, 'smart_follow')

    def test_has_check_inbox(self):
        from src.app_automation.tiktok import TikTokAutomation
        assert hasattr(TikTokAutomation, 'check_inbox')

    def test_watch_duration_range(self):
        from src.app_automation.tiktok import TikTokAutomation
        tt = TikTokAutomation.__new__(TikTokAutomation)
        durations = [tt._watch_duration() for _ in range(100)]
        assert min(durations) >= 1.0
        assert max(durations) <= 40.0
        assert sum(durations) / len(durations) > 3.0

    def test_dismiss_dialog_texts(self):
        from src.app_automation.tiktok import _DISMISS_TEXTS
        assert len(_DISMISS_TEXTS) > 5
        assert "Skip" in _DISMISS_TEXTS
        assert "Allow" in _DISMISS_TEXTS


# ── Twitter Module Tests ─────────────────────────────────────────────────

class TestTwitterAutomation:
    """Tests for TwitterAutomation module structure."""

    def test_import(self):
        from src.app_automation.twitter import TwitterAutomation, PACKAGE
        assert PACKAGE == "com.twitter.android"

    def test_class_attributes(self):
        from src.app_automation.twitter import TwitterAutomation
        assert TwitterAutomation.PLATFORM == "twitter"
        assert TwitterAutomation.PACKAGE == "com.twitter.android"

    def test_inherits_base(self):
        from src.app_automation.twitter import TwitterAutomation
        from src.app_automation.base_automation import BaseAutomation
        assert issubclass(TwitterAutomation, BaseAutomation)

    def test_has_search_users(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'search_users')

    def test_has_follow_user(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'follow_user')

    def test_has_like_tweet(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'like_tweet')

    def test_has_retweet(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'retweet')

    def test_has_reply_tweet(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'reply_tweet')

    def test_has_send_dm(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'send_dm')

    def test_has_browse_timeline(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'browse_timeline')

    def test_has_search_and_engage(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'search_and_engage')

    def test_has_search_and_collect_leads(self):
        from src.app_automation.twitter import TwitterAutomation
        assert hasattr(TwitterAutomation, 'search_and_collect_leads')

    def test_dismiss_dialog_texts(self):
        from src.app_automation.twitter import _X_DISMISS_TEXTS
        assert len(_X_DISMISS_TEXTS) > 5
        assert "Not now" in _X_DISMISS_TEXTS

    def test_generate_reply_without_rewriter(self):
        from src.app_automation.twitter import TwitterAutomation
        tw = TwitterAutomation.__new__(TwitterAutomation)
        tw._rewriter = None
        reply = tw._generate_reply("AI startup")
        # With no rewriter, falls back to template-based reply (may still return text)
        # The method returns None ONLY if rewriter exists but fails AND templates fail
        assert reply is None or isinstance(reply, str)


# ── Acquisition Workflow YAML Tests ──────────────────────────────────────

class TestAcquisitionWorkflow:
    """Tests for AcquisitionWorkflow YAML parsing."""

    def test_parse_from_dict(self):
        from src.workflow.acquisition import AcquisitionWorkflow
        data = {
            "name": "test_wf",
            "description": "Test workflow",
            "target_platforms": ["linkedin", "twitter"],
            "stages": {
                "discovery": {"actions": [{"action": "search"}]},
            },
            "escalation_rules": [
                {
                    "trigger_event": "*.message_received",
                    "from_stage": "contacted",
                    "to_stage": "responded",
                    "actions": [],
                },
            ],
        }
        wf = AcquisitionWorkflow.from_dict(data)
        assert wf.name == "test_wf"
        assert len(wf.target_platforms) == 2
        assert len(wf.stages) == 1
        assert len(wf.escalation_rules) == 1
        assert wf.escalation_rules[0].trigger_event == "*.message_received"

    def test_parse_from_yaml_file(self):
        from src.workflow.acquisition import AcquisitionWorkflow
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml',
                                          delete=False, encoding='utf-8') as f:
            yaml.dump({
                "name": "yaml_test",
                "target_platforms": ["tiktok"],
                "stages": {},
                "escalation_rules": [],
            }, f)
            f.flush()
            wf = AcquisitionWorkflow.from_yaml(f.name)
        os.unlink(f.name)
        assert wf.name == "yaml_test"

    def test_default_acquisition_yaml_exists(self):
        path = Path(__file__).parent.parent / "config" / "workflows" / "default_acquisition.yaml"
        assert path.exists(), "default_acquisition.yaml should exist"

    def test_default_acquisition_yaml_parseable(self):
        from src.workflow.acquisition import AcquisitionWorkflow
        path = str(Path(__file__).parent.parent / "config" / "workflows" / "default_acquisition.yaml")
        wf = AcquisitionWorkflow.from_yaml(path)
        assert wf.name == "default_acquisition"
        assert len(wf.target_platforms) >= 3
        assert len(wf.stages) >= 3
        assert len(wf.escalation_rules) >= 3

    def test_escalation_rule_fields(self):
        from src.workflow.acquisition import EscalationRule
        rule = EscalationRule(
            trigger_event="linkedin.connection_accepted",
            from_stage="discovered",
            to_stage="contacted",
            delay_min=3600,
            delay_max=14400,
            priority=5,
            actions=[{"action": "linkedin.send_dm"}],
        )
        assert rule.trigger_event == "linkedin.connection_accepted"
        assert rule.from_stage == "discovered"
        assert rule.to_stage == "contacted"
        assert rule.delay_min == 3600
        assert rule.priority == 5
        assert len(rule.actions) == 1


# ── Acquisition Pipeline Tests ───────────────────────────────────────────

class TestAcquisitionPipeline:
    """Tests for AcquisitionPipeline orchestration."""

    def test_import(self):
        from src.workflow.acquisition import AcquisitionPipeline, get_acquisition_pipeline
        assert AcquisitionPipeline is not None

    def test_create_pipeline(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.engine import WorkflowExecutor
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(
            executor=WorkflowExecutor(),
            event_bus=EventBus(),
        )
        assert pipeline is not None
        assert pipeline.bus is not None
        assert pipeline.executor is not None

    def test_load_workflow(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        path = str(Path(__file__).parent.parent / "config" / "workflows" / "default_acquisition.yaml")
        wf = pipeline.load_workflow(path)
        assert wf.name == "default_acquisition"
        assert "default_acquisition" in pipeline._workflows

    def test_set_active_workflow(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        path = str(Path(__file__).parent.parent / "config" / "workflows" / "default_acquisition.yaml")
        pipeline.load_workflow(path)
        assert pipeline.set_active("default_acquisition")
        assert pipeline._active_workflow is not None
        assert pipeline._active_workflow.name == "default_acquisition"

    def test_set_active_nonexistent(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        assert not pipeline.set_active("nonexistent")

    def test_status(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        status = pipeline.status()
        assert "workflows_loaded" in status
        assert "active_workflow" in status
        assert "escalation_rules" in status
        assert "registered_actions" in status

    def test_pipeline_actions_registered(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        assert pipeline.registry.has("acquisition.discover")
        assert pipeline.registry.has("acquisition.warm_up")
        assert pipeline.registry.has("acquisition.engage")
        assert pipeline.registry.has("acquisition.run_full_pipeline")

    def test_default_outreach_message(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        msg = pipeline._default_outreach_message(
            {"name": "John Smith", "company": "Acme"}, "linkedin"
        )
        assert "John" in msg
        assert "Acme" in msg
        assert len(msg) > 20

    def test_default_message_per_platform(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        lead = {"name": "Alice", "company": ""}
        for platform in ["linkedin", "twitter", "tiktok", "whatsapp", "telegram"]:
            msg = pipeline._default_outreach_message(lead, platform)
            assert "Alice" in msg
            assert len(msg) > 15

    def test_warmup_actions_known_platforms(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        for platform in ["tiktok", "twitter", "linkedin"]:
            actions = pipeline._get_warmup_actions(platform)
            assert isinstance(actions, list)
            assert len(actions) >= 1

    def test_warmup_actions_unknown_platform(self):
        from src.workflow.acquisition import AcquisitionPipeline
        from src.workflow.event_bus import EventBus
        pipeline = AcquisitionPipeline(event_bus=EventBus())
        actions = pipeline._get_warmup_actions("unknown_platform")
        assert actions == []


# ── EventBus Cross-Platform Tests ────────────────────────────────────────

class TestEventBusCrossPlatform:
    """Tests for EventBus in cross-platform coordination scenarios."""

    def test_escalation_event_triggers_handler(self):
        from src.workflow.event_bus import EventBus, Event
        bus = EventBus()
        received = []
        bus.on("*.message_received", lambda e: received.append(e))
        bus.emit(Event(type="twitter.message_received",
                       data={"lead_id": 1}), synchronous=True)
        assert len(received) == 1
        assert received[0].data["lead_id"] == 1

    def test_wildcard_platform_matching(self):
        from src.workflow.event_bus import EventBus, Event
        bus = EventBus()
        received = []
        bus.on("linkedin.*", lambda e: received.append(e))
        bus.emit(Event(type="linkedin.connection_accepted"), synchronous=True)
        bus.emit(Event(type="linkedin.message_sent"), synchronous=True)
        bus.emit(Event(type="twitter.follow"), synchronous=True)
        assert len(received) == 2

    def test_lead_escalation_event(self):
        from src.workflow.event_bus import EventBus, Event
        bus = EventBus()
        escalations = []
        bus.on("lead.escalated", lambda e: escalations.append(e.data))
        bus.emit(Event(
            type="lead.escalated",
            data={"lead_id": 42, "from_stage": "contacted", "to_stage": "responded"},
        ), synchronous=True)
        assert len(escalations) == 1
        assert escalations[0]["lead_id"] == 42
        assert escalations[0]["to_stage"] == "responded"

    def test_event_history(self):
        from src.workflow.event_bus import EventBus, Event
        bus = EventBus()
        for i in range(5):
            bus.emit_simple(f"test.event_{i}", source="test", value=i)
        events = bus.recent_events(limit=3)
        assert len(events) == 3

    def test_cross_platform_chain(self):
        """Simulate: TikTok discover → Twitter follow → LinkedIn DM"""
        from src.workflow.event_bus import EventBus, Event
        bus = EventBus()
        chain = []
        bus.on("tiktok.lead_discovered", lambda e: chain.append("tiktok_discovered"))
        bus.on("twitter.follow_completed", lambda e: chain.append("twitter_follow"))
        bus.on("linkedin.dm_sent", lambda e: chain.append("linkedin_dm"))

        bus.emit(Event(type="tiktok.lead_discovered", data={"lead_id": 1}), synchronous=True)
        bus.emit(Event(type="twitter.follow_completed", data={"lead_id": 1}), synchronous=True)
        bus.emit(Event(type="linkedin.dm_sent", data={"lead_id": 1}), synchronous=True)

        assert chain == ["tiktok_discovered", "twitter_follow", "linkedin_dm"]


# ── Pipeline Stage Constants ─────────────────────────────────────────────

class TestPipelineConstants:
    """Tests for pipeline stage constants."""

    def test_pipeline_stages(self):
        from src.workflow.acquisition import PIPELINE_STAGES, STAGE_INDEX
        assert len(PIPELINE_STAGES) >= 5
        assert "new" in PIPELINE_STAGES
        assert "converted" in PIPELINE_STAGES

    def test_stage_ordering(self):
        from src.workflow.acquisition import STAGE_INDEX
        assert STAGE_INDEX["new"] < STAGE_INDEX["discovered"]
        assert STAGE_INDEX["discovered"] < STAGE_INDEX["converted"]


# ── Workflow __init__ Export Tests ────────────────────────────────────────

class TestWorkflowExports:
    """Test that acquisition module is properly exported."""

    def test_import_from_workflow(self):
        from src.workflow import (
            AcquisitionPipeline, AcquisitionWorkflow, EscalationRule,
            get_acquisition_pipeline,
        )
        assert AcquisitionPipeline is not None
        assert AcquisitionWorkflow is not None
        assert EscalationRule is not None
        assert callable(get_acquisition_pipeline)


# ── App Config YAML Tests ────────────────────────────────────────────────

class TestAppYAMLConfigs:
    """Verify TikTok and Twitter YAML configs exist and are valid."""

    def test_tiktok_yaml_exists(self):
        path = Path(__file__).parent.parent / "config" / "apps" / "tiktok.yaml"
        assert path.exists()

    def test_twitter_yaml_exists(self):
        path = Path(__file__).parent.parent / "config" / "apps" / "twitter.yaml"
        assert path.exists()

    def test_tiktok_yaml_has_actions(self):
        path = Path(__file__).parent.parent / "config" / "apps" / "tiktok.yaml"
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        assert "actions" in data
        assert len(data["actions"]) >= 3

    def test_twitter_yaml_has_actions(self):
        path = Path(__file__).parent.parent / "config" / "apps" / "twitter.yaml"
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        assert "actions" in data
        assert len(data["actions"]) >= 4

    def test_tiktok_yaml_has_compliance(self):
        path = Path(__file__).parent.parent / "config" / "apps" / "tiktok.yaml"
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        assert "compliance" in data

    def test_twitter_yaml_has_compliance(self):
        path = Path(__file__).parent.parent / "config" / "apps" / "twitter.yaml"
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        assert "compliance" in data
