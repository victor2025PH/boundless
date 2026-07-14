"""
Instagram Automation — hybrid module with GramAddict-inspired anti-detection.

Architecture:
  - Inherits BaseAutomation for device/compliance/behavior
  - Uses AutoSelector for self-learning UI interaction
  - Applies GramAddict-validated timing parameters for anti-detection
  - Supports: follow, like, comment, DM, hashtag browse, feed browse
  - Integrated Leads collection

Anti-detection strategy (from GramAddict research):
  - Variable action intervals with occasional long pauses
  - Natural scroll patterns (different speeds, occasional scrolls up)
  - Double-tap for likes (more human than button tap)
  - Session limits with forced rest periods
  - Never exceed 60 follows/day, 100 likes/day, 20 DMs/day
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional

from .base_automation import BaseAutomation

log = logging.getLogger(__name__)

PACKAGE = "com.instagram.android"

_IG_DISMISS_TEXTS = [
    "Not Now", "Not now", "NOT NOW",
    "Skip", "SKIP",
    "Cancel", "CANCEL",
    "Maybe Later", "Later",
    "No Thanks",
    "Close", "CLOSE",
    "OK", "Got It",
    "Allow", "Don't Allow",
    "Turn On", "Not Now",
]


class InstagramAutomation(BaseAutomation):

    PLATFORM = "instagram"
    PACKAGE = PACKAGE
    MAIN_ACTIVITY = ""

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
            for text in _IG_DISMISS_TEXTS:
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
        """Navigate to home feed tab."""
        did = self._did(device_id)
        d = self._u2(did)
        for sel in [
            {"description": "Home"},
            {"resourceId": "com.instagram.android:id/tab_icon", "instance": 0},
        ]:
            el = d(**sel)
            if el.exists(timeout=2):
                self.hb.tap(d, *self._el_center(el))
                time.sleep(1)
                return True
        d.press("back")
        d.press("back")
        time.sleep(0.5)
        return False

    def go_search(self, device_id: Optional[str] = None):
        """Navigate to search/explore tab."""
        did = self._did(device_id)
        d = self._u2(did)
        for sel in [
            {"description": "Search and explore"},
            {"description": "Search and Explore"},
            {"resourceId": "com.instagram.android:id/tab_icon", "instance": 1},
        ]:
            el = d(**sel)
            if el.exists(timeout=2):
                self.hb.tap(d, *self._el_center(el))
                time.sleep(1)
                return True
        return self.smart_tap("Search tab icon", device_id=did)

    # ── Core Actions ──────────────────────────────────────────────────────

    def search_users(self, query: str, device_id: Optional[str] = None,
                     max_results: int = 10) -> List[Dict[str, str]]:
        """Search for users by keyword."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("search", device_id=did):
            self.go_search(did)
            time.sleep(0.5)

            if not self.smart_tap("Search input field", device_id=did):
                d.click(540, 120)
                time.sleep(0.5)

            self.hb.type_text(d, query)
            time.sleep(1.5)

            self.smart_tap("Accounts tab or People filter", device_id=did)
            time.sleep(1)

        return self._extract_user_results(d, max_results)

    def follow_user(self, device_id: Optional[str] = None) -> bool:
        """Follow the user on the current profile screen."""
        did = self._did(device_id)
        with self.guarded("follow", device_id=did):
            return self.smart_tap("Follow button", device_id=did)

    def like_post_double_tap(self, device_id: Optional[str] = None) -> bool:
        """
        Like a post using double-tap (more human than button tap).
        GramAddict research: double-tap is 40% less likely to trigger detection.
        """
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("like", device_id=did):
            tp = self.hb.profile.tap
            cx = random.randint(200, 880)
            cy = random.randint(600, 1200)
            gap = random.randint(*tp.double_tap_gap_ms) / 1000.0

            d.click(cx, cy)
            time.sleep(gap)
            d.click(cx + random.randint(-3, 3), cy + random.randint(-3, 3))
            self.hb.wait_between_actions(0.5)
            return True

    def like_post_button(self, device_id: Optional[str] = None) -> bool:
        """Like via the heart button."""
        did = self._did(device_id)
        with self.guarded("like", device_id=did):
            return self.smart_tap("Like button (heart icon)", device_id=did)

    def comment_post(self, comment: str,
                     device_id: Optional[str] = None) -> bool:
        """Comment on the current post."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("comment", device_id=did):
            rewritten = self.rewrite_message(comment, {"platform": "instagram"})

            if not self.smart_tap("Comment icon", device_id=did):
                return False

            time.sleep(1)
            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.3)
            return self.smart_tap("Post comment button", device_id=did)

    def send_dm(self, recipient: str, message: str,
                device_id: Optional[str] = None) -> bool:
        """Send a direct message."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("send_dm", device_id=did):
            rewritten = self.rewrite_message(message, {"platform": "instagram", "recipient": recipient})

            if not self.smart_tap("Direct messages icon", device_id=did):
                return False
            time.sleep(1)

            if not self.smart_tap("New message or compose icon", device_id=did):
                return False
            time.sleep(0.8)

            self.hb.type_text(d, recipient)
            time.sleep(1.5)

            if not self.smart_tap("First matching user", device_id=did):
                log.warning("DM recipient not found: %s", recipient)
                return False

            self.smart_tap("Next or Chat button", device_id=did)
            time.sleep(1)

            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.5)
            return self.smart_tap("Send button", device_id=did)

    def browse_feed(self, scroll_count: int = 8,
                    like_probability: float = 0.2,
                    use_double_tap: bool = True,
                    device_id: Optional[str] = None) -> Dict[str, int]:
        """
        Browse home feed with natural behavior.
        GramAddict pattern: variable scroll speed, occasional reverse scrolls,
        random pauses to "read" content.
        """
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"scrolls": 0, "likes": 0, "reverse_scrolls": 0}

        self.go_home(did)
        time.sleep(1)

        for i in range(scroll_count):
            with self.guarded("browse_feed", device_id=did, weight=0.3):
                # occasional reverse scroll (more human)
                if random.random() < 0.1 and i > 0:
                    self.hb.scroll_up(d)
                    stats["reverse_scrolls"] += 1
                    self.hb.wait_read(random.randint(100, 300))

                self.hb.scroll_down(d)
                stats["scrolls"] += 1

                # simulate reading
                read_time = random.uniform(1.5, 6.0)
                time.sleep(read_time)

                # random like
                if random.random() < like_probability:
                    if use_double_tap:
                        self.like_post_double_tap(did)
                    else:
                        self.like_post_button(did)
                    stats["likes"] += 1

                # occasional long pause (checking something else)
                if random.random() < 0.05:
                    time.sleep(random.uniform(5, 15))

        return stats

    def browse_hashtag(self, hashtag: str, scroll_count: int = 5,
                       like_probability: float = 0.25,
                       device_id: Optional[str] = None) -> Dict[str, int]:
        """Browse posts for a hashtag."""
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"scrolls": 0, "likes": 0}

        with self.guarded("browse_hashtag", device_id=did):
            self.go_search(did)
            time.sleep(0.5)

            tag = hashtag if hashtag.startswith("#") else f"#{hashtag}"
            self.hb.type_text(d, tag)
            time.sleep(1.5)

            self.smart_tap("Tags tab", device_id=did)
            time.sleep(0.8)
            self.smart_tap("First matching hashtag", device_id=did)
            time.sleep(2)

        for _ in range(scroll_count):
            self.hb.scroll_down(d)
            time.sleep(random.uniform(2, 5))
            stats["scrolls"] += 1

            if random.random() < like_probability:
                self.like_post_double_tap(did)
                stats["likes"] += 1

        return stats

    # ── Leads Integration ─────────────────────────────────────────────────

    def search_and_collect_leads(self, query: str,
                                 device_id: Optional[str] = None,
                                 max_leads: int = 10) -> List[int]:
        """Search users and store in Leads Pool."""
        from ..leads.store import get_leads_store
        store = get_leads_store()
        users = self.search_users(query, device_id, max_leads)
        lead_ids = []

        for u in users:
            lead_id = store.add_lead(
                name=u.get("name", u.get("username", "Unknown")),
                source_platform="instagram",
                tags=[query],
            )
            if u.get("username"):
                store.add_platform_profile(
                    lead_id, "instagram",
                    username=u["username"],
                    profile_url=f"https://instagram.com/{u['username']}",
                )
            lead_ids.append(lead_id)

        log.info("Collected %d leads from IG search '%s'", len(lead_ids), query)
        return lead_ids

    # ── Internal ──────────────────────────────────────────────────────────

    def _extract_user_results(self, d, max_results: int) -> List[Dict[str, str]]:
        results = []
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
            for el in elements:
                if el.text and len(el.text) > 1 and el.clickable:
                    if not el.text.startswith("#") and "Tab" not in el.text:
                        results.append({
                            "name": el.text,
                            "username": el.text if el.text.isascii() else "",
                        })
                        if len(results) >= max_results:
                            break
        except Exception as e:
            log.warning("Failed to extract IG results: %s", e)
        return results

    @staticmethod
    def _el_center(el) -> tuple:
        info = el.info
        b = info.get("bounds", {})
        return (
            (b.get("left", 0) + b.get("right", 0)) // 2,
            (b.get("top", 0) + b.get("bottom", 0)) // 2,
        )
