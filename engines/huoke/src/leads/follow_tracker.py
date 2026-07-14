# -*- coding: utf-8 -*-
"""
LeadsFollowTracker — drop-in replacement for the JSON-based GlobalFollowTracker.

Bridges the TikTok automation module with the central LeadsStore CRM.
All follow/followback/DM data flows through SQLite (leads.db) instead
of a flat global_followed.json, giving us:
  - Native WAL-mode concurrency (no manual file locks)
  - Unified CRM with scoring, status pipeline, cross-platform dedup
  - Queryable interaction history

Interface is compatible with GlobalFollowTracker so tiktok.py and
tiktok_runner.py need zero changes to switch over.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .store import LeadsStore, get_leads_store

log = logging.getLogger(__name__)


class LeadsFollowTracker:
    """
    Adapter providing the GlobalFollowTracker interface backed by LeadsStore.

    Usage:
        tracker = LeadsFollowTracker()
        tracker.record_follow("mario_rossi", "DEVICE01", display_name="Mario")
        assert tracker.is_followed("mario_rossi")
    """

    def __init__(self, store: Optional[LeadsStore] = None, platform: str = "tiktok"):
        self._store = store or get_leads_store()
        self._platform = platform

    def is_followed(self, username: str) -> bool:
        return self._store.is_followed_on_platform(self._platform, username)

    def record_follow(self, username: str, device_id: str, *,
                      display_name: str = "", score: float = 0,
                      seed: str = ""):
        lead_id = self._store.find_by_platform_username(self._platform, username)
        if lead_id is None:
            lead_id = self._store.add_lead(
                name=display_name or username,
                source_platform=self._platform,
                tags=["auto_follow"],
            )
            self._store.add_platform_profile(
                lead_id, self._platform, username=username,
            )
        self._store.add_interaction(
            lead_id, self._platform, "follow",
            direction="outbound", content=f"seed={seed}",
            metadata={"device_id": device_id, "score": round(score, 2), "seed": seed},
        )
        if score > 0:
            self._store.update_lead(lead_id, score=score)

    def record_followback(self, username: str):
        # P10-B: 跳过 position key（Vision AI 未能提取真实用户名时产生的占位 key）
        if not username or username.startswith("newfollower_"):
            return
        lead_id = self._store.find_by_platform_username(self._platform, username)
        if lead_id is None:
            # P10-B: followback 用户可能从未被关注过（跨设备），直接建档
            lead_id = self._store.add_lead(
                name=username,
                source_platform=self._platform,
                tags=["followback"],
            )
            self._store.add_platform_profile(
                lead_id, self._platform, username=username,
            )
        self._store.add_interaction(
            lead_id, self._platform, "follow_back",
            direction="inbound",
        )
        self._store.update_lead(lead_id, status="responded")
        self._store.update_score(lead_id)
        log.info("[LeadsTracker] Followback recorded: %s", username)

    def record_dm(self, username: str, message: str, variant_id: str = ""):
        # P10-B: 跳过 position key
        if not username or username.startswith("newfollower_"):
            return
        lead_id = self._store.find_by_platform_username(self._platform, username)
        if lead_id is None:
            return
        _meta = {"ab_variant": variant_id} if variant_id else None
        self._store.add_interaction(
            lead_id, self._platform, "send_dm",
            direction="outbound", content=message[:500],
            metadata=_meta,
        )
        # 只在 new 状态时升为 contacted；不降级已经更高状态的 lead
        lead = self._store.get_lead(lead_id)
        if lead and lead.get("status") == "new":
            self._store.update_lead(lead_id, status="contacted")

    def record_dm_received(self, username: str, message: str = ""):
        """记录对方主动发来的 DM（收件箱新消息）。
        add_interaction 内的 _maybe_advance_lead_status 会自动推进状态。
        """
        lead_id = self._store.find_by_platform_username(self._platform, username)
        if lead_id is None:
            return
        self._store.add_interaction(
            lead_id, self._platform, "dm_received",
            direction="inbound", content=message[:500],
        )
        log.info("[LeadsTracker] DM received from: %s", username)

    def was_dm_sent(self, username: str) -> bool:
        """跨设备去重：该用户是否已经收到过任何设备发出的 DM。"""
        return self._store.has_dm_sent(username, self._platform)

    def get_stats(self) -> dict:
        return self._store.get_follow_stats(self._platform)

    def get_device_follows(self, device_id: str) -> int:
        return self._store.get_followed_count_by_device(self._platform, device_id)
