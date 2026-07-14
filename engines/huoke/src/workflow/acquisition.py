"""
Acquisition Pipeline — multi-platform coordinated customer outreach.

Sits on top of WorkflowExecutor + EventBus + LeadsStore to orchestrate
the full lead lifecycle:

  Discovery → Warm-up → Engage → Qualify → Convert

Key capabilities:
  - YAML-defined acquisition workflows (multi-platform sequences)
  - Adaptive escalation: response on platform A triggers action on platform B
  - Lead-aware: tracks each lead's position in the pipeline
  - Priority queue: higher-scored leads get processed first
  - Session-aware: respects per-platform compliance limits
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from src.host.device_registry import config_dir

from .engine import WorkflowDef, WorkflowExecutor, WorkflowResult
from .event_bus import Event, EventBus, get_event_bus
from .actions import ActionRegistry, get_action_registry

log = logging.getLogger(__name__)

_WORKFLOWS_DIR = config_dir() / "workflows"


# ---------------------------------------------------------------------------
# Pipeline stage definitions
# ---------------------------------------------------------------------------

PIPELINE_STAGES = ("new", "discovered", "warmed_up", "engaged",
                   "qualified", "converting", "converted")

STAGE_INDEX = {s: i for i, s in enumerate(PIPELINE_STAGES)}


@dataclass
class EscalationRule:
    """When an event fires, move the lead to next stage + trigger actions."""
    trigger_event: str
    from_stage: str = ""
    to_stage: str = ""
    actions: List[Dict[str, Any]] = field(default_factory=list)
    delay_min: float = 0
    delay_max: float = 0
    priority: int = 0


@dataclass
class AcquisitionWorkflow:
    """Parsed from YAML — defines a complete acquisition strategy."""
    name: str
    description: str = ""
    target_platforms: List[str] = field(default_factory=list)
    stages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    escalation_rules: List[EscalationRule] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> AcquisitionWorkflow:
        rules = []
        for r in d.get("escalation_rules", []):
            rules.append(EscalationRule(
                trigger_event=r["trigger_event"],
                from_stage=r.get("from_stage", ""),
                to_stage=r.get("to_stage", ""),
                actions=r.get("actions", []),
                delay_min=r.get("delay_min", 0),
                delay_max=r.get("delay_max", 0),
                priority=r.get("priority", 0),
            ))
        return AcquisitionWorkflow(
            name=d.get("name", "unnamed"),
            description=d.get("description", ""),
            target_platforms=d.get("target_platforms", []),
            stages=d.get("stages", {}),
            escalation_rules=rules,
            variables=d.get("variables", {}),
        )

    @staticmethod
    def from_yaml(path: str) -> AcquisitionWorkflow:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return AcquisitionWorkflow.from_dict(data)


# ---------------------------------------------------------------------------
# Acquisition Pipeline
# ---------------------------------------------------------------------------

class AcquisitionPipeline:
    """
    Orchestrates multi-platform lead acquisition.

    Usage:
        pipeline = AcquisitionPipeline()
        pipeline.load_workflow("config/workflows/default_acquisition.yaml")
        pipeline.start()

        # Or run specific stages
        pipeline.discover(keywords=["AI startup"], platforms=["linkedin", "twitter"])
        pipeline.warm_up(lead_ids=[1, 2, 3])
        pipeline.engage(lead_ids=[1, 2])
    """

    def __init__(self, executor: Optional[WorkflowExecutor] = None,
                 event_bus: Optional[EventBus] = None):
        self.executor = executor or WorkflowExecutor()
        self.bus = event_bus or get_event_bus()
        self.registry = get_action_registry()
        self._workflows: Dict[str, AcquisitionWorkflow] = {}
        self._active_workflow: Optional[AcquisitionWorkflow] = None
        self._escalation_subs: List[str] = []
        self._running = False
        self._lock = threading.Lock()

        self._register_pipeline_actions()
        self._register_platform_actions()

    # ── Loading ────────────────────────────────────────────────────────────

    def load_workflow(self, path: str) -> AcquisitionWorkflow:
        wf = AcquisitionWorkflow.from_yaml(path)
        self._workflows[wf.name] = wf
        log.info("Acquisition workflow loaded: %s (%d stages, %d escalation rules)",
                 wf.name, len(wf.stages), len(wf.escalation_rules))
        return wf

    def load_all_workflows(self, directory: Optional[str] = None):
        d = Path(directory) if directory else _WORKFLOWS_DIR
        if not d.exists():
            return
        for f in d.glob("*.yaml"):
            if "acquisition" in f.stem:
                try:
                    self.load_workflow(str(f))
                except Exception as e:
                    log.warning("Failed to load workflow %s: %s", f.name, e)

    def set_active(self, workflow_name: str) -> bool:
        wf = self._workflows.get(workflow_name)
        if not wf:
            return False
        self._active_workflow = wf
        self._setup_escalation_rules(wf)
        return True

    # ── Core Operations ───────────────────────────────────────────────────

    def discover(self, keywords: List[str],
                 platforms: Optional[List[str]] = None,
                 max_per_keyword: int = 10,
                 device_id: Optional[str] = None) -> Dict[str, List[int]]:
        """
        Discover leads across platforms by keywords.
        Returns {platform: [lead_ids]} for all discovered leads.
        """
        from ..leads.store import get_leads_store
        store = get_leads_store()
        results: Dict[str, List[int]] = {}
        target_platforms = platforms or (
            self._active_workflow.target_platforms if self._active_workflow else
            ["linkedin", "twitter", "tiktok"]
        )

        for platform in target_platforms:
            platform_leads = []
            for keyword in keywords:
                action_name = f"{platform}.search_and_collect_leads"
                action = self.registry.get(action_name)
                if not action:
                    log.debug("No discover action for %s", platform)
                    continue

                fn, meta = action
                try:
                    lead_ids = fn(query=keyword, max_leads=max_per_keyword,
                                  device_id=device_id)
                    if isinstance(lead_ids, list):
                        platform_leads.extend(lead_ids)
                        for lid in lead_ids:
                            store.update_lead(lid, status="discovered" if store.get_lead(lid) and
                                              store.get_lead(lid).get("status") == "new" else None)
                            self.bus.emit_simple(
                                f"{platform}.lead_discovered",
                                source="acquisition",
                                lead_id=lid, keyword=keyword, platform=platform,
                            )
                except Exception as e:
                    log.warning("Discover failed on %s for '%s': %s", platform, keyword, e)

            results[platform] = platform_leads

        total = sum(len(v) for v in results.values())
        log.info("Discovery complete: %d leads across %d platforms", total, len(results))
        return results

    def warm_up(self, lead_ids: List[int],
                platforms: Optional[List[str]] = None,
                device_id: Optional[str] = None) -> Dict[str, int]:
        """
        Warm up leads with light engagement (likes, follows, views).
        Builds familiarity before direct contact.
        """
        from ..leads.store import get_leads_store
        store = get_leads_store()
        stats: Dict[str, int] = {}

        for lid in lead_ids:
            lead = store.get_lead(lid)
            if not lead:
                continue

            profiles = store.get_platform_profiles(lid)
            for pp in profiles:
                platform = pp["platform"]
                if platforms and platform not in platforms:
                    continue

                warmup_actions = self._get_warmup_actions(platform)
                for action_name, params in warmup_actions:
                    action = self.registry.get(action_name)
                    if not action:
                        continue
                    fn, _ = action
                    try:
                        fn(device_id=device_id, **params)
                        stats[platform] = stats.get(platform, 0) + 1
                        store.add_interaction(lid, platform, "warm_up",
                                              direction="outbound",
                                              content=action_name)
                    except Exception as e:
                        log.debug("Warmup action %s failed: %s", action_name, e)

                    time.sleep(random.uniform(2, 8))

            # Update status
            if lead.get("status") in ("new", "discovered"):
                store.update_lead(lid, status="contacted")
                self.bus.emit_simple("lead.warmed_up", source="acquisition",
                                     lead_id=lid)

        return stats

    def engage(self, lead_ids: List[int], message_template: str = "",
               platforms: Optional[List[str]] = None,
               device_id: Optional[str] = None) -> Dict[str, int]:
        """
        Direct engagement — send messages to leads.
        Uses AI to personalize each message based on lead profile.
        """
        from ..leads.store import get_leads_store
        store = get_leads_store()
        stats: Dict[str, int] = {"messages_sent": 0, "failures": 0}

        for lid in lead_ids:
            lead = store.get_lead(lid)
            if not lead:
                continue

            profiles = store.get_platform_profiles(lid)
            sent_on_any = False

            for pp in profiles:
                platform = pp["platform"]
                if platforms and platform not in platforms:
                    continue

                username = pp.get("username", "")
                if not username:
                    continue

                message = message_template or self._default_outreach_message(lead, platform)
                action_name = f"{platform}.send_dm"
                action = self.registry.get(action_name)
                if not action:
                    continue

                fn, _ = action
                try:
                    success = fn(recipient=username, message=message,
                                 device_id=device_id)
                    if success:
                        stats["messages_sent"] += 1
                        store.add_interaction(lid, platform, "send_message",
                                              direction="outbound",
                                              content=message[:200])
                        sent_on_any = True
                        self.bus.emit_simple(
                            f"{platform}.message_sent",
                            source="acquisition",
                            lead_id=lid, platform=platform,
                        )
                    else:
                        stats["failures"] += 1
                except Exception as e:
                    log.warning("Engage failed for lead %d on %s: %s", lid, platform, e)
                    stats["failures"] += 1

                time.sleep(random.uniform(10, 30))

            if sent_on_any and lead.get("status") in ("new", "discovered", "contacted"):
                store.update_lead(lid, status="contacted")

        return stats

    def run_full_pipeline(self, keywords: List[str],
                          platforms: Optional[List[str]] = None,
                          max_leads: int = 20,
                          device_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Run the complete acquisition pipeline:
          Discover → Warm-up → Engage
        """
        result = {
            "discovery": {},
            "warmup": {},
            "engagement": {},
            "total_leads": 0,
        }

        # Phase 1: Discover
        discovered = self.discover(keywords, platforms,
                                   max_per_keyword=max_leads // max(len(keywords), 1),
                                   device_id=device_id)
        result["discovery"] = {p: len(ids) for p, ids in discovered.items()}
        all_leads = []
        for ids in discovered.values():
            all_leads.extend(ids)
        all_leads = list(set(all_leads))
        result["total_leads"] = len(all_leads)

        if not all_leads:
            return result

        time.sleep(random.uniform(5, 15))

        # Phase 2: Warm-up (subset)
        warmup_ids = all_leads[:min(len(all_leads), max_leads)]
        result["warmup"] = self.warm_up(warmup_ids, platforms, device_id)

        time.sleep(random.uniform(10, 30))

        # Phase 3: Engage (top scored leads)
        from ..leads.store import get_leads_store
        store = get_leads_store()
        for lid in warmup_ids:
            store.update_score(lid)

        scored = [(lid, store.get_lead(lid)) for lid in warmup_ids]
        scored = [(lid, l) for lid, l in scored if l]
        scored.sort(key=lambda x: x[1].get("score", 0), reverse=True)
        engage_ids = [lid for lid, _ in scored[:max(len(scored) // 2, 1)]]

        result["engagement"] = self.engage(engage_ids, platforms=platforms,
                                            device_id=device_id)

        log.info("Full pipeline complete: %d discovered, %d warmed up, %d engaged",
                 result["total_leads"],
                 sum(result["warmup"].values()),
                 result["engagement"].get("messages_sent", 0))

        return result

    # ── Cross-Platform Escalation ─────────────────────────────────────────

    def _setup_escalation_rules(self, wf: AcquisitionWorkflow):
        for sub_id in self._escalation_subs:
            self.bus.off(sub_id)
        self._escalation_subs.clear()

        for rule in wf.escalation_rules:
            def make_handler(r):
                def handler(event: Event):
                    self._handle_escalation(r, event)
                handler.__name__ = f"escalation_{r.trigger_event}"
                return handler

            sub_id = self.bus.on(rule.trigger_event, make_handler(rule))
            self._escalation_subs.append(sub_id)

        log.info("Registered %d escalation rules", len(self._escalation_subs))

    def _handle_escalation(self, rule: EscalationRule, event: Event):
        """Process an escalation trigger."""
        from ..leads.store import get_leads_store
        store = get_leads_store()

        lead_id = event.data.get("lead_id")
        if not lead_id:
            return

        lead = store.get_lead(lead_id)
        if not lead:
            return

        current_stage = lead.get("status", "new")
        if rule.from_stage and current_stage != rule.from_stage:
            return

        # Apply delay
        if rule.delay_min > 0:
            delay = random.uniform(rule.delay_min, max(rule.delay_min, rule.delay_max))
            time.sleep(delay)

        # Update stage
        if rule.to_stage:
            store.update_lead(lead_id, status=rule.to_stage)
            log.info("Lead #%d escalated: %s → %s (triggered by %s)",
                     lead_id, current_stage, rule.to_stage, event.type)

        # Execute follow-up actions
        for action_def in rule.actions:
            action_name = action_def.get("action", "")
            params = action_def.get("params", {})
            params["lead_id"] = lead_id

            action = self.registry.get(action_name)
            if action:
                fn, _ = action
                try:
                    fn(**params)
                except Exception as e:
                    log.warning("Escalation action %s failed: %s", action_name, e)

        self.bus.emit_simple("lead.escalated", source="acquisition",
                             lead_id=lead_id, from_stage=current_stage,
                             to_stage=rule.to_stage or current_stage,
                             trigger=event.type)

    # ── Internal Helpers ──────────────────────────────────────────────────

    def _register_pipeline_actions(self):
        """Register pipeline operations as workflow actions."""
        self.registry.register("acquisition.discover", self.discover,
                               {"params": ["keywords", "platforms", "max_per_keyword"]})
        self.registry.register("acquisition.warm_up", self.warm_up,
                               {"params": ["lead_ids", "platforms"]})
        self.registry.register("acquisition.engage", self.engage,
                               {"params": ["lead_ids", "message_template", "platforms"]})
        self.registry.register("acquisition.run_full_pipeline", self.run_full_pipeline,
                               {"params": ["keywords", "platforms", "max_leads"]})

    def _register_platform_actions(self):
        """
        Register TikTok and Twitter platform actions (lazy import).
        Extends existing registrations for TG/LI/WA.
        """
        platform_modules = {
            "tiktok": ("..app_automation.tiktok", "TikTokAutomation",
                       ["launch", "browse_feed", "send_dm",
                        "search_and_collect_leads", "warmup_session",
                        "smart_follow", "check_and_chat_followbacks",
                        "check_inbox"]),
            "twitter": ("..app_automation.twitter", "TwitterAutomation",
                        ["launch", "search_users", "follow_user", "like_tweet",
                         "retweet", "reply_tweet", "send_dm",
                         "browse_timeline", "search_and_engage",
                         "search_and_collect_leads"]),
            "instagram": ("..app_automation.instagram", "InstagramAutomation",
                            ["launch", "browse_feed", "browse_hashtag",
                             "send_dm", "search_users", "follow_user",
                             "search_and_collect_leads"]),
        }

        for platform, (module_path, class_name, methods) in platform_modules.items():
            try:
                import importlib
                mod = importlib.import_module(module_path, package=__package__)
                cls = getattr(mod, class_name)
                instance = cls()
                self.registry.register_module(platform, instance, methods)
                log.info("Registered %d actions for %s", len(methods), platform)
            except Exception as e:
                log.debug("Could not register %s actions (will be available on demand): %s",
                          platform, e)

    def _get_warmup_actions(self, platform: str) -> List[tuple]:
        """Return lightweight warmup actions per platform."""
        warmup_map = {
            "tiktok": [
                ("tiktok.browse_feed", {"video_count": 3, "like_probability": 0.5}),
            ],
            "twitter": [
                ("twitter.browse_timeline", {"scroll_count": 3, "like_probability": 0.4}),
            ],
            "linkedin": [
                ("linkedin.browse_feed", {"scroll_count": 3}),
            ],
            "instagram": [
                ("instagram.browse_feed", {"scroll_count": 3, "like_probability": 0.4}),
            ],
        }
        return warmup_map.get(platform, [])

    def _default_outreach_message(self, lead: Dict[str, Any],
                                   platform: str) -> str:
        """Generate a default outreach message template."""
        name = lead.get("name", "").split()[0] if lead.get("name") else "there"
        company = lead.get("company", "")

        templates = {
            "linkedin": f"Hi {name}, I came across your profile"
                        f"{' at ' + company if company else ''} and found your work "
                        f"really interesting. Would love to connect and chat!",
            "twitter": f"Hi {name}! I've been following your posts and really "
                       f"appreciate your insights. Would love to connect!",
            "tiktok": f"Hi {name}! Love your content, really inspiring. "
                      f"Would love to connect!",
            "whatsapp": f"Hi {name}! Hope you don't mind me reaching out. "
                        f"I'd love to discuss a potential collaboration.",
            "telegram": f"Hi {name}! Great to connect with you. "
                        f"I've been following your work and would love to chat.",
        }
        return templates.get(platform, f"Hi {name}! Would love to connect.")

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        return {
            "workflows_loaded": list(self._workflows.keys()),
            "active_workflow": self._active_workflow.name if self._active_workflow else None,
            "escalation_rules": len(self._escalation_subs),
            "registered_actions": self.registry.list_by_platform(),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_pipeline: Optional[AcquisitionPipeline] = None
_pipeline_lock = threading.Lock()


def get_acquisition_pipeline() -> AcquisitionPipeline:
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = AcquisitionPipeline()
    return _pipeline
