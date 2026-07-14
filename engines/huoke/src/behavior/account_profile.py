# -*- coding: utf-8 -*-
"""
账号行为画像 — 追踪每账号浏览偏好、互动模式、算法学习曲线。

基于历史数据动态调整养号参数:
  - 高算法学习分 → 缩短 warmup 时长，提高互动率
  - 低互动率 → 增加点赞概率
  - 内容偏好收敛 → 可开始关注阶段
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import data_dir

log = logging.getLogger(__name__)

_PROFILE_DIR = data_dir() / "profiles"


@dataclass
class ContentDistribution:
    """Tracks what content categories the algorithm serves."""
    categories: Dict[str, int] = field(default_factory=dict)
    languages: Dict[str, int] = field(default_factory=dict)
    total_samples: int = 0

    def record(self, category: str = "", language: str = ""):
        self.total_samples += 1
        if category:
            self.categories[category] = self.categories.get(category, 0) + 1
        if language:
            self.languages[language] = self.languages.get(language, 0) + 1

    @property
    def top_category(self) -> str:
        if not self.categories:
            return ""
        return max(self.categories, key=self.categories.get)

    @property
    def concentration(self) -> float:
        """0-1: how focused the content is on top category."""
        if not self.categories or self.total_samples == 0:
            return 0.0
        top_count = max(self.categories.values())
        return top_count / self.total_samples

    def to_dict(self) -> dict:
        return {
            "categories": dict(self.categories),
            "languages": dict(self.languages),
            "total_samples": self.total_samples,
        }

    @staticmethod
    def from_dict(d: dict) -> ContentDistribution:
        cd = ContentDistribution()
        cd.categories = d.get("categories", {})
        cd.languages = d.get("languages", {})
        cd.total_samples = d.get("total_samples", 0)
        return cd


@dataclass
class InteractionPattern:
    """Tracks interaction rates over time."""
    total_watched: int = 0
    total_liked: int = 0
    total_commented: int = 0
    total_shared: int = 0
    total_followed: int = 0
    total_sessions: int = 0
    total_minutes: float = 0.0

    @property
    def like_rate(self) -> float:
        return self.total_liked / max(self.total_watched, 1)

    @property
    def comment_rate(self) -> float:
        return self.total_commented / max(self.total_watched, 1)

    @property
    def engagement_rate(self) -> float:
        eng = self.total_liked + self.total_commented + self.total_shared
        return eng / max(self.total_watched, 1)

    @property
    def avg_session_minutes(self) -> float:
        return self.total_minutes / max(self.total_sessions, 1)

    def record_session(self, watched: int = 0, liked: int = 0,
                       commented: int = 0, shared: int = 0,
                       followed: int = 0, duration_min: float = 0):
        self.total_watched += watched
        self.total_liked += liked
        self.total_commented += commented
        self.total_shared += shared
        self.total_followed += followed
        self.total_sessions += 1
        self.total_minutes += duration_min

    def to_dict(self) -> dict:
        return {
            "total_watched": self.total_watched,
            "total_liked": self.total_liked,
            "total_commented": self.total_commented,
            "total_shared": self.total_shared,
            "total_followed": self.total_followed,
            "total_sessions": self.total_sessions,
            "total_minutes": round(self.total_minutes, 1),
        }

    @staticmethod
    def from_dict(d: dict) -> InteractionPattern:
        ip = InteractionPattern()
        ip.total_watched = d.get("total_watched", 0)
        ip.total_liked = d.get("total_liked", 0)
        ip.total_commented = d.get("total_commented", 0)
        ip.total_shared = d.get("total_shared", 0)
        ip.total_followed = d.get("total_followed", 0)
        ip.total_sessions = d.get("total_sessions", 0)
        ip.total_minutes = d.get("total_minutes", 0.0)
        return ip


@dataclass
class AlgoLearningCurve:
    """Tracks algorithm learning progression."""
    daily_scores: Dict[str, float] = field(default_factory=dict)
    daily_samples: Dict[str, int] = field(default_factory=dict)

    def record_day(self, date_str: str, score: float, samples: int):
        self.daily_scores[date_str] = score
        self.daily_samples[date_str] = samples
        if len(self.daily_scores) > 60:
            oldest = sorted(self.daily_scores.keys())[:-60]
            for k in oldest:
                self.daily_scores.pop(k, None)
                self.daily_samples.pop(k, None)

    @property
    def trend(self) -> str:
        """'improving', 'stable', 'declining', or 'unknown'."""
        dates = sorted(self.daily_scores.keys())
        if len(dates) < 3:
            return "unknown"
        recent = [self.daily_scores[d] for d in dates[-3:]]
        older = [self.daily_scores[d] for d in dates[-6:-3]] if len(dates) >= 6 else []
        if not older:
            return "unknown"
        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)
        diff = recent_avg - older_avg
        if diff > 0.05:
            return "improving"
        if diff < -0.05:
            return "declining"
        return "stable"

    @property
    def latest_score(self) -> float:
        if not self.daily_scores:
            return 0.0
        latest = max(self.daily_scores.keys())
        return self.daily_scores[latest]

    def to_dict(self) -> dict:
        return {
            "daily_scores": dict(self.daily_scores),
            "daily_samples": dict(self.daily_samples),
        }

    @staticmethod
    def from_dict(d: dict) -> AlgoLearningCurve:
        alc = AlgoLearningCurve()
        alc.daily_scores = d.get("daily_scores", {})
        alc.daily_samples = d.get("daily_samples", {})
        return alc


class AccountProfile:
    """
    Complete behavioral profile for a single account.
    Combines content distribution, interaction patterns, and learning curve.
    """

    def __init__(self, device_id: str, account: str):
        self.device_id = device_id
        self.account = account
        self.content = ContentDistribution()
        self.interactions = InteractionPattern()
        self.algo_curve = AlgoLearningCurve()
        self.created_at = time.time()
        self.updated_at = time.time()

    def get_adaptive_params(self) -> dict:
        """
        Generate optimized warmup parameters based on this account's profile.
        """
        algo_score = self.algo_curve.latest_score
        trend = self.algo_curve.trend
        like_rate = self.interactions.like_rate
        sessions = self.interactions.total_sessions
        concentration = self.content.concentration

        params = {
            "duration_minutes": 30,
            "like_probability": 0.20,
            "comment_browse_prob": 0.15,
            "comment_post_prob": 0.03,
            "search_prob": 0.10,
        }

        if algo_score >= 0.6:
            params["duration_minutes"] = 25
            params["like_probability"] = 0.30
            params["comment_post_prob"] = 0.05
            params["search_prob"] = 0.05
        elif algo_score >= 0.4:
            params["duration_minutes"] = 30
            params["like_probability"] = 0.25
        else:
            params["duration_minutes"] = 35
            params["like_probability"] = 0.15
            params["search_prob"] = 0.15

        if trend == "declining":
            params["duration_minutes"] += 5
            params["search_prob"] = min(0.20, params["search_prob"] + 0.05)

        if like_rate < 0.10 and sessions >= 3:
            params["like_probability"] = min(0.35, params["like_probability"] + 0.10)

        if concentration >= 0.7 and algo_score >= 0.5:
            params["search_prob"] = max(0.03, params["search_prob"] - 0.05)

        params["_profile_based"] = True
        params["_algo_score"] = round(algo_score, 3)
        params["_trend"] = trend

        return params

    def get_summary(self) -> dict:
        return {
            "device_id": self.device_id,
            "account": self.account,
            "algo_score": round(self.algo_curve.latest_score, 3),
            "algo_trend": self.algo_curve.trend,
            "engagement_rate": round(self.interactions.engagement_rate, 4),
            "like_rate": round(self.interactions.like_rate, 4),
            "comment_rate": round(self.interactions.comment_rate, 4),
            "sessions": self.interactions.total_sessions,
            "total_watched": self.interactions.total_watched,
            "avg_session_min": round(self.interactions.avg_session_minutes, 1),
            "content_concentration": round(self.content.concentration, 3),
            "top_category": self.content.top_category,
            "adaptive_params": self.get_adaptive_params(),
        }

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "account": self.account,
            "content": self.content.to_dict(),
            "interactions": self.interactions.to_dict(),
            "algo_curve": self.algo_curve.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> AccountProfile:
        p = AccountProfile(d.get("device_id", ""), d.get("account", ""))
        p.content = ContentDistribution.from_dict(d.get("content", {}))
        p.interactions = InteractionPattern.from_dict(d.get("interactions", {}))
        p.algo_curve = AlgoLearningCurve.from_dict(d.get("algo_curve", {}))
        p.created_at = d.get("created_at", time.time())
        p.updated_at = d.get("updated_at", time.time())
        return p


class ProfileManager:
    """Manages all account profiles with persistence."""

    def __init__(self):
        self._lock = threading.Lock()
        self._profiles: Dict[str, AccountProfile] = {}
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def get_or_create(self, device_id: str,
                      account: str) -> AccountProfile:
        key = f"{device_id}::{account}" if account else device_id
        with self._lock:
            if key not in self._profiles:
                self._profiles[key] = AccountProfile(device_id, account)
            return self._profiles[key]

    def record_session(self, device_id: str, account: str,
                       warmup_stats: dict, duration_min: float = 0):
        """Record a warmup session in the account profile."""
        profile = self.get_or_create(device_id, account)
        profile.interactions.record_session(
            watched=warmup_stats.get("watched", 0),
            liked=warmup_stats.get("liked", 0),
            commented=warmup_stats.get("comments_posted", 0),
            shared=warmup_stats.get("shared", 0),
            followed=warmup_stats.get("followed", 0),
            duration_min=duration_min,
        )
        profile.updated_at = time.time()
        self._save_profile(profile)

    def record_content(self, device_id: str, account: str,
                       category: str = "", language: str = ""):
        """Record a content observation (what TikTok is serving)."""
        profile = self.get_or_create(device_id, account)
        profile.content.record(category, language)
        profile.updated_at = time.time()

    def record_algo_score(self, device_id: str, account: str,
                          score: float, date_str: str = "",
                          samples: int = 0):
        """Record algorithm learning score for a day."""
        import time as _time
        if not date_str:
            date_str = _time.strftime("%Y-%m-%d")
        profile = self.get_or_create(device_id, account)
        profile.algo_curve.record_day(date_str, score, samples)
        profile.updated_at = time.time()
        self._save_profile(profile)

    def get_adaptive_params(self, device_id: str,
                            account: str) -> Optional[dict]:
        """Get profile-based adaptive params if profile exists."""
        key = f"{device_id}::{account}" if account else device_id
        with self._lock:
            profile = self._profiles.get(key)
        if not profile:
            return None
        if profile.interactions.total_sessions < 2:
            return None
        return profile.get_adaptive_params()

    def get_summary(self, device_id: str,
                    account: str = "") -> Optional[dict]:
        key = f"{device_id}::{account}" if account else device_id
        with self._lock:
            profile = self._profiles.get(key)
        if not profile:
            return None
        return profile.get_summary()

    def get_all_summaries(self) -> List[dict]:
        with self._lock:
            profiles = list(self._profiles.values())
        return [p.get_summary() for p in profiles]

    def _save_profile(self, profile: AccountProfile):
        """Save a single profile to disk."""
        try:
            key = f"{profile.device_id}::{profile.account}" if profile.account else profile.device_id
            safe_name = key.replace(":", "_").replace("/", "_")
            path = _PROFILE_DIR / f"{safe_name}.json"
            path.write_text(
                json.dumps(profile.to_dict(), indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            log.debug("[Profile] 保存失败: %s", e)

    def _load_all(self):
        """Load all saved profiles from disk."""
        try:
            for f in _PROFILE_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    profile = AccountProfile.from_dict(data)
                    key = (f"{profile.device_id}::{profile.account}"
                           if profile.account else profile.device_id)
                    self._profiles[key] = profile
                except Exception:
                    continue
            if self._profiles:
                log.info("[Profile] 已加载 %d 个账号画像",
                         len(self._profiles))
        except Exception:
            pass


_manager: Optional[ProfileManager] = None
_mgr_lock = threading.Lock()


def get_profile_manager() -> ProfileManager:
    global _manager
    if _manager is None:
        with _mgr_lock:
            if _manager is None:
                _manager = ProfileManager()
    return _manager
