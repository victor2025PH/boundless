"""
Compliance Guard — platform-aware rate limiter with sliding windows.

Tracks actions per (platform, action, account) across both hourly and daily
windows.  Persists to SQLite so quota state survives restarts.

Design decisions:
- Dual sliding windows (hourly + daily) — LinkedIn detects bursts even within
  daily limits, so hourly caps are essential.
- Per-account tracking — multi-account setups need independent quotas.
- Cooldown periods — minimum gap between consecutive same-type actions.
- QuotaExceeded is a soft signal, not a crash — callers can switch accounts.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from src.host.device_registry import data_file

log = logging.getLogger(__name__)


def _facebook_compliance_relaxed() -> bool:
    """真机/脚本压测时跳过 Facebook 的 hourly/daily/cooldown/平台总限额检查（仍会 record）。

    设置环境变量（任一为真即可）::
        MOBILE_AUTO_FB_COMPLIANCE_RELAXED=1
        MOBILE_AUTO_COMPLIANCE_RELAXED=1   # 全局宽松（含 FB）
    生产环境勿开启。
    """
    for key in ("MOBILE_AUTO_FB_COMPLIANCE_RELAXED", "MOBILE_AUTO_COMPLIANCE_RELAXED"):
        v = (os.environ.get(key) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
    return False


class QuotaExceeded(Exception):
    """Raised when an action would violate platform rate limits."""
    def __init__(self, platform: str, action: str, account: str,
                 window: str, current: int, limit: int):
        self.platform = platform
        self.action = action
        self.account = account
        self.window = window
        self.current = current
        self.limit = limit
        super().__init__(
            f"{platform}/{action} quota exceeded for [{account}]: "
            f"{current}/{limit} in {window} window"
        )


# ---------------------------------------------------------------------------
# Limit definitions
# ---------------------------------------------------------------------------

@dataclass
class ActionLimit:
    hourly: int = 999
    daily: int = 999
    cooldown_sec: float = 0.0

@dataclass
class PlatformLimits:
    actions: Dict[str, ActionLimit] = field(default_factory=dict)
    daily_total: int = 9999
    hourly_total: int = 9999


DEFAULT_LIMITS: Dict[str, PlatformLimits] = {
    "telegram": PlatformLimits(
        actions={
            "send_message":    ActionLimit(hourly=25, daily=150, cooldown_sec=3),
            "search_user":     ActionLimit(hourly=20, daily=100, cooldown_sec=5),
            "send_file":       ActionLimit(hourly=10, daily=50,  cooldown_sec=8),
            "forward_message": ActionLimit(hourly=15, daily=80,  cooldown_sec=4),
            "join_group":      ActionLimit(hourly=5,  daily=15,  cooldown_sec=60),
            "switch_account":  ActionLimit(hourly=20, daily=100, cooldown_sec=2),
        },
        daily_total=500,
        hourly_total=60,
    ),
    "linkedin": PlatformLimits(
        actions={
            "send_message":       ActionLimit(hourly=8,  daily=30,  cooldown_sec=30),
            "send_connection":    ActionLimit(hourly=6,  daily=25,  cooldown_sec=45),
            "search_profile":     ActionLimit(hourly=15, daily=60,  cooldown_sec=10),
            "view_profile":       ActionLimit(hourly=15, daily=80,  cooldown_sec=8),
            "like_post":          ActionLimit(hourly=10, daily=40,  cooldown_sec=15),
            "comment_post":       ActionLimit(hourly=5,  daily=20,  cooldown_sec=45),
            "endorse_skill":      ActionLimit(hourly=5,  daily=15,  cooldown_sec=30),
            "post_update":        ActionLimit(hourly=3,  daily=5,   cooldown_sec=120),
            "accept_connections": ActionLimit(hourly=15, daily=50,  cooldown_sec=5),
        },
        daily_total=100,
        hourly_total=25,
    ),
    "whatsapp": PlatformLimits(
        actions={
            "send_message":       ActionLimit(hourly=18, daily=80,  cooldown_sec=5),
            "send_media":         ActionLimit(hourly=8,  daily=30,  cooldown_sec=12),
            "send_group_message": ActionLimit(hourly=10, daily=40,  cooldown_sec=10),
            "search_contact":     ActionLimit(hourly=15, daily=60,  cooldown_sec=5),
            "post_status":        ActionLimit(hourly=3,  daily=10,  cooldown_sec=60),
        },
        daily_total=200,
        hourly_total=35,
    ),
    # ── New platforms ──────────────────────────────────────────────────
    # NOTE: limits are per-account. When account="" (empty), all 28 devices
    # share one counter, so totals must accommodate fleet size.
    "facebook": PlatformLimits(
        actions={
            "send_message":    ActionLimit(hourly=15, daily=60,  cooldown_sec=10),
            "add_friend":      ActionLimit(hourly=10, daily=50,  cooldown_sec=20),
            "add_friend_with_note": ActionLimit(hourly=10, daily=50, cooldown_sec=20),
            "like_post":       ActionLimit(hourly=60, daily=300, cooldown_sec=5),
            "comment":         ActionLimit(hourly=20, daily=100, cooldown_sec=15),
            "search":          ActionLimit(hourly=60, daily=400, cooldown_sec=3),
            "join_group":      ActionLimit(hourly=10, daily=40,  cooldown_sec=30),
            "browse_feed":     ActionLimit(hourly=90, daily=600, cooldown_sec=2),
            "browse_groups":   ActionLimit(hourly=20, daily=80,  cooldown_sec=5),
            "group_engage":    ActionLimit(hourly=30, daily=120, cooldown_sec=5),
            "extract_members": ActionLimit(hourly=20, daily=80,  cooldown_sec=5),
            "share_post":      ActionLimit(hourly=10, daily=40,  cooldown_sec=20),
            "view_profile":    ActionLimit(hourly=90, daily=500, cooldown_sec=2),
        },
        daily_total=4000,
        hourly_total=800,
    ),
    "instagram": PlatformLimits(
        actions={
            "follow":          ActionLimit(hourly=10, daily=60,  cooldown_sec=30),
            "unfollow":        ActionLimit(hourly=8,  daily=50,  cooldown_sec=30),
            "like":            ActionLimit(hourly=20, daily=100, cooldown_sec=10),
            "comment":         ActionLimit(hourly=8,  daily=30,  cooldown_sec=45),
            "send_dm":         ActionLimit(hourly=20, daily=80,  cooldown_sec=30),
            "search":          ActionLimit(hourly=15, daily=80,  cooldown_sec=8),
            "browse_feed":     ActionLimit(hourly=30, daily=200, cooldown_sec=3),
            "view_story":      ActionLimit(hourly=20, daily=150, cooldown_sec=5),
            "browse_hashtag":  ActionLimit(hourly=10, daily=50,  cooldown_sec=10),
        },
        daily_total=350,
        hourly_total=60,
    ),
    "tiktok": PlatformLimits(
        actions={
            "browse_feed":     ActionLimit(hourly=60, daily=300, cooldown_sec=0),
            "like":            ActionLimit(hourly=15, daily=80,  cooldown_sec=3),
            "comment":         ActionLimit(hourly=6,  daily=30,  cooldown_sec=30),
            "follow":          ActionLimit(hourly=8,  daily=30,  cooldown_sec=15),
            "unfollow":        ActionLimit(hourly=10, daily=60,  cooldown_sec=10),
            "send_dm":         ActionLimit(hourly=5,  daily=20,  cooldown_sec=60),
            "search":          ActionLimit(hourly=10, daily=50,  cooldown_sec=5),
            "favorite":        ActionLimit(hourly=10, daily=50,  cooldown_sec=5),
            "share":           ActionLimit(hourly=5,  daily=20,  cooldown_sec=15),
            "view_profile":    ActionLimit(hourly=15, daily=80,  cooldown_sec=5),
        },
        daily_total=350,
        hourly_total=50,
    ),
    "twitter": PlatformLimits(
        actions={
            "follow":          ActionLimit(hourly=10, daily=50,  cooldown_sec=25),
            "like":            ActionLimit(hourly=20, daily=100, cooldown_sec=8),
            "retweet":         ActionLimit(hourly=8,  daily=40,  cooldown_sec=20),
            "reply":           ActionLimit(hourly=10, daily=50,  cooldown_sec=25),
            "send_dm":         ActionLimit(hourly=8,  daily=40,  cooldown_sec=30),
            "search":          ActionLimit(hourly=15, daily=80,  cooldown_sec=8),
            "browse_feed":     ActionLimit(hourly=30, daily=200, cooldown_sec=3),
            "post_tweet":      ActionLimit(hourly=5,  daily=25,  cooldown_sec=60),
        },
        daily_total=350,
        hourly_total=55,
    ),
}


# ---------------------------------------------------------------------------
# ComplianceGuard
# ---------------------------------------------------------------------------

class ComplianceGuard:
    """
    Thread-safe, SQLite-backed rate limiter.

    Usage:
        guard = ComplianceGuard()
        guard.check("linkedin", "send_connection", "user@example.com")  # raises QuotaExceeded
        # ... perform action ...
        guard.record("linkedin", "send_connection", "user@example.com", device_id="...")
    """

    def __init__(self,
                 db_path: Optional[str] = None,
                 limits: Optional[Dict[str, PlatformLimits]] = None):
        self._db_path = db_path or str(data_file("compliance.db"))
        self._limits = limits or DEFAULT_LIMITS
        self._lock = threading.Lock()
        self._last_action: Dict[Tuple[str, str, str], float] = {}
        self._init_db()

    # -- Database -----------------------------------------------------------

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform  TEXT    NOT NULL,
                    action    TEXT    NOT NULL,
                    account   TEXT    NOT NULL DEFAULT '',
                    device_id TEXT    NOT NULL DEFAULT '',
                    ts        REAL    NOT NULL,
                    metadata  TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_action_log_lookup
                ON action_log (platform, action, account, ts)
            """)
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- Core API -----------------------------------------------------------

    def check(self, platform: str, action: str, account: str = "") -> bool:
        """
        Check if action is allowed.  Raises QuotaExceeded if not.
        Returns True if safe to proceed.
        """
        with self._lock:
            self._check_cooldown(platform, action, account)
            self._check_action_limits(platform, action, account)
            self._check_platform_totals(platform, account)
        return True

    def record(self, platform: str, action: str, account: str = "",
               device_id: str = "", metadata: Optional[str] = None):
        """Record a completed action."""
        now = time.time()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO action_log (platform, action, account, device_id, ts, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (platform, action, account, device_id, now, metadata),
                )
            self._last_action[(platform, action, account)] = now
        log.debug("ComplianceGuard recorded: %s/%s [%s]", platform, action, account)

    def check_and_record(self, platform: str, action: str, account: str = "",
                         device_id: str = "", metadata: Optional[str] = None):
        """Atomic check + record under single lock. Raises QuotaExceeded if not allowed."""
        with self._lock:
            self._check_cooldown(platform, action, account)
            self._check_action_limits(platform, action, account)
            self._check_platform_totals(platform, account)
            # Record immediately while still holding the lock
            now = time.time()
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO action_log (platform, action, account, device_id, ts, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (platform, action, account, device_id or "", now, metadata),
                )
            self._last_action[(platform, action, account)] = now
        log.debug("ComplianceGuard check_and_record OK: %s/%s [%s]", platform, action, account)

    # -- Query API ----------------------------------------------------------

    def get_remaining(self, platform: str, action: str, account: str = "") -> Dict[str, int]:
        """Return remaining quota for both windows."""
        limits = self._get_action_limit(platform, action)
        now = time.time()
        with self._conn() as conn:
            hourly = self._count(conn, platform, action, account, now - 3600)
            daily = self._count(conn, platform, action, account, now - 86400)
        return {
            "hourly_remaining": max(0, limits.hourly - hourly),
            "daily_remaining": max(0, limits.daily - daily),
            "hourly_used": hourly,
            "daily_used": daily,
        }

    def get_next_slot_eta(self, platform: str, action: str,
                          account: str = "") -> float:
        """返回下一次能派任务的 ETA（秒）, 0 = 现在就能派.

        sliding window 语义: hourly limit 满 = window 内已有 N 条 log,
        最老的那条 ts 在 (ts + 3600) 时滑出 window, 那时即可再派一次.
        如果 hourly + daily 都满, 取较大的 ETA (更晚 unblock).

        2026-04-27 P4: dashboard 给用户显示 "X 分钟后可派下一个" 用,
        以及 _execute_facebook catch QuotaExceeded 时做友好 message.
        """
        limits = self._get_action_limit(platform, action)
        now = time.time()
        eta = 0.0
        with self._conn() as conn:
            for window_sec, cap in (
                (3600, limits.hourly),
                (86400, limits.daily),
            ):
                if cap <= 0:
                    continue
                cnt = self._count(conn, platform, action, account, now - window_sec)
                if cnt < cap:
                    continue
                # window 满, 找最老 log 的 ts
                row = conn.execute(
                    "SELECT MIN(ts) FROM action_log "
                    "WHERE platform=? AND action=? AND account=? AND ts >= ?",
                    (platform, action, account, now - window_sec),
                ).fetchone()
                if row and row[0]:
                    window_eta = max(0.0, row[0] + window_sec - now)
                    eta = max(eta, window_eta)
        return eta

    def get_cooldown(self, platform: str, action: str, account: str = "") -> float:
        """Seconds until cooldown expires.  0 = ready now."""
        limits = self._get_action_limit(platform, action)
        key = (platform, action, account)
        last = self._last_action.get(key)
        if last is None:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT MAX(ts) FROM action_log WHERE platform=? AND action=? AND account=?",
                    (platform, action, account),
                ).fetchone()
                last = row[0] if row and row[0] else 0
                self._last_action[key] = last

        elapsed = time.time() - last
        remaining = limits.cooldown_sec - elapsed
        return max(0.0, remaining)

    def get_platform_status(self, platform: str, account: str = "") -> dict:
        """Dashboard-friendly summary for a platform+account."""
        plimits = self._limits.get(platform)
        if not plimits:
            return {"error": f"unknown platform: {platform}"}

        now = time.time()
        result = {"platform": platform, "account": account, "actions": {}}
        with self._conn() as conn:
            for action_name in plimits.actions:
                al = plimits.actions[action_name]
                hourly = self._count(conn, platform, action_name, account, now - 3600)
                daily = self._count(conn, platform, action_name, account, now - 86400)
                result["actions"][action_name] = {
                    "hourly": f"{hourly}/{al.hourly}",
                    "daily": f"{daily}/{al.daily}",
                    "cooldown_sec": round(self.get_cooldown(platform, action_name, account), 1),
                }

            total_hourly = self._count_platform(conn, platform, account, now - 3600)
            total_daily = self._count_platform(conn, platform, account, now - 86400)
            result["totals"] = {
                "hourly": f"{total_hourly}/{plimits.hourly_total}",
                "daily": f"{total_daily}/{plimits.daily_total}",
            }
        return result

    def cleanup_old(self, days: int = 7):
        """Purge records older than N days (inclusive boundary).

        用 `ts <= cutoff` 而非 `<`: Windows `time.time()` 精度约 15.6ms, 刚插的
        record 和随后 cleanup 的 `cutoff = time.time() - days*86400` 可能落在
        同一 tick 值相等, `<` 会漏删; 改 `<=` 让 days=0 "清全部" 语义可靠。
        对 days>0 只影响边界 1 microsecond, 无实质差异。
        """
        cutoff = time.time() - days * 86400
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM action_log WHERE ts <= ?", (cutoff,))
            log.info("ComplianceGuard cleanup: removed %d old records", cur.rowcount)

    # -- Internal -----------------------------------------------------------

    def _get_action_limit(self, platform: str, action: str) -> ActionLimit:
        plimits = self._limits.get(platform)
        if not plimits:
            return ActionLimit()
        return plimits.actions.get(action, ActionLimit())

    def _check_cooldown(self, platform: str, action: str, account: str):
        if platform == "facebook" and _facebook_compliance_relaxed():
            return
        remaining = self.get_cooldown(platform, action, account)
        if remaining > 0:
            log.debug("Cooldown active for %s/%s [%s]: %.1fs remaining — waiting",
                      platform, action, account, remaining)
            time.sleep(remaining)

    def _check_action_limits(self, platform: str, action: str, account: str):
        if platform == "facebook" and _facebook_compliance_relaxed():
            log.debug("ComplianceGuard: Facebook action limits skipped (%s)", action)
            return
        limits = self._get_action_limit(platform, action)
        now = time.time()
        with self._conn() as conn:
            hourly = self._count(conn, platform, action, account, now - 3600)
            if hourly >= limits.hourly:
                raise QuotaExceeded(platform, action, account, "hourly", hourly, limits.hourly)

            daily = self._count(conn, platform, action, account, now - 86400)
            if daily >= limits.daily:
                raise QuotaExceeded(platform, action, account, "daily", daily, limits.daily)

    def _check_platform_totals(self, platform: str, account: str):
        if platform == "facebook" and _facebook_compliance_relaxed():
            log.debug("ComplianceGuard: Facebook platform totals skipped")
            return
        plimits = self._limits.get(platform)
        if not plimits:
            return
        now = time.time()
        with self._conn() as conn:
            hourly = self._count_platform(conn, platform, account, now - 3600)
            if hourly >= plimits.hourly_total:
                raise QuotaExceeded(platform, "*", account, "hourly_total",
                                    hourly, plimits.hourly_total)
            daily = self._count_platform(conn, platform, account, now - 86400)
            if daily >= plimits.daily_total:
                raise QuotaExceeded(platform, "*", account, "daily_total",
                                    daily, plimits.daily_total)

    @staticmethod
    def _count(conn, platform: str, action: str, account: str, since: float) -> int:
        if account:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_log "
                "WHERE platform=? AND action=? AND account=? AND ts>=?",
                (platform, action, account, since),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE platform=? AND action=? AND ts>=?",
                (platform, action, since),
            ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _count_platform(conn, platform: str, account: str, since: float) -> int:
        if account:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE platform=? AND account=? AND ts>=?",
                (platform, account, since),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM action_log WHERE platform=? AND ts>=?",
                (platform, since),
            ).fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_guard: Optional[ComplianceGuard] = None
_guard_lock = threading.Lock()


def get_compliance_guard(db_path: Optional[str] = None) -> ComplianceGuard:
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                _guard = ComplianceGuard(db_path=db_path)
    return _guard
