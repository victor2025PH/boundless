"""账号风控事件 24h 滚动计数（反封号反馈闭环的缺失一环）。

背景（补的真洞）
================
``account_health`` 评分**吃** ``flood_waits_24h`` / ``errors_24h``（Telegram/个人号
最强的封号前兆信号），但生产装配 ``build_account_signals`` **从来没喂这两个**；
``ban_signal.handle_send_exception`` 抓到 FloodWait 只退避一次就 return，不累计。
结果：一个号被平台风控狂限一整天，健康分看不见 → ``companion_send_gate`` 的
``recommended_cap`` 不会自动收紧 → 越限越发、越发越限，直到被封。

本模块把这些事件按 ``(platform, account_id, kind)`` 落 **24h 滚动计数**，再由
``build_account_signals`` 读回，闭合「风控压力 → 健康分下降 → 自动降速」的反馈环。
**不新造评分/预算**——只补一个被遗漏的信号源，接回既有 M7 评分。

设计（对齐 ``kill_switch`` / ``SendCountStore``）
------------------------------------------------
- ops/ 内独立 SQLite（``<data_root>/config/account_risk_events.db``），进程单例。
- **全 best-effort，永不抛**：记账失败绝不能掩盖原始发送错误、更不能拖垮发送主链。
- 表恒小：只看 24h，摊还清理 >2 天旧行（留 48h 冗余）。
- 纯依赖注入（db_path 可传 tmp）→ 不依赖真库即可单测。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DAY = 86400.0

#: 记账的事件类别 → 喂给 account_health 的信号字段。
#: 只认这两轴（与 account_health 扣分轴一一对应）；pause/ban 走 kill-switch 不在此。
KIND_FLOOD = "flood"   # FloodWait / SlowmodeWait / PeerFlood（限频家族）
KIND_ERROR = "error"   # 其它发送失败/异常（非我方控制流、非对端注销）

_DDL = """
CREATE TABLE IF NOT EXISTS risk_events (
    account_key TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_events_key_ts
    ON risk_events(account_key, ts);
"""


def account_key(platform: str, account_id: str) -> str:
    """与 ``AutoReplyLimiter`` 同口径的账号键：``<platform>:<account_id>``（小写平台）。"""
    return f"{str(platform or '').lower()}:{account_id}"


def _default_db_path() -> Path:
    """默认落 **可写数据区**（服务进程 = 实例数据根；测试 conftest = tmp）。

    用 ``data_paths.config_dir()`` 而非裸 ``config/``：前者认 ``AITR_DATA_DIR``，
    测试里被 conftest 指向 tmp → 天然隔离，绝不写仓库 config/。取不到再回落裸路径。
    """
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / "account_risk_events.db"
    except Exception:
        return Path("config") / "account_risk_events.db"


class RiskEventStore:
    """按账号的风控事件时间戳持久化（线程安全 SQLite；重启存活）。"""

    def __init__(self, db_path: Any) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._lock = threading.Lock()
        self._writes = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()
            self._prune_locked(time.time() - 2 * _DAY)

    def record(self, key: str, kind: str, ts: Optional[float] = None) -> None:
        """记一条风控事件（best-effort；摊还清理旧行）。"""
        ts = float(ts if ts is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO risk_events (account_key, kind, ts) VALUES (?,?,?)",
                (str(key), str(kind), ts),
            )
            self._conn.commit()
            self._writes += 1
            if self._writes % 50 == 0:  # 摊还清理，热路径零额外开销
                self._prune_locked(time.time() - 2 * _DAY)

    def counts_since(self, key: str, since_ts: float) -> Dict[str, int]:
        """``since_ts`` 起各类别计数：``{kind: n}``（无事件 → 空 dict）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) FROM risk_events "
                "WHERE account_key=? AND ts>=? GROUP BY kind",
                (str(key), float(since_ts)),
            ).fetchall()
        return {str(k): int(n or 0) for (k, n) in rows}

    def _prune_locked(self, before_ts: float) -> None:
        try:
            self._conn.execute(
                "DELETE FROM risk_events WHERE ts<?", (float(before_ts),))
            self._conn.commit()
        except Exception:
            logger.debug("[risk-events] prune 失败（忽略）", exc_info=True)


_singleton: Optional[RiskEventStore] = None
_singleton_lock = threading.Lock()


def get_risk_event_store(db_path: Optional[Any] = None) -> RiskEventStore:
    """进程内单例。首次调用可指定路径，默认 ``<data_root>/config/account_risk_events.db``。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = RiskEventStore(db_path or _default_db_path())
    return _singleton


def record_risk_event(
    platform: str, account_id: str, kind: str, *, now: Optional[float] = None
) -> None:
    """模块级便捷入口（发送异常处置路径调用）：单例懒建，**永不抛**。"""
    try:
        get_risk_event_store().record(
            account_key(platform, account_id), kind, now)
    except Exception:
        logger.debug("[risk-events] record 失败（忽略）", exc_info=True)


def risk_counts_24h(
    platform: str, account_id: str, *, now: Optional[float] = None
) -> Dict[str, int]:
    """近 24h 各类别计数（供 ``build_account_signals`` 装配健康信号）；**永不抛**。

    单例未建 / IO 异常 → 返回空 dict（缺数据视为良性，健康分不误伤）。
    """
    store = _singleton
    if store is None:
        # 读侧懒建：让「记录了却读不到」不发生（记录侧已懒建同一单例）
        try:
            store = get_risk_event_store()
        except Exception:
            return {}
    now = float(now if now is not None else time.time())
    try:
        return store.counts_since(account_key(platform, account_id), now - _DAY)
    except Exception:
        return {}


__all__ = [
    "RiskEventStore", "get_risk_event_store",
    "record_risk_event", "risk_counts_24h", "account_key",
    "KIND_FLOOD", "KIND_ERROR",
]
