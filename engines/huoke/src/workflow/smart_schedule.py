"""
Smart Scheduling — timezone-aware scheduling with activity windows.

Extends the base scheduler with:
- Timezone awareness (per-schedule timezone)
- Activity windows (only run within specified hours)
- Jitter (randomize ±N minutes to avoid detection patterns)
- Weekend mode (different behavior on weekends)
- Workflow triggering (schedule can trigger a workflow YAML instead of a task)
- Blackout periods (holidays, maintenance)

Design: This module provides SmartScheduleConfig and check functions.
The existing SchedulerThread calls check_smart_constraints() before
executing a scheduled item.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


@dataclass
class ActivityWindow:
    """Time window during which automation is allowed."""
    start_hour: int = 8
    start_minute: int = 0
    end_hour: int = 22
    end_minute: int = 0

    def contains(self, dt: datetime) -> bool:
        start = dt.replace(hour=self.start_hour, minute=self.start_minute, second=0)
        end = dt.replace(hour=self.end_hour, minute=self.end_minute, second=0)
        if end < start:
            return dt >= start or dt <= end
        return start <= dt <= end


@dataclass
class WeekendConfig:
    enabled: bool = True
    window: Optional[ActivityWindow] = None
    rate_multiplier: float = 0.5

    def __post_init__(self):
        if self.window is None:
            self.window = ActivityWindow(start_hour=10, end_hour=20)


@dataclass
class SmartScheduleConfig:
    timezone: str = "UTC"
    activity_window: ActivityWindow = field(default_factory=ActivityWindow)
    weekend: WeekendConfig = field(default_factory=WeekendConfig)
    jitter_minutes: int = 5
    blackout_dates: List[str] = field(default_factory=list)
    max_daily_runs: int = 0

    @staticmethod
    def from_dict(d: dict) -> SmartScheduleConfig:
        aw_data = d.get("activity_window", {})
        aw = ActivityWindow(**aw_data) if aw_data else ActivityWindow()

        we_data = d.get("weekend", {})
        we_window = None
        if "window" in we_data:
            we_window = ActivityWindow(**we_data.pop("window"))
        we = WeekendConfig(**we_data) if we_data else WeekendConfig()
        if we_window:
            we.window = we_window

        return SmartScheduleConfig(
            timezone=d.get("timezone", "UTC"),
            activity_window=aw,
            weekend=we,
            jitter_minutes=d.get("jitter_minutes", 5),
            blackout_dates=d.get("blackout_dates", []),
            max_daily_runs=d.get("max_daily_runs", 0),
        )


def check_smart_constraints(config: SmartScheduleConfig,
                             daily_run_count: int = 0) -> Tuple[bool, str, float]:
    """
    Check if current time satisfies smart scheduling constraints.

    Returns:
        (allowed, reason, jitter_seconds)
        - allowed: True if execution should proceed
        - reason: human-readable explanation
        - jitter_seconds: random delay to add before execution
    """
    try:
        tz = ZoneInfo(config.timezone)
    except Exception:
        tz = timezone.utc

    now = datetime.now(tz)
    is_weekend = now.weekday() >= 5

    # Blackout dates
    today_str = now.strftime("%Y-%m-%d")
    if today_str in config.blackout_dates:
        return False, f"blackout date: {today_str}", 0.0

    # Daily limit
    if config.max_daily_runs > 0 and daily_run_count >= config.max_daily_runs:
        return False, f"daily limit reached ({daily_run_count}/{config.max_daily_runs})", 0.0

    # Activity window
    if is_weekend:
        if not config.weekend.enabled:
            return False, "weekend mode disabled", 0.0
        window = config.weekend.window or config.activity_window
    else:
        window = config.activity_window

    if not window.contains(now):
        return False, (
            f"outside activity window ({window.start_hour:02d}:{window.start_minute:02d}"
            f"-{window.end_hour:02d}:{window.end_minute:02d})"
        ), 0.0

    # Jitter
    jitter_sec = random.uniform(0, config.jitter_minutes * 60) if config.jitter_minutes > 0 else 0.0

    return True, "ok", jitter_sec


def next_available_time(config: SmartScheduleConfig) -> datetime:
    """Calculate the next time that satisfies all constraints."""
    try:
        tz = ZoneInfo(config.timezone)
    except Exception:
        tz = timezone.utc

    now = datetime.now(tz)

    for delta_hours in range(48):
        candidate = now + timedelta(hours=delta_hours)
        is_weekend = candidate.weekday() >= 5

        date_str = candidate.strftime("%Y-%m-%d")
        if date_str in config.blackout_dates:
            continue

        if is_weekend and not config.weekend.enabled:
            continue

        window = (config.weekend.window or config.activity_window) if is_weekend else config.activity_window
        if window.contains(candidate):
            return candidate

    return now + timedelta(hours=48)


def get_rate_multiplier(config: SmartScheduleConfig) -> float:
    """
    Get current rate multiplier based on time of day and weekend status.
    Used to dynamically adjust compliance limits.
    """
    try:
        tz = ZoneInfo(config.timezone)
    except Exception:
        tz = timezone.utc

    now = datetime.now(tz)
    hour = now.hour

    if now.weekday() >= 5:
        return config.weekend.rate_multiplier

    # Ramp up in morning, full speed midday, ramp down evening
    if hour < 9:
        return 0.3
    if hour < 11:
        return 0.7
    if hour < 16:
        return 1.0
    if hour < 19:
        return 0.7
    return 0.3


# -- Default config ----------------------------------------------------------

_DEFAULT_CONFIG = SmartScheduleConfig(
    timezone="Asia/Manila",
    activity_window=ActivityWindow(start_hour=8, end_hour=22),
    weekend=WeekendConfig(
        enabled=True,
        window=ActivityWindow(start_hour=10, end_hour=20),
        rate_multiplier=0.5,
    ),
    jitter_minutes=5,
)


def get_default_config() -> SmartScheduleConfig:
    return _DEFAULT_CONFIG


# ── Timezone-Aware Lead Scheduling ────────────────────────────────────────

TIMEZONE_MAP = {
    "US": "America/New_York",
    "UK": "Europe/London",
    "EU": "Europe/Berlin",
    "CN": "Asia/Shanghai",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "IN": "Asia/Kolkata",
    "PH": "Asia/Manila",
    "SG": "Asia/Singapore",
    "AU": "Australia/Sydney",
    "BR": "America/Sao_Paulo",
    "CA": "America/Toronto",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "IT": "Europe/Rome",
    "ES": "Europe/Madrid",
    "NL": "Europe/Amsterdam",
    "CH": "Europe/Zurich",
    "AT": "Europe/Vienna",
    "BE": "Europe/Brussels",
    "PT": "Europe/Lisbon",
    "GR": "Europe/Athens",
    "RU": "Europe/Moscow",
    "TR": "Europe/Istanbul",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "ITALY": "Europe/Rome",
}

OPTIMAL_HOURS = {
    "linkedin": (8, 10, 17, 19),   # morning 8-10, evening 17-19
    "twitter": (7, 9, 12, 14, 17, 20),
    "tiktok": (11, 13, 19, 22),
    "whatsapp": (9, 11, 14, 16),
    "telegram": (9, 12, 18, 21),
    "facebook": (9, 11, 13, 16),
    "instagram": (11, 13, 19, 21),
}


def best_send_time(lead_timezone: str, platform: str = "",
                   avoid_weekends: bool = True) -> Optional[datetime]:
    """
    Calculate the best time to send a message to a lead,
    based on their timezone and platform-specific engagement patterns.

    Returns a datetime in UTC that maps to an optimal local time for the lead.
    """
    try:
        tz = ZoneInfo(lead_timezone)
    except Exception:
        mapped = TIMEZONE_MAP.get(lead_timezone.upper(), "")
        if mapped:
            tz = ZoneInfo(mapped)
        else:
            return None

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    hours = OPTIMAL_HOURS.get(platform, (9, 11, 14, 17))
    hour_pairs = [(hours[i], hours[i + 1]) for i in range(0, len(hours) - 1, 2)]

    for delta_days in range(7):
        candidate_day = now_local + timedelta(days=delta_days)

        if avoid_weekends and candidate_day.weekday() >= 5:
            continue

        for start_h, end_h in hour_pairs:
            candidate = candidate_day.replace(
                hour=start_h, minute=random.randint(0, 30),
                second=0, microsecond=0,
            )
            if candidate > now_local:
                return candidate.astimezone(timezone.utc)

    # Fallback: next weekday 10 AM local
    for delta in range(1, 8):
        candidate = now_local + timedelta(days=delta)
        if candidate.weekday() < 5:
            return candidate.replace(hour=10, minute=0).astimezone(timezone.utc)

    return now_utc + timedelta(hours=12)


def schedule_for_leads(leads: list, platform: str = "") -> list:
    """
    Schedule optimal send times for a batch of leads.

    Args:
        leads: list of dicts with at least {"lead_id": int, "timezone": str}
        platform: target platform

    Returns: list of {"lead_id": int, "send_at": str (ISO UTC), "local_time": str}
    """
    scheduled = []
    for lead in leads:
        tz_str = lead.get("timezone", lead.get("location", ""))
        send_at = best_send_time(tz_str, platform)
        if send_at:
            try:
                tz = ZoneInfo(TIMEZONE_MAP.get(tz_str.upper(), tz_str))
                local = send_at.astimezone(tz).strftime("%H:%M %Z")
            except Exception:
                local = send_at.strftime("%H:%M UTC")
            scheduled.append({
                "lead_id": lead.get("lead_id"),
                "send_at": send_at.isoformat(),
                "local_time": local,
            })
    return scheduled
