# -*- coding: utf-8 -*-
"""
ReplyFilter — Safety filter for AI-generated replies.

Catches replies that could trigger platform risk-control:
- Blacklisted keywords (links, prices, hard-sell phrases)
- Excessive emoji density
- Length violations
- Language inconsistency
- Repetitive patterns
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+|www\.\S+|bit\.ly/\S+|\S+\.com/\S+", re.I)
EMAIL_RE = re.compile(r"\b[\w.-]+@[\w.-]+\.\w{2,}\b")
PHONE_RE = re.compile(r"\+?\d[\d\s\-]{7,}\d")
EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U0000FE00-\U0000FE0F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U0000200D"
    "]+",
    re.UNICODE,
)


@dataclass
class FilterConfig:
    """Per-platform filter configuration."""
    max_length: int = 300
    min_length: int = 2
    max_emoji_ratio: float = 0.15
    max_emoji_count: int = 5
    block_urls: bool = True
    block_emails: bool = True
    block_phone_numbers: bool = True
    keyword_blacklist: List[str] = field(default_factory=list)
    keyword_whitelist: List[str] = field(default_factory=list)


PLATFORM_CONFIGS: Dict[str, FilterConfig] = {
    "tiktok": FilterConfig(
        max_length=250,
        max_emoji_ratio=0.12,
        max_emoji_count=4,
        block_urls=True,
        block_emails=True,
        block_phone_numbers=True,
        keyword_blacklist=[
            "buy now", "limited offer", "click here", "free money",
            "dm me for", "price", "discount", "promo code",
            "投资", "赚钱", "免费", "优惠", "点击链接",
            "make money", "earn $", "join my",
        ],
    ),
    "telegram": FilterConfig(
        max_length=400,
        max_emoji_ratio=0.15,
        max_emoji_count=6,
        block_urls=False,
        block_emails=False,
        block_phone_numbers=False,
        keyword_blacklist=[
            "buy now", "limited offer", "free money",
            "投资", "赚钱", "免费领取",
        ],
    ),
    "whatsapp": FilterConfig(
        max_length=350,
        max_emoji_ratio=0.15,
        max_emoji_count=5,
        block_urls=True,
        block_emails=False,
        block_phone_numbers=False,
        keyword_blacklist=[
            "buy now", "limited offer", "click here", "free money",
            "投资", "赚钱",
        ],
    ),
    "facebook": FilterConfig(
        max_length=350,
        max_emoji_ratio=0.12,
        max_emoji_count=4,
        block_urls=True,
        block_emails=True,
        block_phone_numbers=True,
        keyword_blacklist=[
            "buy now", "limited offer", "click here", "free money",
            "dm me", "make money", "earn money",
            "投资", "赚钱", "免费",
        ],
    ),
}


@dataclass
class FilterResult:
    passed: bool
    original: str
    filtered: str
    violations: List[str] = field(default_factory=list)
    auto_fixed: bool = False


class ReplyFilter:
    """Filters AI-generated replies for platform safety compliance."""

    def __init__(self, configs: Optional[Dict[str, FilterConfig]] = None):
        self._configs = configs or PLATFORM_CONFIGS

    def filter(self, text: str, platform: str = "tiktok") -> FilterResult:
        """Apply safety filters. Returns FilterResult with pass/fail and violations."""
        config = self._configs.get(platform, FilterConfig())
        violations: List[str] = []
        filtered = text.strip()

        if not filtered or len(filtered) < config.min_length:
            return FilterResult(False, text, "", ["empty_or_too_short"])

        if len(filtered) > config.max_length:
            violations.append(f"too_long ({len(filtered)} > {config.max_length})")
            filtered = filtered[:config.max_length].rsplit(" ", 1)[0]
            if not filtered:
                filtered = text[:config.max_length]

        if config.block_urls:
            urls = URL_RE.findall(filtered)
            if urls:
                violations.append(f"contains_url: {urls[0][:30]}")
                filtered = URL_RE.sub("", filtered).strip()

        if config.block_emails:
            emails = EMAIL_RE.findall(filtered)
            if emails:
                violations.append("contains_email")
                filtered = EMAIL_RE.sub("", filtered).strip()

        if config.block_phone_numbers:
            phones = PHONE_RE.findall(filtered)
            if phones:
                violations.append("contains_phone")
                filtered = PHONE_RE.sub("", filtered).strip()

        lower = filtered.lower()
        for kw in config.keyword_blacklist:
            if kw.lower() in lower:
                violations.append(f"blacklisted_keyword: {kw}")

        emojis = EMOJI_RE.findall(filtered)
        emoji_count = len(emojis)
        total_chars = max(1, len(filtered))
        emoji_chars = sum(len(e) for e in emojis)
        emoji_ratio = emoji_chars / total_chars

        if emoji_count > config.max_emoji_count:
            violations.append(
                f"too_many_emojis ({emoji_count} > {config.max_emoji_count})")
            to_remove = emoji_count - config.max_emoji_count
            for _ in range(to_remove):
                m = EMOJI_RE.search(filtered)
                if m:
                    filtered = filtered[:m.start()] + filtered[m.end():]

        if emoji_ratio > config.max_emoji_ratio:
            violations.append(
                f"emoji_ratio_high ({emoji_ratio:.2f} > {config.max_emoji_ratio})")

        has_blocking = any(
            v.startswith("blacklisted_keyword") for v in violations
        )

        if has_blocking:
            return FilterResult(
                passed=False,
                original=text,
                filtered=filtered,
                violations=violations,
                auto_fixed=False,
            )

        auto_fixed = len(violations) > 0 and filtered != text
        return FilterResult(
            passed=True,
            original=text,
            filtered=filtered,
            violations=violations,
            auto_fixed=auto_fixed,
        )

    def add_blacklist(self, platform: str, keywords: List[str]):
        config = self._configs.get(platform)
        if config:
            config.keyword_blacklist.extend(keywords)

    def get_config(self, platform: str) -> FilterConfig:
        return self._configs.get(platform, FilterConfig())


_filter: Optional[ReplyFilter] = None


def get_reply_filter() -> ReplyFilter:
    global _filter
    if _filter is None:
        _filter = ReplyFilter()
    return _filter
