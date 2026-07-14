# -*- coding: utf-8 -*-
"""
Adaptive Compliance — dynamic rate limit adjustment + recovery strategy.

Standard ComplianceGuard uses fixed limits. This module monitors behavioral
signals and adjusts limits per-device to avoid detection:

Risk Signals (increase risk score):
  - Follow failure rate > 20% → account may be soft-limited
  - Consecutive errors for same action → throttle that action
  - Account age < 7 days → new account needs conservative limits
  - Pattern regularity → actions at exactly same intervals look robotic
  - High daily volume relative to account age

Risk Responses (lower risk = more aggressive):
  - risk_score 0.0-0.3: normal limits (100%)
  - risk_score 0.3-0.6: reduced limits (70%)
  - risk_score 0.6-0.8: conservative limits (40%) + enter recovery mode
  - risk_score 0.8-1.0: minimal activity (20%) + recovery mode + alert human

Recovery Strategy (when risk >= high):
  Account is NOT abandoned — it enters recovery mode:
  1. Phase auto-regresses to recovery warmup (passive browsing only)
  2. Sensitive actions blocked: follow, send_dm, comment
  3. Allowed actions: browse_feed, like (at cold_start rate), watch videos
  4. Recovery sessions count toward algorithm learning
  5. After N successful recovery sessions with no failures:
     risk signals decay → score drops → auto-exit recovery
  6. Recovery progress tracked (recovery_sessions, recovery_start_date)

Integrates with:
  - ComplianceGuard: scales action limits
  - DeviceStateStore: persists risk data and recovery state
  - EventBus: emits risk alerts and recovery progress
  - ABTestStore: tracks which patterns trigger restrictions
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RISK_THRESHOLDS = {
    "low": (0.0, 0.3),
    "medium": (0.3, 0.6),
    "high": (0.6, 0.8),
    "critical": (0.8, 1.0),
}

RISK_MULTIPLIERS = {
    "low": 1.0,
    "medium": 0.7,
    "high": 0.4,
    "critical": 0.2,
}

# Actions completely blocked during recovery
RECOVERY_BLOCKED_ACTIONS = {"follow", "send_dm", "comment", "share"}

# Actions allowed during recovery (passive behaviors)
RECOVERY_ALLOWED_ACTIONS = {"browse_feed", "like", "search", "watch"}

# Number of clean recovery sessions needed before exiting recovery
RECOVERY_SESSIONS_REQUIRED = 3


@dataclass
class RiskSignal:
    name: str
    weight: float
    value: float = 0.0
    threshold: float = 0.0

    @property
    def contribution(self) -> float:
        if self.threshold <= 0:
            return 0.0
        return min(1.0, self.value / self.threshold) * self.weight


@dataclass
class DeviceRiskProfile:
    device_id: str
    signals: Dict[str, RiskSignal] = field(default_factory=dict)
    _last_updated: float = 0.0

    @property
    def risk_score(self) -> float:
        if not self.signals:
            return 0.0
        return min(1.0, sum(s.contribution for s in self.signals.values()))

    @property
    def risk_level(self) -> str:
        score = self.risk_score
        for level, (lo, hi) in RISK_THRESHOLDS.items():
            if lo <= score < hi:
                return level
        return "critical"

    @property
    def multiplier(self) -> float:
        return RISK_MULTIPLIERS.get(self.risk_level, 1.0)


class AdaptiveCompliance:
    """
    Monitors device behavior, adjusts compliance limits, and manages recovery.

    Usage:
        ac = get_adaptive_compliance()

        # Record outcomes
        ac.record_outcome("DEVICE01", "follow", success=True)
        ac.record_outcome("DEVICE01", "follow", success=False)

        # Get adjusted limits
        multiplier = ac.get_multiplier("DEVICE01")
        # → 0.7 if risk is medium

        # Check if action should be skipped
        if ac.should_skip("DEVICE01", "follow"):
            print("Too risky, skip follow for now")

        # Recovery mode
        if ac.is_recovering("DEVICE01"):
            print("Device in recovery — passive warmup only")
    """

    def __init__(self):
        self._profiles: Dict[str, DeviceRiskProfile] = {}
        self._lock = threading.Lock()
        self._outcome_window: Dict[str, List[Tuple[float, bool]]] = {}
        self._recovery_state: Dict[str, dict] = {}

    def record_outcome(self, device_id: str, action: str,
                       success: bool = True, error_code: str = ""):
        """Record the result of an action for risk calculation."""
        key = f"{device_id}:{action}"
        now = time.time()

        with self._lock:
            if key not in self._outcome_window:
                self._outcome_window[key] = []

            self._outcome_window[key].append((now, success))

            cutoff = now - 7200
            self._outcome_window[key] = [
                (ts, ok) for ts, ok in self._outcome_window[key] if ts > cutoff
            ]

        self._update_risk(device_id)

        if not success:
            self._check_alert(device_id, action, error_code)

        # Check if recovery should be triggered or progressed
        self._evaluate_recovery(device_id, action, success)

    def get_multiplier(self, device_id: str) -> float:
        """Get the risk-based rate limit multiplier for a device (0.2 - 1.0)."""
        profile = self._get_or_create_profile(device_id)
        return profile.multiplier

    def get_risk_profile(self, device_id: str) -> dict:
        """Full risk profile for dashboard."""
        profile = self._get_or_create_profile(device_id)
        recovery = self._recovery_state.get(device_id, {})
        return {
            "device_id": device_id,
            "risk_score": round(profile.risk_score, 3),
            "risk_level": profile.risk_level,
            "multiplier": profile.multiplier,
            "recovering": bool(recovery.get("active")),
            "recovery": {
                "sessions_completed": recovery.get("sessions", 0),
                "sessions_required": RECOVERY_SESSIONS_REQUIRED,
                "phase_before": recovery.get("phase_before", ""),
                "started_at": recovery.get("started_at", ""),
            } if recovery.get("active") else None,
            "signals": {
                name: {
                    "value": round(s.value, 3),
                    "weight": s.weight,
                    "threshold": s.threshold,
                    "contribution": round(s.contribution, 3),
                }
                for name, s in profile.signals.items()
            },
        }

    def should_skip(self, device_id: str, action: str) -> bool:
        """Check if an action should be skipped due to high risk or recovery mode."""
        # Recovery mode: block all sensitive actions
        if self.is_recovering(device_id):
            if action in RECOVERY_BLOCKED_ACTIONS:
                log.info("[恢复模式] 设备 %s 恢复中, 屏蔽 %s (仅允许被动浏览)",
                         device_id[:12], action)
                return True
            return False

        profile = self._get_or_create_profile(device_id)

        if profile.risk_level == "critical":
            log.warning("[风控] 设备 %s 风险等级=critical, 跳过 %s", device_id, action)
            return True

        sensitive_actions = {"follow", "send_dm", "comment"}
        if action in sensitive_actions and profile.risk_level == "high":
            import random
            if random.random() > profile.multiplier:
                log.info("[风控] 设备 %s 风险高, 概率跳过 %s", device_id, action)
                return True

        return False

    def is_recovering(self, device_id: str) -> bool:
        """Check if a device is currently in recovery mode."""
        state = self._recovery_state.get(device_id, {})
        return bool(state.get("active"))

    def get_recovery_warmup_params(self, device_id: str) -> dict:
        """
        Get warmup parameters for a device in recovery mode.

        Recovery warmup is like cold_start: passive browsing, minimal likes,
        no follows/DMs/comments. Goal is to look like a normal browsing user.
        """
        return {
            "phase": "cold_start",
            "like_probability": 0.05,
            "comment_browse_prob": 0.10,
            "comment_post_prob": 0.0,
            "search_prob": 0.0,
            "duration_minutes": 20,
            "is_recovery": True,
        }

    def record_recovery_session(self, device_id: str):
        """
        Record a successful recovery warmup session.

        After RECOVERY_SESSIONS_REQUIRED clean sessions, exit recovery.
        """
        state = self._recovery_state.get(device_id)
        if not state or not state.get("active"):
            return

        state["sessions"] = state.get("sessions", 0) + 1
        log.info("[恢复模式] 设备 %s 完成恢复养号 %d/%d",
                 device_id[:12], state["sessions"], RECOVERY_SESSIONS_REQUIRED)

        # Decay risk signals after each clean session
        self._decay_risk_signals(device_id, decay_factor=0.3)

        profile = self._get_or_create_profile(device_id)
        if (state["sessions"] >= RECOVERY_SESSIONS_REQUIRED and
                profile.risk_level in ("low", "medium")):
            self._exit_recovery(device_id)

    def force_recovery(self, device_id: str, reason: str = "manual"):
        """Manually trigger recovery mode for a device."""
        self._enter_recovery(device_id, reason)

    def force_exit_recovery(self, device_id: str):
        """Manually exit recovery mode."""
        self._exit_recovery(device_id)

    def get_adjusted_limit(self, device_id: str, base_limit: int) -> int:
        """Apply risk multiplier to a base limit value."""
        mult = self.get_multiplier(device_id)
        return max(1, int(base_limit * mult))

    def get_adjusted_cooldown(self, device_id: str, base_cooldown: float) -> float:
        """Increase cooldown for high-risk devices."""
        mult = self.get_multiplier(device_id)
        if mult >= 1.0:
            return base_cooldown
        return base_cooldown / mult

    def all_profiles(self) -> List[dict]:
        """Get risk profiles for all tracked devices."""
        with self._lock:
            return [self.get_risk_profile(did) for did in self._profiles]

    # ── Recovery Mode ──

    def _evaluate_recovery(self, device_id: str, action: str, success: bool):
        """Decide whether to enter/exit recovery based on current risk."""
        profile = self._get_or_create_profile(device_id)

        # Enter recovery if risk is high/critical and not already recovering
        if (profile.risk_level in ("high", "critical") and
                not self.is_recovering(device_id) and
                action in ("follow", "send_dm")):
            self._enter_recovery(device_id, f"{action}_failures")

    def _enter_recovery(self, device_id: str, reason: str):
        """Enter recovery mode: save current phase, switch to recovery warmup."""
        from datetime import datetime, timezone as tz

        # Save current phase before switching
        phase_before = ""
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            phase_before = ds.get_phase(device_id)
        except Exception:
            pass

        self._recovery_state[device_id] = {
            "active": True,
            "sessions": 0,
            "phase_before": phase_before,
            "reason": reason,
            "started_at": datetime.now(tz.utc).isoformat(),
        }

        # Persist to DeviceStateStore
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            ds.set(device_id, "recovery_active", True)
            ds.set(device_id, "recovery_reason", reason)
            ds.set(device_id, "recovery_phase_before", phase_before)
        except Exception:
            pass

        log.warning("[恢复模式] ⚠ 设备 %s 进入恢复养号 (原因: %s, 原阶段: %s)",
                    device_id[:12], reason, phase_before)
        log.warning("[恢复模式] 策略: 被动浏览(%d 分钟/次) × %d 次, 禁止关注/DM/评论",
                    20, RECOVERY_SESSIONS_REQUIRED)

        # Emit event for monitoring
        try:
            from ..workflow.event_bus import get_event_bus
            bus = get_event_bus()
            bus.emit_simple(
                "device.recovery_entered",
                source="adaptive_compliance",
                device_id=device_id,
                reason=reason,
                phase_before=phase_before,
            )
        except Exception:
            pass

    def _exit_recovery(self, device_id: str):
        """Exit recovery mode: restore previous phase."""
        state = self._recovery_state.get(device_id, {})
        phase_before = state.get("phase_before", "interest_building")
        sessions = state.get("sessions", 0)

        # Restore phase
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            ds.set(device_id, "recovery_active", False)

            # Don't restore to "active" if the account was restricted —
            # go back one step to be safe
            restore_phase = phase_before
            if phase_before == "active":
                restore_phase = "interest_building"
            ds.set_phase(device_id, restore_phase)

            log.info("[恢复模式] ✓ 设备 %s 恢复完成! %d 次养号, 恢复到 %s 阶段",
                     device_id[:12], sessions, restore_phase)
        except Exception:
            pass

        # Clear recovery state
        self._recovery_state.pop(device_id, None)

        try:
            from ..workflow.event_bus import get_event_bus
            bus = get_event_bus()
            bus.emit_simple(
                "device.recovery_completed",
                source="adaptive_compliance",
                device_id=device_id,
                sessions_completed=sessions,
                restored_phase=phase_before,
            )
        except Exception:
            pass

    def _decay_risk_signals(self, device_id: str, decay_factor: float = 0.3):
        """Reduce risk signal values after a clean recovery session."""
        profile = self._get_or_create_profile(device_id)
        for signal in profile.signals.values():
            signal.value *= (1.0 - decay_factor)

    # ── Risk Calculation ──

    def _update_risk(self, device_id: str):
        """Recalculate risk score from outcome data."""
        profile = self._get_or_create_profile(device_id)
        now = time.time()

        # Signal 1: Follow failure rate
        follow_outcomes = self._outcome_window.get(f"{device_id}:follow", [])
        if len(follow_outcomes) >= 3:
            recent = follow_outcomes[-20:]
            failures = sum(1 for _, ok in recent if not ok)
            failure_rate = failures / len(recent)
            profile.signals["follow_failure"] = RiskSignal(
                name="follow_failure", weight=0.35,
                value=failure_rate, threshold=0.3,
            )

        # Signal 2: DM failure rate
        dm_outcomes = self._outcome_window.get(f"{device_id}:send_dm", [])
        if len(dm_outcomes) >= 2:
            recent = dm_outcomes[-10:]
            failures = sum(1 for _, ok in recent if not ok)
            failure_rate = failures / len(recent)
            profile.signals["dm_failure"] = RiskSignal(
                name="dm_failure", weight=0.25,
                value=failure_rate, threshold=0.3,
            )

        # Signal 3: Consecutive errors (any action)
        max_consecutive = 0
        for key, outcomes in self._outcome_window.items():
            if not key.startswith(f"{device_id}:"):
                continue
            consecutive = 0
            for _, ok in reversed(outcomes):
                if not ok:
                    consecutive += 1
                else:
                    break
            max_consecutive = max(max_consecutive, consecutive)

        if max_consecutive > 0:
            profile.signals["consecutive_errors"] = RiskSignal(
                name="consecutive_errors", weight=0.2,
                value=float(max_consecutive), threshold=5.0,
            )

        # Signal 4: Action density (too many actions in short time)
        all_actions = []
        for key, outcomes in self._outcome_window.items():
            if key.startswith(f"{device_id}:"):
                all_actions.extend(outcomes)
        recent_count = sum(1 for ts, _ in all_actions if ts > now - 3600)
        profile.signals["action_density"] = RiskSignal(
            name="action_density", weight=0.1,
            value=float(recent_count), threshold=50.0,
        )

        # Signal 5: Account age factor
        try:
            from ..host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            day = ds.get_device_day(device_id)
            if day is not None and day < 7:
                age_risk = 1.0 - (day / 7.0)
                profile.signals["new_account"] = RiskSignal(
                    name="new_account", weight=0.1,
                    value=age_risk, threshold=1.0,
                )
        except Exception:
            pass

        profile._last_updated = now

        if profile.risk_score > 0.6:
            log.warning("[风控] 设备 %s 风险评分=%.2f (%s), multiplier=%.1f",
                        device_id, profile.risk_score, profile.risk_level,
                        profile.multiplier)

    def _get_or_create_profile(self, device_id: str) -> DeviceRiskProfile:
        with self._lock:
            if device_id not in self._profiles:
                self._profiles[device_id] = DeviceRiskProfile(device_id=device_id)
            return self._profiles[device_id]

    def _check_alert(self, device_id: str, action: str, error_code: str):
        """Emit EventBus alert for repeated failures."""
        profile = self._get_or_create_profile(device_id)
        if profile.risk_level in ("high", "critical"):
            try:
                from ..workflow.event_bus import get_event_bus
                bus = get_event_bus()
                bus.emit_simple(
                    "device.risk_alert",
                    source="adaptive_compliance",
                    device_id=device_id,
                    action=action,
                    risk_score=profile.risk_score,
                    risk_level=profile.risk_level,
                    error_code=error_code,
                    recovering=self.is_recovering(device_id),
                )
            except Exception:
                pass


    # ── A/B Data-Driven Auto-Tuning ──

    def auto_tune_from_experiments(self):
        """
        Analyze A/B experiment outcomes and adjust compliance parameters.

        Looks at experiments tracking follow/DM strategies and adjusts risk
        thresholds based on which patterns cause failures.
        """
        try:
            from ..host.ab_testing import get_ab_store
            ab = get_ab_store()
        except Exception:
            return

        adjustments = {}

        for exp_name in ("dm_template_style", "follow_batch_size",
                         "dm_timing", "warmup_strategy"):
            try:
                analysis = ab.analyze(exp_name)
                if not analysis:
                    continue

                for variant, stats in analysis.items():
                    if not isinstance(stats, dict):
                        continue
                    sent = stats.get("sent", 0)
                    if sent < 10:
                        continue

                    failed = stats.get("failed", 0)
                    restricted = stats.get("restricted", 0)
                    failure_rate = (failed + restricted) / max(sent, 1)

                    if failure_rate > 0.3:
                        adjustments[f"{exp_name}:{variant}"] = {
                            "action": "reduce_aggression",
                            "failure_rate": round(failure_rate, 3),
                            "sample_size": sent,
                        }
                        log.warning(
                            "[自适应反检测] 实验 %s 变体 %s 失败率 %.1f%% (n=%d) — 降低激进度",
                            exp_name, variant, failure_rate * 100, sent)

            except Exception:
                continue

        if adjustments:
            self._apply_tuning_adjustments(adjustments)

        return adjustments

    def _apply_tuning_adjustments(self, adjustments: dict):
        """Apply learned adjustments to compliance parameters."""
        reduce_count = sum(1 for a in adjustments.values()
                           if a.get("action") == "reduce_aggression")

        if reduce_count > 0:
            global RISK_MULTIPLIERS
            penalty = min(0.15, reduce_count * 0.05)

            for level in ("medium", "high"):
                old = RISK_MULTIPLIERS[level]
                new = max(0.1, old - penalty)
                if new != old:
                    RISK_MULTIPLIERS[level] = round(new, 2)
                    log.info("[自适应反检测] %s 风险乘数 %.2f → %.2f",
                             level, old, new)

            try:
                from ..host.device_state import get_device_state_store
                ds = get_device_state_store("tiktok")
                import json as _json
                ds.set("__compliance", "tuning_adjustments",
                       _json.dumps(adjustments))
                ds.set("__compliance", "tuning_timestamp",
                       time.strftime("%Y-%m-%dT%H:%M:%S"))
            except Exception:
                pass

    def get_tuning_status(self) -> dict:
        """Return current tuning state for dashboard."""
        try:
            from ..host.device_state import get_device_state_store
            import json as _json
            ds = get_device_state_store("tiktok")
            raw = ds.get("__compliance", "tuning_adjustments")
            ts = ds.get("__compliance", "tuning_timestamp")
            return {
                "adjustments": _json.loads(raw) if raw else {},
                "last_tuned": ts or "",
                "current_multipliers": dict(RISK_MULTIPLIERS),
            }
        except Exception:
            return {"adjustments": {}, "last_tuned": "",
                    "current_multipliers": dict(RISK_MULTIPLIERS)}


# ── Singleton ──

_instance: Optional[AdaptiveCompliance] = None
_ac_lock = threading.Lock()


# ── 测试支持 (Stage F.2) ──

# 默认值快照 — _apply_tuning_adjustments 会就地 mutate RISK_MULTIPLIERS,
# 跨 test 污染下游 (test_many_failures_trigger_high_risk 期望 multiplier
# 等等). reset_for_tests 用此默认值 deep copy 还原.
_DEFAULT_RISK_MULTIPLIERS = {
    "low": 1.0,
    "medium": 0.7,
    "high": 0.4,
    "critical": 0.2,
}


def reset_for_tests() -> None:
    """仅测试用. 还原 RISK_MULTIPLIERS 到默认值 + 清 singleton.

    根因 (2026-05-04 Stage F.2):
      _apply_tuning_adjustments (line 560) global RISK_MULTIPLIERS 修改
      module-level dict (e.g. high: 0.4 → 0.25 自适应反检测降级). 某测试
      触发后, 下个 TestAdaptiveCompliance test 的 _fresh_ac() 创新 instance,
      但 instance.multiplier property 读 RISK_MULTIPLIERS module-level →
      拿到 mutate 后的值, 假设默认 0.4 的断言 fail.

      conftest P2-⑨ autouse 调本函数, 让 RISK_MULTIPLIERS 永远从默认开始.
    """
    global _instance
    RISK_MULTIPLIERS.clear()
    RISK_MULTIPLIERS.update(_DEFAULT_RISK_MULTIPLIERS)
    with _ac_lock:
        _instance = None


def get_adaptive_compliance() -> AdaptiveCompliance:
    global _instance
    if _instance is None:
        with _ac_lock:
            if _instance is None:
                _instance = AdaptiveCompliance()
    return _instance
