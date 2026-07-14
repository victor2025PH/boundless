"""
AutoReply — Intelligent auto-reply engine with intent classification.

Key optimization: Before generating an expensive LLM reply, classify the
incoming message's intent. This saves ~60% of API calls:
- NEEDS_REPLY (question, request, greeting) → generate reply
- OPTIONAL (casual chat, reaction) → 50% chance reply, 50% just acknowledge
- NO_REPLY (notification, bot msg, group noise) → skip

Also supports:
- Per-account persona configuration
- Conversation history for context-aware replies
- Platform-specific tone adaptation
- Reply delay simulation (via HumanBehavior)
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple
import random

from .llm_client import LLMClient, get_llm_client
from .conversation_memory import ConversationMemory
from .conversation_strategy import get_strategy_engine
from .reply_filter import get_reply_filter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent Classification
# ---------------------------------------------------------------------------

class Intent:
    NEEDS_REPLY = "needs_reply"
    OPTIONAL = "optional"
    NO_REPLY = "no_reply"


def classify_intent(message: str) -> str:
    """
    Fast rule-based intent classification. No LLM call needed.
    Falls back to OPTIONAL for ambiguous messages.
    """
    text = message.strip().lower()

    if not text or len(text) < 2:
        return Intent.NO_REPLY

    # Bot / system messages
    bot_patterns = [
        r"^/\w+",
        r"joined the group",
        r"left the group",
        r"pinned a message",
        r"changed the group",
        r"加入了群组",
        r"离开了群组",
        r"置顶了消息",
    ]
    for pat in bot_patterns:
        if re.search(pat, text):
            return Intent.NO_REPLY

    # Questions → always reply
    if text.endswith("?") or text.endswith("？"):
        return Intent.NEEDS_REPLY
    question_words = ["what", "when", "where", "how", "why", "who", "can you",
                      "could you", "would you", "do you", "are you", "is there",
                      "什么", "怎么", "为什么", "在哪", "能不能", "可以", "吗",
                      "是否", "有没有", "请问"]
    for qw in question_words:
        if qw in text:
            return Intent.NEEDS_REPLY

    # Greetings → reply
    greetings = ["hello", "hi ", "hey", "good morning", "good evening",
                 "你好", "嗨", "早上好", "晚上好", "在吗"]
    for g in greetings:
        if text.startswith(g) or text == g.strip():
            return Intent.NEEDS_REPLY

    # Requests → reply
    request_signals = ["please", "help", "send", "share", "tell me",
                       "请", "帮", "发给我", "告诉我", "分享"]
    for r in request_signals:
        if r in text:
            return Intent.NEEDS_REPLY

    # Very short messages (reactions, emojis, stickers)
    if len(text) <= 5:
        return Intent.OPTIONAL

    # Default: optional
    return Intent.OPTIONAL


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

@dataclass
class Persona:
    name: str = "default"
    description: str = "a helpful and friendly person"
    language: str = "auto"
    tone: str = "friendly"
    response_style: str = "concise"
    knowledge: str = ""
    platform: str = "telegram"

    def to_system_prompt(self) -> str:
        lang_hint = ""
        if self.language != "auto":
            lang_hint = f" Reply in {self.language}."

        return (
            f"You are {self.name}, {self.description}. "
            f"Your tone is {self.tone} and you keep responses {self.response_style}.{lang_hint}\n"
            f"Platform: {self.platform}. Respond naturally as a real person would on this platform.\n"
            f"{self.knowledge}"
        ).strip()


DEFAULT_PERSONAS: Dict[str, Persona] = {
    "professional": Persona(
        name="Professional",
        description="a business professional",
        tone="professional but approachable",
        response_style="clear and concise",
    ),
    "casual": Persona(
        name="Casual",
        description="a friendly acquaintance",
        tone="casual and warm",
        response_style="brief and natural",
    ),
    "assistant": Persona(
        name="Assistant",
        description="a helpful AI assistant",
        tone="polite and efficient",
        response_style="detailed when needed, brief otherwise",
    ),
}


# ---------------------------------------------------------------------------
# Conversation History
# ---------------------------------------------------------------------------

@dataclass
class ConversationHistory:
    max_messages: int = 20
    _messages: Deque = field(default=None, repr=False)

    def __post_init__(self):
        self._messages = deque(maxlen=self.max_messages)

    def add(self, role: str, content: str, timestamp: Optional[float] = None):
        self._messages.append({
            "role": role,
            "content": content,
            "ts": timestamp or time.time(),
        })

    def to_messages(self, limit: int = 10) -> List[Dict[str, str]]:
        recent = list(self._messages)[-limit:]
        return [{"role": m["role"], "content": m["content"]} for m in recent]

    def clear(self):
        self._messages.clear()

    @property
    def length(self) -> int:
        return len(self._messages)


# ---------------------------------------------------------------------------
# AutoReply Engine
# ---------------------------------------------------------------------------

class AutoReply:
    """
    Intelligent auto-reply engine.

    Usage:
        ar = AutoReply()
        reply = ar.generate_reply(
            message="Hey, how are you doing?",
            sender="Alice",
            platform="telegram",
            persona="casual",
        )
        if reply:
            # reply.text = "Hey Alice! Doing great, thanks for asking. How about you?"
            # reply.delay_sec = 4.2  (simulated thinking time)
            ...
    """

    def __init__(self, client: Optional[LLMClient] = None,
                 personas: Optional[Dict[str, Persona]] = None,
                 use_persistent_memory: bool = True):
        self._client = client or get_llm_client()
        self._personas = personas or DEFAULT_PERSONAS
        self._histories: Dict[str, ConversationHistory] = {}
        self._memory: Optional[ConversationMemory] = None
        if use_persistent_memory:
            try:
                self._memory = ConversationMemory.get_instance()
            except Exception as e:
                log.warning("ConversationMemory init failed, using in-memory only: %s", e)

    def generate_reply(self, message: str, sender: str = "",
                       platform: str = "telegram",
                       persona: str = "casual",
                       conversation_id: str = "",
                       extra_context: str = "",
                       fsm_state: str = "") -> Optional[ReplyResult]:
        """
        Generate a reply for an incoming message.

        Returns None if no reply should be sent (intent = NO_REPLY).
        Returns ReplyResult with text + suggested delay.

        extra_context: additional state-specific instructions injected into system prompt
        """
        intent = classify_intent(message)

        if intent == Intent.NO_REPLY:
            log.debug("AutoReply: skipping (no_reply intent): %s", message[:50])
            return None

        if intent == Intent.OPTIONAL and random.random() > 0.5:
            log.debug("AutoReply: skipping (optional, coin flip): %s", message[:50])
            return None

        conv_key = conversation_id or f"{platform}:{sender}"
        if conv_key not in self._histories:
            self._histories[conv_key] = ConversationHistory()
        history = self._histories[conv_key]

        history.add("user", f"[{sender}]: {message}")

        if self._memory:
            try:
                lead_id = self._memory.resolve_lead(conv_key)
                self._memory.add_message(lead_id, "user", f"[{sender}]: {message}",
                                         platform=platform)
            except Exception as e:
                log.debug("Memory persist failed: %s", e)

        p = self._personas.get(persona, DEFAULT_PERSONAS.get("casual", Persona()))
        knowledge = p.knowledge
        if extra_context:
            knowledge = f"{knowledge}\n\nCurrent conversation stage guidance: {extra_context}"

        p_copy = Persona(
            name=p.name, description=p.description, language=p.language,
            tone=p.tone, response_style=p.response_style,
            knowledge=knowledge, platform=platform,
        )

        base_prompt = p_copy.to_system_prompt()
        if fsm_state:
            try:
                engine = get_strategy_engine()
                base_prompt = engine.build_system_prompt(
                    fsm_state, platform=platform,
                    persona_prompt=base_prompt,
                    lead_context=extra_context or "")
            except Exception as e:
                log.debug("Strategy prompt failed: %s", e)

        messages = [{"role": "system", "content": base_prompt}]
        if self._memory:
            try:
                lead_id = self._memory.resolve_lead(conv_key)
                ctx = self._memory.get_context(lead_id, limit=15, platform=platform)
                messages.extend(ctx)
            except Exception:
                messages.extend(history.to_messages(limit=15))
        else:
            messages.extend(history.to_messages(limit=15))

        # Generate
        reply_text = self._client.chat_messages(messages, temperature=0.8, max_tokens=256)

        if not reply_text:
            return None

        reply_text = self._clean_reply(reply_text, p_copy.name)

        try:
            filt = get_reply_filter()
            result = filt.filter(reply_text, platform=platform)
            if not result.passed:
                log.warning("Reply blocked by filter: %s | violations=%s",
                            reply_text[:60], result.violations)
                return None
            if result.auto_fixed:
                log.info("Reply auto-fixed: %s → %s",
                         reply_text[:40], result.filtered[:40])
                reply_text = result.filtered
        except Exception as e:
            log.debug("Filter error (proceeding anyway): %s", e)

        history.add("assistant", reply_text)

        if self._memory:
            try:
                lead_id = self._memory.resolve_lead(conv_key)
                self._memory.add_message(lead_id, "assistant", reply_text,
                                         platform=platform)
            except Exception:
                pass

        delay = self._calculate_delay(message, reply_text, intent)

        return ReplyResult(
            text=reply_text,
            intent=intent,
            delay_sec=delay,
            persona=persona,
        )

    def add_persona(self, name: str, persona: Persona):
        self._personas[name] = persona

    def get_history(self, conversation_id: str) -> Optional[ConversationHistory]:
        return self._histories.get(conversation_id)

    def clear_history(self, conversation_id: str):
        if conversation_id in self._histories:
            self._histories[conversation_id].clear()

    # -- Internal -----------------------------------------------------------

    @staticmethod
    def _clean_reply(text: str, persona_name: str) -> str:
        """Remove common LLM artifacts from reply."""
        text = text.strip()
        for prefix in [f"{persona_name}:", f"[{persona_name}]:", "Assistant:", "AI:"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text

    @staticmethod
    def _calculate_delay(incoming: str, reply: str, intent: str) -> float:
        """Simulate human reading + thinking + typing time."""
        read_time = len(incoming) / 250 * 60  # ~250 chars/min reading
        think_time = random.gauss(2.0, 0.8)
        type_time = len(reply) / 300 * 60  # ~300 chars/min typing

        base = read_time + max(0.5, think_time) + type_time

        if intent == Intent.NEEDS_REPLY:
            base *= random.uniform(0.8, 1.2)
        else:
            base *= random.uniform(1.5, 3.0)

        return max(2.0, min(base, 30.0))


@dataclass
class ReplyResult:
    text: str
    intent: str
    delay_sec: float
    persona: str
