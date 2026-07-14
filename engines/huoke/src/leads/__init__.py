"""
Leads module — lightweight CRM for multi-platform customer acquisition.

Components:
  store           — SQLite-backed lead management, interaction tracking, scoring, dedup
  follow_tracker  — Adapter bridging platform automation with LeadsStore for follow dedup
"""

from .store import (
    LeadsStore,
    get_leads_store,
    normalize_name,
    normalize_phone,
    normalize_email,
    LEAD_STATUSES,
)
from .follow_tracker import LeadsFollowTracker

__all__ = [
    "LeadsStore", "get_leads_store",
    "LeadsFollowTracker",
    "normalize_name", "normalize_phone", "normalize_email",
    "LEAD_STATUSES",
]
