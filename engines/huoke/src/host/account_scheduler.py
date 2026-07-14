# -*- coding: utf-8 -*-
"""
多账号调度引擎 — 单设备多 TikTok 账号智能轮换。

调度策略 (三合一):
  1. 每日预算: 每个账号每天最多 N 次会话、M 分钟在线
  2. 冷却计时: 同一账号两次使用之间至少休息 K 分钟
  3. 风控感知: 如果 adaptive_compliance 检测到高风险，自动跳过或降级

选择算法:
  score = freshness * 0.4 + budget_remaining * 0.3 + safety * 0.3
  - freshness: 距上次使用越久分越高
  - budget_remaining: 今日剩余预算比例
  - safety: 风险越低分越高
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_DEFAULT_DAILY_SESSIONS = 4
_DEFAULT_DAILY_MINUTES = 120
_DEFAULT_COOLDOWN_MINUTES = 90
_WEIGHT_FRESHNESS = 0.40
_WEIGHT_BUDGET = 0.30
_WEIGHT_SAFETY = 0.30


@dataclass
class AccountConfig:
    """Per-account scheduling configuration."""
    username: str
    daily_sessions: int = _DEFAULT_DAILY_SESSIONS
    daily_minutes: int = _DEFAULT_DAILY_MINUTES
    cooldown_minutes: int = _DEFAULT_COOLDOWN_MINUTES
    enabled: bool = True
    priority: int = 0  # higher = more preferred


@dataclass
class AccountUsage:
    """Runtime tracking of account usage for the current day."""
    sessions_today: int = 0
    minutes_today: float = 0.0
    last_session_end: float = 0.0
    last_session_start: float = 0.0
    day_key: str = ""

    def reset_if_new_day(self):
        today = time.strftime("%Y-%m-%d")
        if self.day_key != today:
            self.sessions_today = 0
            self.minutes_today = 0.0
            self.day_key = today


_PERSISTENCE_FILE = None

def _get_persistence_path():
    global _PERSISTENCE_FILE
    if _PERSISTENCE_FILE is None:
        from src.host.device_registry import data_file

        _PERSISTENCE_FILE = data_file("account_schedule.json")
        _PERSISTENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    return _PERSISTENCE_FILE


class AccountScheduler:
    """Manages account rotation for devices with multiple TikTok accounts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._configs: Dict[str, Dict[str, AccountConfig]] = {}
        self._usage: Dict[str, Dict[str, AccountUsage]] = defaultdict(
            lambda: defaultdict(AccountUsage))
        self._load_state()

    def register_accounts(self, device_id: str,
                          accounts: List[AccountConfig]):
        """Register the available accounts on a device."""
        with self._lock:
            self._configs[device_id] = {a.username: a for a in accounts}
        log.info("[AccountScheduler] 设备 %s 注册 %d 个账号: %s",
                 device_id[:8], len(accounts),
                 [a.username for a in accounts])

    def register_account(self, device_id: str, username: str, **kwargs):
        """Register or update a single account."""
        with self._lock:
            if device_id not in self._configs:
                self._configs[device_id] = {}
            cfg = AccountConfig(username=username, **kwargs)
            self._configs[device_id][username] = cfg

    def remove_account(self, device_id: str, username: str):
        with self._lock:
            if device_id in self._configs:
                self._configs[device_id].pop(username, None)

    def get_device_accounts(self, device_id: str) -> List[str]:
        with self._lock:
            return list(self._configs.get(device_id, {}).keys())

    def select_account(self, device_id: str,
                       task_type: str = "") -> Optional[str]:
        """
        Select the best account for the next task on this device.
        Returns username or None if no accounts are configured/available.
        """
        with self._lock:
            configs = self._configs.get(device_id, {})
            if not configs:
                return None

            usages = self._usage[device_id]
            now = time.time()
            scores = {}

            for username, cfg in configs.items():
                if not cfg.enabled:
                    continue

                usage = usages[username]
                usage.reset_if_new_day()

                if usage.sessions_today >= cfg.daily_sessions:
                    log.debug("[调度] %s@%s: 今日会话已满 %d/%d",
                              username, device_id[:8],
                              usage.sessions_today, cfg.daily_sessions)
                    continue

                if usage.minutes_today >= cfg.daily_minutes:
                    log.debug("[调度] %s@%s: 今日时间已满 %.0f/%.0f",
                              username, device_id[:8],
                              usage.minutes_today, cfg.daily_minutes)
                    continue

                cooldown_sec = cfg.cooldown_minutes * 60
                if usage.last_session_end > 0:
                    elapsed = now - usage.last_session_end
                    if elapsed < cooldown_sec:
                        remaining = int((cooldown_sec - elapsed) / 60)
                        log.debug("[调度] %s@%s: 冷却中，还需 %d 分钟",
                                  username, device_id[:8], remaining)
                        continue

                freshness = self._freshness_score(usage, now)
                budget = self._budget_score(usage, cfg)
                safety = self._safety_score(device_id, username)

                total = (freshness * _WEIGHT_FRESHNESS
                         + budget * _WEIGHT_BUDGET
                         + safety * _WEIGHT_SAFETY
                         + cfg.priority * 5)

                scores[username] = {
                    "total": round(total, 1),
                    "freshness": freshness,
                    "budget": budget,
                    "safety": safety,
                }

            if not scores:
                log.info("[调度] 设备 %s 无可用账号 (全部冷却/超额/禁用)",
                         device_id[:8])
                return None

            best = max(scores, key=lambda u: scores[u]["total"])
            log.info("[调度] 设备 %s 选择账号 @%s (%.1f) 候选=%d",
                     device_id[:8], best, scores[best]["total"],
                     len(scores))
            return best

    def start_session(self, device_id: str, username: str):
        """Record start of a session for an account."""
        with self._lock:
            usage = self._usage[device_id][username]
            usage.reset_if_new_day()
            usage.sessions_today += 1
            usage.last_session_start = time.time()
        self._save_state()

    def end_session(self, device_id: str, username: str,
                    duration_minutes: float = 0):
        """Record end of a session, update usage stats."""
        with self._lock:
            usage = self._usage[device_id][username]
            usage.last_session_end = time.time()
            if duration_minutes > 0:
                usage.minutes_today += duration_minutes
            elif usage.last_session_start > 0:
                elapsed = (time.time() - usage.last_session_start) / 60
                usage.minutes_today += elapsed
        self._save_state()

    def get_schedule_status(self, device_id: str) -> Dict[str, dict]:
        """Return scheduling status for all accounts on a device."""
        with self._lock:
            configs = self._configs.get(device_id, {})
            usages = self._usage.get(device_id, {})
            now = time.time()
            result = {}

            for username, cfg in configs.items():
                usage = usages.get(username, AccountUsage())
                usage.reset_if_new_day()

                cooldown_remaining = 0
                if usage.last_session_end > 0:
                    elapsed = now - usage.last_session_end
                    cooldown_sec = cfg.cooldown_minutes * 60
                    if elapsed < cooldown_sec:
                        cooldown_remaining = int(cooldown_sec - elapsed)

                result[username] = {
                    "enabled": cfg.enabled,
                    "sessions_today": usage.sessions_today,
                    "daily_sessions_max": cfg.daily_sessions,
                    "minutes_today": round(usage.minutes_today, 1),
                    "daily_minutes_max": cfg.daily_minutes,
                    "cooldown_remaining_sec": cooldown_remaining,
                    "available": (cfg.enabled
                                  and usage.sessions_today < cfg.daily_sessions
                                  and usage.minutes_today < cfg.daily_minutes
                                  and cooldown_remaining == 0),
                    "priority": cfg.priority,
                }
            return result

    def get_all_schedules(self) -> Dict[str, Dict[str, dict]]:
        """Return scheduling status for all registered devices."""
        with self._lock:
            devices = list(self._configs.keys())
        return {did: self.get_schedule_status(did) for did in devices}

    def _freshness_score(self, usage: AccountUsage, now: float) -> int:
        """Score 0-100: higher when account hasn't been used recently."""
        if usage.last_session_end <= 0:
            return 100
        hours_since = (now - usage.last_session_end) / 3600
        if hours_since >= 6:
            return 100
        if hours_since >= 3:
            return 80
        if hours_since >= 1.5:
            return 60
        return 30

    def _budget_score(self, usage: AccountUsage,
                      cfg: AccountConfig) -> int:
        """Score 0-100: higher when more budget remains."""
        session_ratio = 1 - (usage.sessions_today / max(cfg.daily_sessions, 1))
        time_ratio = 1 - (usage.minutes_today / max(cfg.daily_minutes, 1))
        return int(min(session_ratio, time_ratio) * 100)

    def _safety_score(self, device_id: str, username: str) -> int:
        """Score 0-100: combines risk level and profile learning progress."""
        base = 70
        try:
            from src.behavior.adaptive_compliance import get_adaptive_compliance
            from src.host.device_state import DeviceStateStore
            ac = get_adaptive_compliance()
            cid = DeviceStateStore.account_device_id(device_id, username)
            profile = ac.get_risk_profile(cid)
            risk = profile.get("risk_score", 0)
            if profile.get("recovering"):
                return 20
            base = max(10, int((1 - risk) * 100))
        except Exception:
            pass

        try:
            from src.behavior.account_profile import get_profile_manager
            pm = get_profile_manager()
            summary = pm.get_summary(device_id, username)
            if summary and summary.get("sessions", 0) >= 2:
                algo = summary.get("algo_score", 0)
                base = int(base * 0.7 + algo * 100 * 0.3)
        except Exception:
            pass

        return max(10, min(100, base))

    def _save_state(self):
        """Persist usage data to disk."""
        try:
            import json
            path = _get_persistence_path()
            data = {"configs": {}, "usage": {}}
            for did, accts in self._configs.items():
                data["configs"][did] = {
                    u: {"daily_sessions": c.daily_sessions,
                        "daily_minutes": c.daily_minutes,
                        "cooldown_minutes": c.cooldown_minutes,
                        "enabled": c.enabled, "priority": c.priority}
                    for u, c in accts.items()
                }
            for did, accts in self._usage.items():
                data["usage"][did] = {}
                for u, usage in accts.items():
                    data["usage"][did][u] = {
                        "sessions_today": usage.sessions_today,
                        "minutes_today": usage.minutes_today,
                        "last_session_end": usage.last_session_end,
                        "day_key": usage.day_key,
                    }
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("[AccountScheduler] 持久化失败: %s", e)

    def _load_state(self):
        """Load persisted state from disk."""
        try:
            import json
            path = _get_persistence_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            for did, accts in data.get("configs", {}).items():
                self._configs[did] = {
                    u: AccountConfig(username=u, **cfg)
                    for u, cfg in accts.items()
                }
            for did, accts in data.get("usage", {}).items():
                for u, udata in accts.items():
                    usage = AccountUsage()
                    usage.sessions_today = udata.get("sessions_today", 0)
                    usage.minutes_today = udata.get("minutes_today", 0)
                    usage.last_session_end = udata.get("last_session_end", 0)
                    usage.day_key = udata.get("day_key", "")
                    usage.reset_if_new_day()
                    self._usage[did][u] = usage
            log.info("[AccountScheduler] 已加载 %d 设备的调度状态",
                     len(self._configs))
        except Exception as e:
            log.debug("[AccountScheduler] 加载状态失败: %s", e)

    def auto_discover_accounts(self, device_id: str,
                               manager=None) -> List[str]:
        """Discover accounts from DeviceStateStore and register them."""
        try:
            from src.host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            accounts = ds.list_accounts(device_id)
            if accounts:
                for acct in accounts:
                    if acct not in self._configs.get(device_id, {}):
                        self.register_account(device_id, acct)
                log.info("[调度] 自动发现 %s 的 %d 个账号: %s",
                         device_id[:8], len(accounts), accounts)
            return accounts
        except Exception:
            return []


_scheduler: Optional[AccountScheduler] = None
_scheduler_lock = threading.Lock()


def get_account_scheduler() -> AccountScheduler:
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = AccountScheduler()
    return _scheduler
