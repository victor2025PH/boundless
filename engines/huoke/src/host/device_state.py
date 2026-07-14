# -*- coding: utf-8 -*-
"""
DeviceStateStore — SQLite-backed per-device automation state.

Replaces tiktok_state.json + stats.json with a single transactional store.
Lives in openclaw.db alongside tasks and schedules tables.

State keys per (device_id, platform):
  phase, can_follow, start_date, follow_unlocked_date,
  follow_test_failures, follow_tested_days, active_days_following,
  total_watched, total_liked, total_followed, total_dms_sent,
  total_comments, daily:{date}:{metric}
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import get_conn

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeviceStateStore:
    """Thread-safe per-device state backed by openclaw.db device_states table."""

    def __init__(self, platform: str = "tiktok"):
        self._platform = platform

    def get(self, device_id: str, key: str, default: str = "") -> str:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM device_states "
                "WHERE device_id = ? AND platform = ? AND key = ?",
                (device_id, self._platform, key),
            ).fetchone()
        return row[0] if row else default

    def get_int(self, device_id: str, key: str, default: int = 0) -> int:
        val = self.get(device_id, key)
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    def get_float(self, device_id: str, key: str, default: float = 0.0) -> float:
        val = self.get(device_id, key)
        if not val:
            return default
        try:
            return float(val)
        except ValueError:
            return default

    def get_bool(self, device_id: str, key: str, default: bool = False) -> bool:
        val = self.get(device_id, key)
        if not val:
            return default
        return val.lower() in ("1", "true", "yes")

    def get_json(self, device_id: str, key: str, default: Any = None) -> Any:
        val = self.get(device_id, key)
        if not val:
            return default if default is not None else {}
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else {}

    def set(self, device_id: str, key: str, value: Any):
        if isinstance(value, bool):
            str_val = "1" if value else "0"
        elif isinstance(value, (dict, list)):
            str_val = json.dumps(value, ensure_ascii=False)
        else:
            str_val = str(value)
        now = _now_iso()
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO device_states "
                "(device_id, platform, key, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (device_id, self._platform, key, str_val, now),
            )

    def increment(self, device_id: str, key: str, amount: int = 1) -> int:
        current = self.get_int(device_id, key, 0)
        new_val = current + amount
        self.set(device_id, key, new_val)
        return new_val

    def get_all(self, device_id: str) -> Dict[str, str]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM device_states "
                "WHERE device_id = ? AND platform = ?",
                (device_id, self._platform),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def delete(self, device_id: str, key: str):
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM device_states "
                "WHERE device_id = ? AND platform = ? AND key = ?",
                (device_id, self._platform, key),
            )

    def list_devices(self) -> List[str]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT device_id FROM device_states WHERE platform = ?",
                (self._platform,),
            ).fetchall()
        return [row[0] for row in rows]

    # ── High-level helpers ──

    def init_device(self, device_id: str):
        if self.get(device_id, "start_date"):
            return
        now = datetime.now().isoformat()
        defaults = {
            "start_date": now,
            "phase": "cold_start",
            "can_follow": False,
            "follow_test_failures": 0,
            "follow_tested_days": [],
            "follow_unlocked_date": "",
            "active_days_following": 0,
        }
        for k, v in defaults.items():
            self.set(device_id, k, v)
        log.info("[DeviceState] Initialized device %s", device_id[:8])

    def get_device_day(self, device_id: str) -> int:
        start = self.get(device_id, "start_date")
        if not start:
            return 0
        try:
            return (datetime.now() - datetime.fromisoformat(start)).days + 1
        except ValueError:
            return 0

    def get_phase(self, device_id: str) -> str:
        return self.get(device_id, "phase", "cold_start")

    def set_phase(self, device_id: str, phase: str):
        self.set(device_id, "phase", phase)

    def can_follow(self, device_id: str) -> bool:
        return self.get_bool(device_id, "can_follow")

    def mark_can_follow(self, device_id: str, can: bool):
        self.set(device_id, "can_follow", can)
        if can:
            self.set(device_id, "follow_unlocked_date", datetime.now().isoformat())
            self.set(device_id, "phase", "active")
        today = datetime.now().strftime("%Y-%m-%d")
        tested = self.get_json(device_id, "follow_tested_days", [])
        if today not in tested:
            tested.append(today)
            self.set(device_id, "follow_tested_days", tested)

    def determine_phase(self, device_id: str,
                        cold_start_min_watched: int = 100) -> str:
        """
        Evaluate and potentially advance the nurturing phase.

        Uses algorithm learning metrics: if the For You feed shows a high
        ratio of target-country content, the algorithm has learned our
        preferences and we can advance earlier.
        """
        current = self.get_phase(device_id)
        if current == "active":
            return "active"

        if self.can_follow(device_id):
            if current != "active":
                log.info("[%s] Phase → active (follow unlocked)", device_id[:8])
                self.set_phase(device_id, "active")
            return "active"

        total_watched = self.get_int(device_id, "total_watched")
        day = self.get_device_day(device_id)
        algo_score = self.get_algorithm_learning_score(device_id)

        if current == "cold_start":
            can_advance = False
            reason = ""

            # AI-driven: high algorithm learning score → advance early
            if algo_score >= 0.40 and total_watched >= 50:
                can_advance = True
                reason = f"algo_score={algo_score:.0%}, watched={total_watched}"
            elif total_watched >= cold_start_min_watched:
                can_advance = True
                reason = f"watched={total_watched} >= {cold_start_min_watched}"
            elif day >= 3:
                can_advance = True
                reason = f"day={day} >= 3"

            if can_advance:
                log.info("[%s] Phase → interest_building (%s)",
                         device_id[:8], reason)
                self.set_phase(device_id, "interest_building")
                return "interest_building"

        elif current == "interest_building":
            # Auto-advance to active if algorithm strongly aligned
            if algo_score >= 0.60 and total_watched >= 200 and day >= 2:
                log.info("[%s] Phase → active (algo_score=%.0f%%, ready for follow test)",
                         device_id[:8], algo_score * 100)
                self.set_phase(device_id, "active")
                return "active"

        return current

    # ── Algorithm Learning Metrics ──

    def record_feed_analysis(self, device_id: str, target_videos: int,
                             total_videos: int):
        """Record how many For You videos matched the target country."""
        self.increment(device_id, "algo_target_videos", target_videos)
        self.increment(device_id, "algo_total_videos", total_videos)

        today = self._today()
        self.increment(device_id, f"algo:{today}:target", target_videos)
        self.increment(device_id, f"algo:{today}:total", total_videos)

    def get_algorithm_learning_score(self, device_id: str) -> float:
        """
        Calculate how well TikTok's algorithm has learned our preferences.

        Returns 0.0-1.0 ratio of target-country videos in the For You feed.
        Uses exponentially weighted recent sessions for freshness.
        """
        total_target = self.get_int(device_id, "algo_target_videos")
        total_all = self.get_int(device_id, "algo_total_videos")

        if total_all < 10:
            return 0.0

        overall_ratio = total_target / total_all

        # Weight recent days more heavily
        today = self._today()
        today_target = self.get_int(device_id, f"algo:{today}:target")
        today_total = self.get_int(device_id, f"algo:{today}:total")

        if today_total >= 5:
            today_ratio = today_target / today_total
            return 0.4 * overall_ratio + 0.6 * today_ratio

        return overall_ratio

    def get_follow_ramp_max(self, device_id: str,
                            ramp_table: Optional[Dict[int, int]] = None) -> int:
        ramp = ramp_table or {1: 5, 2: 8, 3: 12, 4: 15}
        unlock_date = self.get(device_id, "follow_unlocked_date")
        if not unlock_date:
            return ramp.get(1, 5)
        try:
            days_since = (datetime.now() - datetime.fromisoformat(unlock_date)).days + 1
        except ValueError:
            return ramp.get(1, 5)
        for threshold in sorted(ramp.keys(), reverse=True):
            if days_since >= threshold:
                return ramp[threshold]
        return ramp.get(1, 5)

    # ── Stats (replaces StatsTracker) ──

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def record_warmup(self, device_id: str, warmup_stats: dict):
        watched = warmup_stats.get("watched", 0)
        liked = warmup_stats.get("liked", 0)
        comments = warmup_stats.get("comments_posted", 0)
        day = self._today()

        self.increment(device_id, "total_watched", watched)
        self.increment(device_id, "total_liked", liked)
        self.increment(device_id, "total_comments", comments)
        self.increment(device_id, f"daily:{day}:sessions")
        self.increment(device_id, f"daily:{day}:watched", watched)
        self.increment(device_id, f"daily:{day}:liked", liked)
        self.increment(device_id, f"daily:{day}:comments", comments)

    def record_follow_test(self, device_id: str):
        self.increment(device_id, f"daily:{self._today()}:follow_tests")

    def record_follows(self, device_id: str, count: int):
        self.increment(device_id, "total_followed", count)
        self.increment(device_id, f"daily:{self._today()}:followed", count)

    def record_chats(self, device_id: str, count: int):
        self.increment(device_id, "total_dms_sent", count)
        self.increment(device_id, f"daily:{self._today()}:dms", count)

    def get_sessions_today(self, device_id: str) -> int:
        return self.get_int(device_id, f"daily:{self._today()}:sessions")

    def get_follow_tests_today(self, device_id: str) -> int:
        return self.get_int(device_id, f"daily:{self._today()}:follow_tests")

    def get_device_summary(self, device_id: str) -> dict:
        algo_score = self.get_algorithm_learning_score(device_id)
        recovery_active = self.get_bool(device_id, "recovery_active")
        summary = {
            "device_id": device_id,
            "phase": self.get_phase(device_id),
            "day": self.get_device_day(device_id),
            "can_follow": self.can_follow(device_id),
            "total_watched": self.get_int(device_id, "total_watched"),
            "total_liked": self.get_int(device_id, "total_liked"),
            "total_followed": self.get_int(device_id, "total_followed"),
            "total_dms_sent": self.get_int(device_id, "total_dms_sent"),
            "total_comments": self.get_int(device_id, "total_comments"),
            "start_date": self.get(device_id, "start_date"),
            "follow_unlocked_date": self.get(device_id, "follow_unlocked_date"),
            "sessions_today": self.get_sessions_today(device_id),
            "algorithm_score": round(algo_score, 3),
            "recovery_active": recovery_active,
        }
        if recovery_active:
            summary["recovery_reason"] = self.get(device_id, "recovery_reason")
            summary["recovery_phase_before"] = self.get(device_id, "recovery_phase_before")
        return summary

    # ── Seed Quality Learning ──

    _SEED_DEVICE = "__seeds__"

    def record_seed_quality(self, seed_username: str, country: str,
                            checked: int, followed: int, hit_rate: float):
        """Record actual follow results for a seed account per target country."""
        key = f"seed:{country}:{seed_username}"
        existing = self.get_json(self._SEED_DEVICE, key, {})

        total_checked = existing.get("total_checked", 0) + checked
        total_followed = existing.get("total_followed", 0) + followed
        uses = existing.get("uses", 0) + 1
        avg_hit_rate = total_followed / max(total_checked, 1)

        data = {
            "total_checked": total_checked,
            "total_followed": total_followed,
            "uses": uses,
            "hit_rate": round(avg_hit_rate, 3),
            "last_hit_rate": round(hit_rate, 3),
            "last_used": self._today(),
        }
        self.set(self._SEED_DEVICE, key, data)
        log.info("[SeedQuality] %s (country=%s): hit_rate=%.0f%% (uses=%d, total=%d/%d)",
                 seed_username, country, avg_hit_rate * 100, uses,
                 total_followed, total_checked)

    def get_best_seeds(self, country: str, top_n: int = 10,
                       min_uses: int = 1, min_hit_rate: float = 0.1) -> List[dict]:
        """Return top seeds for a country, sorted by hit rate descending."""
        prefix = f"seed:{country}:"
        all_data = self.get_all(self._SEED_DEVICE)

        seeds = []
        for key, value in all_data.items():
            if not key.startswith(prefix):
                continue
            username = key[len(prefix):]
            try:
                data = json.loads(value) if isinstance(value, str) else value
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("uses", 0) < min_uses:
                continue
            if data.get("hit_rate", 0) < min_hit_rate:
                continue
            data["username"] = username
            seeds.append(data)

        seeds.sort(key=lambda s: (s.get("hit_rate", 0), s.get("uses", 0)),
                   reverse=True)
        return seeds[:top_n]

    def get_seed_quality(self, seed_username: str, country: str) -> Optional[dict]:
        key = f"seed:{country}:{seed_username}"
        return self.get_json(self._SEED_DEVICE, key, None)

    # ── Multi-Account Support ──

    @staticmethod
    def account_device_id(device_id: str, account: str) -> str:
        """Build a composite key for per-account state: 'DEVICE::account'."""
        if not account or "::" in device_id:
            return device_id
        return f"{device_id}::{account}"

    @staticmethod
    def split_device_account(composite_id: str):
        """Split composite id back to (device_id, account)."""
        if "::" in composite_id:
            parts = composite_id.split("::", 1)
            return parts[0], parts[1]
        return composite_id, ""

    def init_account(self, device_id: str, account: str):
        """Initialize state for a specific account on a device."""
        cid = self.account_device_id(device_id, account)
        self.init_device(cid)
        self.set(cid, "account_name", account)
        self.set(cid, "physical_device", device_id)

    def list_accounts(self, device_id: str) -> List[str]:
        """List all accounts registered for a physical device."""
        prefix = f"{device_id}::"
        devices = self.list_devices()
        accounts = []
        for d in devices:
            if d.startswith(prefix):
                _, acct = self.split_device_account(d)
                accounts.append(acct)
        if device_id in devices:
            main_acct = self.get(device_id, "account_name")
            if main_acct and main_acct not in accounts:
                accounts.insert(0, main_acct)
        return accounts

    def get_account_summaries(self, device_id: str) -> List[dict]:
        """Get summaries for all accounts on a device."""
        accounts = self.list_accounts(device_id)
        summaries = []
        for acct in accounts:
            cid = self.account_device_id(device_id, acct)
            summary = self.get_device_summary(cid)
            summary["account"] = acct
            summary["physical_device"] = device_id
            summaries.append(summary)
        if not accounts:
            summary = self.get_device_summary(device_id)
            summary["account"] = ""
            summary["physical_device"] = device_id
            summaries.append(summary)
        return summaries


# ── Singleton ──

_stores: Dict[str, DeviceStateStore] = {}
_lock = threading.Lock()


def get_device_state_store(platform: str = "tiktok") -> DeviceStateStore:
    if platform not in _stores:
        with _lock:
            if platform not in _stores:
                _stores[platform] = DeviceStateStore(platform)
    return _stores[platform]
