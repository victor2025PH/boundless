"""
MessageRewriter — LLM-powered message uniquification.

LinkedIn detects 34% of automation via repeated message patterns.
This module ensures every outgoing message is unique by:
1. Taking a template + recipient context
2. Generating N unique variants via LLM
3. Caching variants to avoid redundant API calls
4. Providing offline fallback (simple template substitution)

Optimization: Pre-generation mode creates a pool of variants for popular
templates. Runtime picks from pool instead of calling LLM per-message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .llm_client import LLMClient, get_llm_client

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a message rewriting assistant. Your job is to rewrite messages to sound natural and unique while preserving the original meaning.

Rules:
- Keep the same language as the input (if Chinese, reply in Chinese; if English, reply in English)
- Maintain the same tone and intent
- Make each variant sound like a different person wrote it
- Keep length similar to original (±20%)
- Do NOT add greetings/signatures unless the original has them
- Output ONLY the rewritten message, nothing else"""

SYSTEM_PROMPT_LANG = """You are a multilingual message writer. Your job is to rewrite messages in {language} that sound completely native and natural.

Rules:
- Write the output ENTIRELY in {language}
- Sound like a native {language} speaker — use natural expressions, not literal translation
- Maintain the same tone, intent, and meaning as the input
- Keep length similar to original (±20%)
- Do NOT add greetings/signatures unless the original has them
- Output ONLY the rewritten message in {language}, nothing else"""

BATCH_SYSTEM = """You are a message variant generator. Given a template message, generate exactly {count} unique variants.

Rules:
- Same language as input
- Each variant must be meaningfully different (not just synonym swaps)
- Preserve intent and tone
- Similar length to original
- Output as numbered list: 1. ... 2. ... 3. ..."""

BATCH_SYSTEM_LANG = """You are a multilingual message variant generator. Given a template, generate exactly {count} unique variants in {language}.

Rules:
- Write ALL variants ENTIRELY in {language}
- Sound like a native {language} speaker — natural expressions, not translation
- Each variant must be meaningfully different (not just synonym swaps)
- Preserve intent and tone
- Similar length to original
- Output as numbered list: 1. ... 2. ... 3. ..."""


@dataclass
class RewriterConfig:
    default_variants: int = 5
    max_cached_variants: int = 50
    offline_mode: bool = False
    platform_tone: Dict[str, str] = field(default_factory=lambda: {
        "telegram": "casual and friendly",
        "linkedin": "professional but warm",
        "whatsapp": "informal and conversational",
        "tiktok": "young, trendy, casual and engaging, use emojis sparingly",
    })


class MessageRewriter:
    """
    Rewrites messages for uniqueness.

    Usage:
        rw = MessageRewriter()
        unique_msg = rw.rewrite("Hi {name}, let's connect!", context={"name": "Alice"}, platform="linkedin")

        # Pre-generate variants for a template
        rw.pregenerate("Hi {name}, I'd love to connect about {field}!", count=10, platform="linkedin")
    """

    def __init__(self, client: Optional[LLMClient] = None,
                 config: Optional[RewriterConfig] = None):
        self._client = client or get_llm_client()
        self.config = config or RewriterConfig()
        self._variant_pool: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    # -- Main API -----------------------------------------------------------

    def rewrite(self, template: str, context: Optional[Dict[str, str]] = None,
                platform: str = "telegram",
                target_language: str = "") -> str:
        """
        Rewrite a message template into a unique variant.

        1. Check pre-generated pool
        2. If pool empty, call LLM
        3. If LLM unavailable, do offline substitution
        4. Apply context variables

        target_language: if set, rewrites message natively in the target language
                         (e.g. "italian", "german", "french")
        """
        filled = self._fill_template(template, context)
        pool_key = self._pool_key(template, platform)

        with self._lock:
            if pool_key in self._variant_pool and self._variant_pool[pool_key]:
                variant_template = self._variant_pool[pool_key].pop(
                    random.randint(0, len(self._variant_pool[pool_key]) - 1)
                )
                result = self._fill_template(variant_template, context)
                log.debug("Rewriter: used pooled variant (%d remaining)", len(self._variant_pool[pool_key]))
                return result

        if self.config.offline_mode:
            return self._offline_rewrite(filled)

        rewritten = self._llm_rewrite(filled, platform, target_language)
        return rewritten if rewritten else filled

    def rewrite_batch(self, template: str, contexts: List[Dict[str, str]],
                      platform: str = "telegram") -> List[str]:
        """Rewrite a template for multiple recipients."""
        results = []
        for ctx in contexts:
            results.append(self.rewrite(template, ctx, platform))
        return results

    def pregenerate(self, template: str, count: int = 0,
                    platform: str = "telegram",
                    target_language: str = "") -> int:
        """
        Pre-generate variant pool for a template.
        Returns number of variants generated.
        """
        count = count or self.config.default_variants
        pool_key = self._pool_key(template, platform)

        if self.config.offline_mode:
            variants = [self._offline_rewrite(template) for _ in range(count)]
        else:
            variants = self._llm_batch_generate(template, count, platform,
                                                target_language)

        with self._lock:
            existing = self._variant_pool.get(pool_key, [])
            existing.extend(variants)
            if len(existing) > self.config.max_cached_variants:
                existing = existing[-self.config.max_cached_variants:]
            self._variant_pool[pool_key] = existing

        log.info("Pre-generated %d variants for template (pool size: %d)",
                 len(variants), len(self._variant_pool[pool_key]))
        return len(variants)

    def pool_status(self) -> Dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._variant_pool.items()}

    # -- LLM calls ----------------------------------------------------------

    def _llm_rewrite(self, message: str, platform: str,
                     target_language: str = "") -> str:
        tone = self.config.platform_tone.get(platform, "natural")

        if target_language:
            system = SYSTEM_PROMPT_LANG.format(language=target_language)
            prompt = (f"Rewrite this message natively in {target_language}, "
                      f"with a {tone} tone:\n\n{message}")
        else:
            system = SYSTEM_PROMPT
            prompt = f"Rewrite this message in a {tone} tone:\n\n{message}"

        return self._client.chat_with_system(system, prompt, temperature=0.9)

    def _llm_batch_generate(self, template: str, count: int, platform: str,
                            target_language: str = "") -> List[str]:
        tone = self.config.platform_tone.get(platform, "natural")

        if target_language:
            system = BATCH_SYSTEM_LANG.format(count=count, language=target_language)
            prompt = (f"Generate {count} unique variants of this message "
                      f"natively in {target_language}, with a {tone} tone:\n\n{template}")
        else:
            system = BATCH_SYSTEM.format(count=count)
            prompt = f"Generate {count} unique variants of this message in a {tone} tone:\n\n{template}"

        response = self._client.chat_with_system(system, prompt, temperature=0.9, max_tokens=1024)
        if not response:
            return []

        return self._parse_numbered_list(response, count)

    @staticmethod
    def _parse_numbered_list(text: str, expected: int) -> List[str]:
        """Parse '1. ...\n2. ...' format into list of strings."""
        lines = text.strip().split("\n")
        variants = []
        current = ""
        for line in lines:
            m = re.match(r'^\d+[\.\)]\s*(.+)', line.strip())
            if m:
                if current:
                    variants.append(current.strip())
                current = m.group(1)
            elif current and line.strip():
                current += " " + line.strip()
        if current:
            variants.append(current.strip())
        return [v for v in variants if len(v) > 5]

    # -- Offline fallback ---------------------------------------------------

    @staticmethod
    def _offline_rewrite(message: str) -> str:
        """Simple rule-based rewriting when LLM is unavailable."""
        substitutions = [
            ("Hi ", random.choice(["Hey ", "Hello ", "Hi there, "])),
            ("I'd love to", random.choice(["I'd be happy to", "I'd enjoy", "I'm keen to"])),
            ("connect", random.choice(["connect", "link up", "get in touch"])),
            ("noticed", random.choice(["noticed", "saw", "came across"])),
            ("interesting", random.choice(["interesting", "impressive", "great"])),
            ("work", random.choice(["work", "background", "experience"])),
            ("Let's", random.choice(["Let's", "Shall we", "How about we"])),
            ("你好", random.choice(["你好", "嗨", "Hi"])),
            ("我想", random.choice(["我想", "我希望能", "我很想"])),
            ("联系", random.choice(["联系", "交流", "沟通"])),
        ]
        result = message
        applied = 0
        for old, new in substitutions:
            if old in result and applied < 3 and random.random() > 0.4:
                result = result.replace(old, new, 1)
                applied += 1
        return result

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _fill_template(template: str, context: Optional[Dict[str, str]]) -> str:
        if not context:
            return template
        result = template
        for key, value in context.items():
            result = result.replace(f"{{{key}}}", value)
        return result

    @staticmethod
    def _pool_key(template: str, platform: str) -> str:
        raw = f"{platform}:{template}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_rewriter: Optional[MessageRewriter] = None
_rw_lock = threading.Lock()


def get_rewriter(config: Optional[RewriterConfig] = None) -> MessageRewriter:
    global _rewriter
    if _rewriter is None:
        with _rw_lock:
            if _rewriter is None:
                _rewriter = MessageRewriter(config=config)
    return _rewriter
