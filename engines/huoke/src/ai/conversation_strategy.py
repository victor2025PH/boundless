# -*- coding: utf-8 -*-
"""
ConversationStrategy — Stage-aware system prompts and follow-up scheduling.

Extends the ConversationFSM with per-stage AI behavior:
- Each conversation stage has its own system prompt and constraints
- Follow-up scheduling with configurable delays and escalation
- Platform-specific tone adaptation
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class StageStrategy:
    """AI behavior configuration for a conversation stage."""
    stage: str
    system_prompt: str
    constraints: List[str] = field(default_factory=list)
    max_response_length: int = 200
    allowed_topics: List[str] = field(default_factory=list)
    forbidden_topics: List[str] = field(default_factory=list)
    follow_up_delay_hours: float = 24.0
    follow_up_templates: List[str] = field(default_factory=list)
    success_signals: List[str] = field(default_factory=list)
    failure_signals: List[str] = field(default_factory=list)


DEFAULT_STRATEGIES: Dict[str, StageStrategy] = {
    "cold_open": StageStrategy(
        stage="cold_open",
        system_prompt=(
            "You are making initial contact. Your goal is to spark interest within 3 messages. "
            "Be casual, warm, and curious. Ask about their interests or comment on their profile. "
            "DO NOT pitch anything yet. DO NOT mention products or business."
        ),
        constraints=[
            "Keep messages under 50 words",
            "Use 1-2 emojis max",
            "Ask exactly one question",
            "Never mention selling or business",
        ],
        max_response_length=100,
        allowed_topics=["hobbies", "travel", "interests", "profile"],
        forbidden_topics=["price", "product", "buy", "deal", "offer"],
        follow_up_delay_hours=24,
        follow_up_templates=[
            "Hey! Just came across your profile again. How's your day going?",
            "Hi there! Hope you're having a great day 😊",
        ],
        success_signals=["reply", "question", "emoji", "lol", "haha"],
        failure_signals=["stop", "spam", "block", "不感兴趣"],
    ),
    "warm_up": StageStrategy(
        stage="warm_up",
        system_prompt=(
            "The lead has responded positively. Build rapport through genuine conversation. "
            "Share relevant experiences. Show interest in their life/work. "
            "Subtly introduce what you do when naturally appropriate. "
            "DO NOT hard-sell. Keep it friendly and natural."
        ),
        constraints=[
            "Keep messages under 80 words",
            "Share a brief personal experience if relevant",
            "Transition naturally, no abrupt topic changes",
        ],
        max_response_length=160,
        follow_up_delay_hours=36,
        success_signals=["interested", "tell me more", "sounds cool", "that's great"],
        failure_signals=["busy", "not interested", "stop messaging"],
    ),
    "qualify": StageStrategy(
        stage="qualify",
        system_prompt=(
            "Time to understand if this lead is a good fit. "
            "Ask qualifying questions naturally: what they need, their situation, timeline. "
            "Listen actively and respond to their needs. "
            "If they seem like a match, gently move toward your solution."
        ),
        constraints=[
            "Ask one qualifying question per message",
            "Acknowledge their answers before asking more",
            "Keep it conversational, not interrogative",
        ],
        max_response_length=180,
        follow_up_delay_hours=48,
        success_signals=["need", "looking for", "problem", "challenge", "help"],
        failure_signals=["no budget", "not now", "maybe later"],
    ),
    "pitch": StageStrategy(
        stage="pitch",
        system_prompt=(
            "The lead is qualified and interested. Present your value proposition naturally. "
            "Focus on benefits, not features. Address their specific needs from qualifying. "
            "Use social proof if available. Make a clear but soft call-to-action."
        ),
        constraints=[
            "Connect solution to their stated needs",
            "Include one social proof element",
            "End with a clear next step",
            "No pressure tactics",
        ],
        max_response_length=250,
        follow_up_delay_hours=48,
        success_signals=["how much", "how does it work", "can we talk", "interested"],
        failure_signals=["too expensive", "not for me", "no thanks"],
    ),
    "follow_up": StageStrategy(
        stage="follow_up",
        system_prompt=(
            "The lead hasn't responded. Send a gentle, non-pushy follow-up. "
            "Reference your previous conversation. Offer new value or angle. "
            "Respect their time. One follow-up attempt, then cool down."
        ),
        constraints=[
            "Keep under 40 words",
            "Reference something specific from earlier",
            "No guilt-tripping",
            "Maximum 2 follow-ups per stage",
        ],
        max_response_length=80,
        follow_up_delay_hours=72,
        follow_up_templates=[
            "Hey {name}, just wanted to check in! Any thoughts on what we discussed?",
            "Hi {name}! No pressure at all, just wanted to see if you had any questions 😊",
        ],
    ),
    "cool_down": StageStrategy(
        stage="cool_down",
        system_prompt=(
            "The lead has declined or gone silent. Gracefully accept and leave the door open. "
            "Be professional and kind. Let them know they can reach out anytime."
        ),
        constraints=[
            "Keep under 30 words",
            "No further attempts after this",
            "Be genuinely kind",
        ],
        max_response_length=60,
        follow_up_delay_hours=0,
    ),
}

FSM_TO_STRATEGY_MAP = {
    "idle": "cold_open",
    "greeting": "cold_open",
    "qualifying": "qualify",
    "pitching": "pitch",
    "negotiating": "pitch",
    "converted": None,
    "dormant": "follow_up",
    "rejected": "cool_down",
}


class ConversationStrategyEngine:
    """Provides stage-aware AI prompts and follow-up decisions."""

    def __init__(self, strategies: Optional[Dict[str, StageStrategy]] = None,
                 platform_tones: Optional[Dict[str, str]] = None):
        self._strategies = strategies or DEFAULT_STRATEGIES
        self._platform_tones = platform_tones or {
            "tiktok": "casual, brief, emoji-friendly, Gen-Z vibe",
            "telegram": "slightly more detailed, professional but approachable",
            "whatsapp": "warm, conversational, use voice-note style language",
            "facebook": "friendly, slightly formal, community-oriented",
        }
        self._follow_up_schedule: Dict[str, float] = {}

    def get_strategy(self, fsm_state: str) -> Optional[StageStrategy]:
        """Map FSM state to strategy."""
        strategy_name = FSM_TO_STRATEGY_MAP.get(fsm_state, "cold_open")
        if not strategy_name:
            return None
        return self._strategies.get(strategy_name)

    def build_system_prompt(self, fsm_state: str, platform: str = "tiktok",
                            persona_prompt: str = "",
                            lead_context: str = "") -> str:
        """Build a complete system prompt combining persona + strategy + platform tone."""
        strategy = self.get_strategy(fsm_state)
        if not strategy:
            return persona_prompt

        tone = self._platform_tones.get(platform, "")
        parts = []
        if persona_prompt:
            parts.append(persona_prompt)

        parts.append(f"\n--- Current Stage: {strategy.stage.upper()} ---")
        parts.append(strategy.system_prompt)

        if strategy.constraints:
            parts.append("Constraints:")
            for c in strategy.constraints:
                parts.append(f"  - {c}")

        if tone:
            parts.append(f"\nPlatform tone ({platform}): {tone}")

        if strategy.forbidden_topics:
            parts.append(f"\nNEVER mention: {', '.join(strategy.forbidden_topics)}")

        if lead_context:
            parts.append(f"\nLead context: {lead_context}")

        parts.append(f"\nMax response length: {strategy.max_response_length} chars")
        return "\n".join(parts)

    def get_follow_up_action(self, lead_id: str, fsm_state: str,
                             hours_since_last: float,
                             lead_name: str = "") -> Optional[Dict]:
        """Determine if and what follow-up to send."""
        strategy = self.get_strategy(fsm_state)
        if not strategy or strategy.follow_up_delay_hours <= 0:
            return None

        if hours_since_last < strategy.follow_up_delay_hours:
            return None

        cooldown_key = f"{lead_id}:{fsm_state}"
        last_follow_up = self._follow_up_schedule.get(cooldown_key, 0)
        if time.time() - last_follow_up < strategy.follow_up_delay_hours * 3600:
            return None

        template = ""
        if strategy.follow_up_templates:
            template = random.choice(strategy.follow_up_templates)
            if lead_name:
                template = template.replace("{name}", lead_name)

        self._follow_up_schedule[cooldown_key] = time.time()

        return {
            "lead_id": lead_id,
            "stage": strategy.stage,
            "template": template,
            "delay_hours": strategy.follow_up_delay_hours,
        }

    def detect_stage_signals(self, message: str,
                             fsm_state: str) -> Optional[str]:
        """Detect success/failure signals to suggest state transitions."""
        strategy = self.get_strategy(fsm_state)
        if not strategy:
            return None

        lower = message.lower()
        for signal in strategy.failure_signals:
            if signal.lower() in lower:
                return "failure"

        for signal in strategy.success_signals:
            if signal.lower() in lower:
                return "success"

        return None

    def add_strategy(self, name: str, strategy: StageStrategy):
        self._strategies[name] = strategy

    def set_platform_tone(self, platform: str, tone: str):
        self._platform_tones[platform] = tone


_engine: Optional[ConversationStrategyEngine] = None


def get_strategy_engine() -> ConversationStrategyEngine:
    global _engine
    if _engine is None:
        _engine = ConversationStrategyEngine()
    return _engine
