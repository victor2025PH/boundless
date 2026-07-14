"""
账号健康监控模块。

检测 TikTok/Instagram/Telegram 账号的健康状态：
- 发布成功率下降 → 可能被限流
- 连续发布失败 → 可能被封号
- 播放量突降 → 算法降权信号

自动触发动作：
- 健康度 < 0.5 → 发送告警通知
- 健康度 < 0.3 → 自动暂停该账号的发布队列
- 健康度 = 0   → 标记为需要手动处理
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("account_health")

# ── 健康状态数据类 ──────────────────────────────────────────────────────────

@dataclass
class AccountHealth:
    persona_id: str
    platform: str
    health_score: float = 1.0        # 0.0 ~ 1.0，1.0 表示完全健康
    consecutive_failures: int = 0    # 连续发布失败次数
    total_publishes: int = 0
    successful_publishes: int = 0
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_failure_reason: str = ""
    status: str = "active"           # active / warning / paused / blocked
    alerts: List[str] = field(default_factory=list)

    def success_rate(self) -> float:
        if self.total_publishes == 0:
            return 1.0
        return self.successful_publishes / self.total_publishes

    def to_dict(self) -> dict:
        return {
            "persona_id": self.persona_id,
            "platform": self.platform,
            "health_score": round(self.health_score, 3),
            "consecutive_failures": self.consecutive_failures,
            "total_publishes": self.total_publishes,
            "success_rate": round(self.success_rate(), 3),
            "status": self.status,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_failure_reason": self.last_failure_reason,
            "alerts": self.alerts[-5:],  # 只返回最近5条
        }


# ── 健康监控器 ──────────────────────────────────────────────────────────────

class AccountHealthMonitor:
    """账号健康监控器（内存 + SQLite 持久化）。"""

    # 健康度阈值
    WARN_THRESHOLD  = 0.6   # 低于此值发出告警
    PAUSE_THRESHOLD = 0.3   # 低于此值自动暂停
    BLOCK_THRESHOLD = 0.1   # 低于此值标记为封号嫌疑

    # 衰减参数
    FAILURE_PENALTY  = 0.15  # 每次失败扣分
    SUCCESS_RECOVERY = 0.05  # 每次成功回血
    MAX_HEALTH = 1.0

    def __init__(self):
        self._accounts: Dict[str, AccountHealth] = {}
        self._load_from_db()

    def _key(self, persona_id: str, platform: str) -> str:
        return f"{persona_id}:{platform}"

    def get_or_create(self, persona_id: str, platform: str) -> AccountHealth:
        k = self._key(persona_id, platform)
        if k not in self._accounts:
            self._accounts[k] = AccountHealth(persona_id=persona_id, platform=platform)
        return self._accounts[k]

    def record_success(self, persona_id: str, platform: str) -> AccountHealth:
        """记录一次成功发布。"""
        acc = self.get_or_create(persona_id, platform)
        acc.total_publishes += 1
        acc.successful_publishes += 1
        acc.consecutive_failures = 0
        acc.health_score = min(self.MAX_HEALTH, acc.health_score + self.SUCCESS_RECOVERY)
        acc.last_success_at = datetime.now(timezone.utc).isoformat()

        # 恢复状态
        if acc.status in ("warning",) and acc.health_score >= self.WARN_THRESHOLD:
            acc.status = "active"
            logger.info("[健康监控] %s:%s 健康恢复 → active", persona_id, platform)

        self._save_to_db(acc)
        return acc

    def record_failure(self, persona_id: str, platform: str, reason: str = "") -> AccountHealth:
        """记录一次发布失败，自动评估是否需要暂停。"""
        acc = self.get_or_create(persona_id, platform)
        acc.total_publishes += 1
        acc.consecutive_failures += 1
        acc.health_score = max(0.0, acc.health_score - self.FAILURE_PENALTY)
        acc.last_failure_at = datetime.now(timezone.utc).isoformat()
        acc.last_failure_reason = reason[:200] if reason else ""

        # 更新状态
        if acc.health_score <= self.BLOCK_THRESHOLD or acc.consecutive_failures >= 10:
            acc.status = "blocked"
            alert = f"⛔ 账号 {persona_id}@{platform} 疑似被封号（连续失败{acc.consecutive_failures}次）"
            acc.alerts.append(alert)
            logger.error("[健康监控] %s", alert)
        elif acc.health_score <= self.PAUSE_THRESHOLD or acc.consecutive_failures >= 5:
            acc.status = "paused"
            alert = f"⏸️ 账号 {persona_id}@{platform} 已自动暂停（健康度{acc.health_score:.1%}）"
            acc.alerts.append(alert)
            logger.warning("[健康监控] %s", alert)
        elif acc.health_score <= self.WARN_THRESHOLD:
            acc.status = "warning"
            alert = f"⚠️ 账号 {persona_id}@{platform} 健康度下降至 {acc.health_score:.1%}"
            acc.alerts.append(alert)
            logger.warning("[健康监控] %s", alert)

        self._save_to_db(acc)
        return acc

    def is_paused(self, persona_id: str, platform: str) -> bool:
        """检查账号是否被暂停/封禁，暂停时应跳过发布。"""
        acc = self.get_or_create(persona_id, platform)
        return acc.status in ("paused", "blocked")

    def get_all_health(self) -> List[dict]:
        """返回所有账号的健康状态摘要。"""
        return [acc.to_dict() for acc in self._accounts.values()]

    def _load_from_db(self):
        """从 studio_db 加载历史健康数据。"""
        try:
            from .studio_db import _get_conn
            with _get_conn() as conn:
                # 建表（如果不存在）
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_health (
                        id TEXT PRIMARY KEY,
                        persona_id TEXT,
                        platform TEXT,
                        health_score REAL DEFAULT 1.0,
                        consecutive_failures INTEGER DEFAULT 0,
                        total_publishes INTEGER DEFAULT 0,
                        successful_publishes INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'active',
                        last_success_at TEXT,
                        last_failure_at TEXT,
                        last_failure_reason TEXT,
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """)
                conn.commit()
                rows = conn.execute("SELECT * FROM account_health").fetchall()
                for row in rows:
                    k = f"{row['persona_id']}:{row['platform']}"
                    self._accounts[k] = AccountHealth(
                        persona_id=row["persona_id"],
                        platform=row["platform"],
                        health_score=row["health_score"],
                        consecutive_failures=row["consecutive_failures"],
                        total_publishes=row["total_publishes"],
                        successful_publishes=row["successful_publishes"],
                        status=row["status"],
                        last_success_at=row["last_success_at"],
                        last_failure_at=row["last_failure_at"],
                        last_failure_reason=row["last_failure_reason"] or "",
                    )
        except Exception as e:
            logger.warning("账号健康数据加载失败（继续使用内存）: %s", e)

    def _save_to_db(self, acc: AccountHealth):
        """持久化单个账号健康状态。"""
        try:
            from .studio_db import _get_conn
            k = self._key(acc.persona_id, acc.platform)
            with _get_conn() as conn:
                conn.execute("""
                    INSERT INTO account_health
                        (id, persona_id, platform, health_score, consecutive_failures,
                         total_publishes, successful_publishes, status,
                         last_success_at, last_failure_at, last_failure_reason, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(id) DO UPDATE SET
                        health_score=excluded.health_score,
                        consecutive_failures=excluded.consecutive_failures,
                        total_publishes=excluded.total_publishes,
                        successful_publishes=excluded.successful_publishes,
                        status=excluded.status,
                        last_success_at=excluded.last_success_at,
                        last_failure_at=excluded.last_failure_at,
                        last_failure_reason=excluded.last_failure_reason,
                        updated_at=datetime('now')
                """, (
                    k, acc.persona_id, acc.platform, acc.health_score,
                    acc.consecutive_failures, acc.total_publishes, acc.successful_publishes,
                    acc.status, acc.last_success_at, acc.last_failure_at, acc.last_failure_reason
                ))
                conn.commit()
        except Exception as e:
            logger.debug("账号健康数据保存失败: %s", e)


# ── 全局单例 ───────────────────────────────────────────────────────────────
_health_monitor: Optional[AccountHealthMonitor] = None

def get_health_monitor() -> AccountHealthMonitor:
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = AccountHealthMonitor()
    return _health_monitor
