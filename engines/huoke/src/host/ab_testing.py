# -*- coding: utf-8 -*-
"""
A/B Testing Framework — lightweight experiment tracking for automation strategies.

Supports split-testing:
- Chat message templates (which opening line converts better)
- Seed account strategies (which seed selection method yields more follows)
- Warmup rhythms (how many sessions/day is optimal)
- Engagement patterns (like probability, comment frequency)

Usage:
    ab = get_ab_store()

    # Create experiment
    ab.create("dm_opening_v2", "message",
              variants=["casual_hi", "question_opener", "compliment_first"])

    # Assign a variant to a device/interaction
    variant = ab.assign("dm_opening_v2", device_id="DEVICE01")

    # Record outcomes
    ab.record("dm_opening_v2", variant, "sent", device_id="DEVICE01")
    ab.record("dm_opening_v2", variant, "reply_received", device_id="DEVICE01")

    # Analyze
    results = ab.analyze("dm_opening_v2")
    # → {"casual_hi": {"sent": 45, "reply_received": 12, "conversion": 0.267}, ...}

    best = ab.best_variant("dm_opening_v2", metric="reply_received")
    # → "question_opener"
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import get_conn

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ABTestStore:
    """SQLite-backed A/B experiment tracking."""

    def create(self, name: str, category: str = "general",
               variants: Optional[List[str]] = None) -> str:
        """Create a new experiment. Returns experiment_id."""
        exp_id = str(uuid.uuid4())[:8]
        variants = variants or ["control", "variant_a"]
        now = _now_iso()

        with get_conn() as conn:
            existing = conn.execute(
                "SELECT experiment_id FROM experiments WHERE name = ? AND status = 'active'",
                (name,),
            ).fetchone()
            if existing:
                return existing[0]

            conn.execute(
                "INSERT INTO experiments (experiment_id, name, category, status, variants, created_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (exp_id, name, category, json.dumps(variants), now),
            )

        log.info("[A/B] Created experiment '%s' (%s) variants=%s", name, exp_id, variants)
        return exp_id

    def assign(self, experiment_name: str, device_id: str = "",
               user_id: str = "") -> str:
        """
        Deterministically assign a variant based on device/user ID.

        Uses consistent hashing so the same device always gets the same variant.
        """
        exp = self._get_experiment(experiment_name)
        if not exp:
            return "control"

        variants = json.loads(exp["variants"])
        if not variants:
            return "control"

        seed = f"{experiment_name}:{device_id or user_id}"
        h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        idx = h % len(variants)
        return variants[idx]

    def record(self, experiment_name: str, variant: str, event_type: str,
               device_id: str = "", metadata: Optional[dict] = None):
        """Record an event for a variant."""
        exp = self._get_experiment(experiment_name)
        if not exp:
            return

        with get_conn() as conn:
            conn.execute(
                "INSERT INTO experiment_events "
                "(experiment_id, variant, event_type, device_id, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (exp["experiment_id"], variant, event_type, device_id,
                 json.dumps(metadata or {}), _now_iso()),
            )

    def analyze(self, experiment_name: str) -> Dict[str, Dict[str, Any]]:
        """
        Analyze experiment results.

        Returns per-variant event counts and conversion rates.
        """
        exp = self._get_experiment(experiment_name)
        if not exp:
            return {}

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT variant, event_type, COUNT(*) as cnt "
                "FROM experiment_events WHERE experiment_id = ? "
                "GROUP BY variant, event_type",
                (exp["experiment_id"],),
            ).fetchall()

        results: Dict[str, Dict[str, Any]] = {}
        variants = json.loads(exp["variants"])
        for v in variants:
            results[v] = {"total_events": 0}

        for row in rows:
            v = row[0]
            if v not in results:
                results[v] = {"total_events": 0}
            results[v][row[1]] = row[2]
            results[v]["total_events"] += row[2]

        # Calculate conversion rates
        for v, data in results.items():
            sent = data.get("sent", 0)
            replied = data.get("reply_received", 0)
            followed = data.get("followed", 0)
            converted = data.get("converted", 0)

            if sent > 0:
                data["reply_rate"] = round(replied / sent, 4)
            if followed > 0:
                data["followback_rate"] = round(
                    data.get("followback_received", 0) / followed, 4)
            if sent > 0:
                data["conversion_rate"] = round(converted / sent, 4)

        return results

    def best_variant(self, experiment_name: str,
                     metric: str = "reply_received",
                     min_samples: int = 5) -> str:
        """Find the best-performing variant for a given metric."""
        results = self.analyze(experiment_name)
        if not results:
            return "control"

        best = "control"
        best_rate = -1.0

        for variant, data in results.items():
            total = data.get("total_events", 0)
            if total < min_samples:
                continue

            sent = data.get("sent", 1)
            value = data.get(metric, 0)
            rate = value / max(sent, 1)

            if rate > best_rate:
                best_rate = rate
                best = variant

        return best

    def end_experiment(self, experiment_name: str):
        """Mark experiment as completed."""
        exp = self._get_experiment(experiment_name)
        if not exp:
            return

        with get_conn() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'completed', ended_at = ? "
                "WHERE experiment_id = ?",
                (_now_iso(), exp["experiment_id"]),
            )
        log.info("[A/B] Ended experiment '%s'", experiment_name)

    def list_experiments(self, status: str = "") -> List[dict]:
        """List all experiments, optionally filtered by status."""
        with get_conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM experiments WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM experiments ORDER BY created_at DESC"
                ).fetchall()

        return [self._row_to_dict(r) for r in rows]

    def get_experiment_summary(self, experiment_name: str) -> Optional[dict]:
        """Full summary with experiment info + analysis."""
        exp = self._get_experiment(experiment_name)
        if not exp:
            return None
        analysis = self.analyze(experiment_name)
        best = self.best_variant(experiment_name)
        return {
            "experiment": self._row_to_dict(exp),
            "analysis": analysis,
            "best_variant": best,
        }

    def _get_experiment(self, name: str):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE name = ? ORDER BY created_at DESC LIMIT 1",
                (name,),
            ).fetchone()
        return row

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        if "variants" in d and isinstance(d["variants"], str):
            try:
                d["variants"] = json.loads(d["variants"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d


# ── Singleton ──

_store: Optional[ABTestStore] = None
_lock = threading.Lock()


def get_ab_store() -> ABTestStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = ABTestStore()
    return _store
