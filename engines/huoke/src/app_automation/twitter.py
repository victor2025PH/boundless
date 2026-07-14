"""
X/Twitter Automation — conversation-driven engagement.

Twitter acquisition strategy:
  - Search by keywords/hashtags to find relevant conversations
  - Reply with valuable, AI-generated comments (not spam)
  - Follow relevant accounts
  - DM for deeper engagement
  - Retweet to build presence

Anti-detection:
  - Variable intervals between actions
  - Natural scrolling with pauses
  - Never exceed 50 follows/day or 100 likes/day
  - Genuine replies that add value to conversations
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional

from .base_automation import BaseAutomation

log = logging.getLogger(__name__)

PACKAGE = "com.twitter.android"

_X_DISMISS_TEXTS = [
    "Not now", "Not Now", "NOT NOW",
    "Skip", "SKIP", "Skip for now",
    "Maybe later", "Later",
    "No thanks", "No Thanks",
    "Close", "CLOSE",
    "OK", "Got it", "GOT IT",
    "Allow", "Don't allow",
    "Turn on", "Not now",
    "Continue", "CONTINUE",
    "I accept",
]


class TwitterAutomation(BaseAutomation):

    PLATFORM = "twitter"
    PACKAGE = PACKAGE

    def __init__(self, device_manager=None, **kwargs):
        if device_manager is None:
            from ..device_control.device_manager import get_device_manager
            device_manager = get_device_manager()
        super().__init__(device_manager, **kwargs)

    # ── Startup ────────────────────────────────────────────────────────────

    def launch(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        d = self._u2(did)
        d.app_stop(PACKAGE)
        time.sleep(1)
        d.app_start(PACKAGE)
        time.sleep(4)
        self._dismiss_dialogs(d)
        return self.is_foreground(did)

    def _dismiss_dialogs(self, d, max_attempts: int = 6):
        for _ in range(max_attempts):
            dismissed = False
            for text in _X_DISMISS_TEXTS:
                btn = d(text=text)
                if btn.exists(timeout=0.3):
                    self.hb.tap(d, *self._el_center(btn))
                    time.sleep(0.6)
                    dismissed = True
                    break
            if not dismissed:
                break

    # ── Navigation ─────────────────────────────────────────────────────────

    def go_home(self, device_id: Optional[str] = None):
        did = self._did(device_id)
        d = self._u2(did)
        for desc in ["Home", "Home Tab", "首页"]:
            el = d(description=desc)
            if el.exists(timeout=2):
                el.click()
                time.sleep(1)
                return True
        d.press("back")
        d.press("back")
        return False

    def go_explore(self, device_id: Optional[str] = None):
        did = self._did(device_id)
        d = self._u2(did)
        for desc in ["Search and Explore", "Explore", "Search", "搜索"]:
            el = d(description=desc)
            if el.exists(timeout=2):
                el.click()
                time.sleep(1)
                return True
        return self.smart_tap("Search tab or Explore icon", device_id=did)

    # ── Core Actions ──────────────────────────────────────────────────────

    def search_users(self, query: str, device_id: Optional[str] = None,
                     max_results: int = 10) -> List[Dict[str, str]]:
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("search", device_id=did):
            self.go_explore(did)
            time.sleep(0.5)

            if not self.smart_tap("Search input field", device_id=did):
                d.click(540, 120)
                time.sleep(0.5)

            self.hb.type_text(d, query)
            d.press("enter")
            time.sleep(2)

            self.smart_tap("People tab", device_id=did)
            time.sleep(1)

        return self._extract_user_results(d, max_results)

    def follow_user(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        with self.guarded("follow", device_id=did):
            return self.smart_tap("Follow button", device_id=did)

    def like_tweet(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        with self.guarded("like", device_id=did):
            return self.smart_tap("Like or heart icon", device_id=did)

    def retweet(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        with self.guarded("retweet", device_id=did):
            if self.smart_tap("Retweet or repost icon", device_id=did):
                time.sleep(0.5)
                # confirm dialog
                for text in ["Repost", "Retweet", "转推"]:
                    el = self._u2(did)(text=text)
                    if el.exists(timeout=2):
                        self.hb.tap(self._u2(did), *self._el_center(el))
                        return True
                return True
        return False

    def reply_tweet(self, reply_text: str,
                    device_id: Optional[str] = None) -> bool:
        """Reply to a tweet with AI-generated valuable content."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("reply", device_id=did):
            rewritten = self.rewrite_message(reply_text, {"platform": "twitter", "type": "reply"})

            if not self.smart_tap("Reply icon (speech bubble)", device_id=did):
                return False
            time.sleep(1)

            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.5)
            return self.smart_tap("Post or Reply button", device_id=did)

    def send_dm(self, recipient: str, message: str,
                device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("send_dm", device_id=did):
            rewritten = self.rewrite_message(message, {"platform": "twitter", "recipient": recipient})

            if not self.smart_tap("Messages or DM icon", device_id=did):
                return False
            time.sleep(1)

            if not self.smart_tap("New message or compose icon", device_id=did):
                return False
            time.sleep(0.8)

            self.hb.type_text(d, recipient)
            time.sleep(1.5)

            if not self.smart_tap("First matching user", device_id=did):
                log.warning("X DM recipient not found: %s", recipient)
                return False

            self.smart_tap("Next button", device_id=did)
            time.sleep(1)

            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.3)
            return self.smart_tap("Send message button", device_id=did)

    def browse_timeline(self, scroll_count: int = 8,
                        like_probability: float = 0.2,
                        retweet_probability: float = 0.05,
                        device_id: Optional[str] = None) -> Dict[str, int]:
        """Browse home timeline with natural engagement."""
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"scrolls": 0, "likes": 0, "retweets": 0}

        self.go_home(did)
        time.sleep(1)

        for i in range(scroll_count):
            with self.guarded("browse_feed", device_id=did, weight=0.3):
                self.hb.scroll_down(d)
                stats["scrolls"] += 1

                read_time = random.uniform(1.0, 5.0)
                time.sleep(read_time)

                if random.random() < like_probability:
                    if self.like_tweet(did):
                        stats["likes"] += 1

                if random.random() < retweet_probability:
                    if self.retweet(did):
                        stats["retweets"] += 1

                # occasional reverse scroll
                if random.random() < 0.08:
                    self.hb.scroll_up(d)
                    time.sleep(random.uniform(1, 3))

        return stats

    def search_and_engage(self, keyword: str, max_tweets: int = 5,
                          reply_probability: float = 0.3,
                          like_probability: float = 0.5,
                          device_id: Optional[str] = None) -> Dict[str, int]:
        """
        Search for tweets by keyword and engage with them.
        This is the core outreach strategy on Twitter:
        find relevant conversations → add value → get noticed.
        """
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"tweets_seen": 0, "likes": 0, "replies": 0}

        with self.guarded("search", device_id=did):
            self.go_explore(did)
            time.sleep(0.5)
            self.hb.type_text(d, keyword)
            d.press("enter")
            time.sleep(2)

        for _ in range(max_tweets):
            stats["tweets_seen"] += 1
            time.sleep(random.uniform(2, 5))

            if random.random() < like_probability:
                if self.like_tweet(did):
                    stats["likes"] += 1

            if random.random() < reply_probability:
                reply = self._generate_reply(keyword)
                if reply and self.reply_tweet(reply, did):
                    stats["replies"] += 1

            self.hb.scroll_down(d)
            time.sleep(random.uniform(0.5, 1.5))

        return stats

    # ── Leads Integration ─────────────────────────────────────────────────

    def search_and_collect_leads(self, query: str,
                                 device_id: Optional[str] = None,
                                 max_leads: int = 10) -> List[int]:
        from ..leads.store import get_leads_store
        store = get_leads_store()
        users = self.search_users(query, device_id, max_leads)
        lead_ids = []
        for u in users:
            lead_id = store.add_lead(
                name=u.get("name", u.get("username", "Unknown")),
                source_platform="twitter",
                tags=[query],
            )
            if u.get("username"):
                store.add_platform_profile(
                    lead_id, "twitter",
                    username=u["username"],
                    profile_url=f"https://x.com/{u['username'].lstrip('@')}",
                )
            lead_ids.append(lead_id)
        log.info("Collected %d leads from X search '%s'", len(lead_ids), query)
        return lead_ids

    # ── Internal ──────────────────────────────────────────────────────────

    def _generate_reply(self, context_keyword: str) -> Optional[str]:
        """Generate a valuable reply using AI or templates."""
        if self.rewriter:
            try:
                templates = [
                    f"Great point! I've been thinking about {context_keyword} as well and found that...",
                    f"This is a really insightful take on {context_keyword}. Have you considered...",
                    f"Interesting perspective! In my experience with {context_keyword}...",
                ]
                base = random.choice(templates)
                return self.rewrite_message(base, {"platform": "twitter", "type": "reply"})
            except Exception:
                pass
        return None

    def _extract_user_results(self, d, max_results: int) -> List[Dict[str, str]]:
        results = []
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
            for el in elements:
                if el.text and el.text.startswith("@") and el.clickable:
                    results.append({
                        "name": "",
                        "username": el.text,
                    })
                elif el.text and len(el.text) > 2 and el.clickable:
                    if not any(skip in el.text for skip in ["Tab", "Search", "Explore"]):
                        results.append({
                            "name": el.text,
                            "username": "",
                        })
                if len(results) >= max_results:
                    break
        except Exception as e:
            log.warning("Failed to extract X results: %s", e)
        return results

    @staticmethod
    def _el_center(el) -> tuple:
        info = el.info
        b = info.get("bounds", {})
        return (
            (b.get("left", 0) + b.get("right", 0)) // 2,
            (b.get("top", 0) + b.get("bottom", 0)) // 2,
        )
