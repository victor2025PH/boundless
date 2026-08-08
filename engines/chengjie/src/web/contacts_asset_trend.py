"""客户资产（好友名单盘点）「按日落库」时序持久化（SQLite）。

背景与定位
----------
ops「客户资产」卡的 总好友/未开口/沉默 是**点值**——运营看板回答不了「破冰功能上线
一周，未开口存量压下去没有」这类趋势问题（2026-08-02 破冰闭环上线后的验收刚需）。
本模块把每账号的盘点数字按 (日, 平台, 账号) upsert 落地，看板据此画环比。

设计（对齐 ui_event_trend / frontend_error_trend / csrf_trend 家族）：
- **写侧＝读时懒快照**（见 ops_overview_routes 的 trend 端点）：打开 ops 看板本身就是
  每日节奏，无需看门狗/计划任务；当天已有快照则跳过。刻意不挂 health_watchdog——
  既避免常驻 job，也避开该文件上的并行施工线。
- **同日重快照＝覆盖**（取当日最新值，非累加——这是 gauge 不是 counter，与
  ui_event_trend 的增量语义相反，勿抄错）。
- **默认关**：未 ``configure_contacts_asset_trend(enabled=True, ...)`` → snap 恒 no-op。
- 只存 (日期, 平台, 账号id, 三个整数)。账号 id 本就进程内可见（registry 口径），
  不落任何客户侧内容。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS contacts_asset_daily (
    day         TEXT NOT NULL,
    platform    TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    never_spoke INTEGER NOT NULL DEFAULT 0,
    silent      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, platform, account_id)
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与其他 trend store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class ContactsAssetTrendStore:
    """客户资产按 (日, 平台, 账号) 快照（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def snap(self, platform: str, account_id: str, *, total: int,
             never_spoke: int, silent: int, now: Optional[float] = None) -> None:
        """写入/覆盖当日该账号的盘点快照（gauge 语义：同日重写取最新）。绝不抛。"""
        plat = str(platform or "").lower()
        acct = str(account_id or "")
        if not plat or not acct:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO contacts_asset_daily "
                    "  (day, platform, account_id, total, never_spoke, silent) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(day, platform, account_id) DO UPDATE SET "
                    "  total = excluded.total, never_spoke = excluded.never_spoke, "
                    "  silent = excluded.silent",
                    (day, plat, acct, max(0, int(total)), max(0, int(never_spoke)),
                     max(0, int(silent))),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[ca_trend] snap 失败（已忽略）", exc_info=True)

    def has_day(self, *, now: Optional[float] = None) -> bool:
        """当日是否已有任何快照（懒快照的「一天一次」闸门）。"""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM contacts_asset_daily WHERE day = ? LIMIT 1",
                    (_day_str(now),)).fetchone()
            return row is not None
        except Exception:
            logger.debug("[ca_trend] has_day 失败（已忽略）", exc_info=True)
            return False

    def series(self, *, days: int = 14,
               now: Optional[float] = None) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）：[{day, total, never_spoke, silent, accounts}]。

        **只返回有快照的日子**——这是 gauge，缺天补零会画出「资产归零」的假凹坑
        （与 ui_event_trend 计数补零的口径刻意相反）。环比由消费方拿首/末元素算。
        """
        n = max(1, min(int(days or 14), 90))
        cut = _day_str((now if now is not None else time.time()) - (n - 1) * 86400)
        out: List[Dict[str, Any]] = []
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, SUM(total) AS t, SUM(never_spoke) AS ns, "
                    "       SUM(silent) AS s, COUNT(*) AS a "
                    "FROM contacts_asset_daily WHERE day >= ? "
                    "GROUP BY day ORDER BY day",
                    (cut,),
                ).fetchall():
                    out.append({
                        "day": str(r["day"]),
                        "total": int(r["t"] or 0),
                        "never_spoke": int(r["ns"] or 0),
                        "silent": int(r["s"] or 0),
                        "accounts": int(r["a"] or 0),
                    })
        except Exception:
            logger.debug("[ca_trend] series 读取失败（已忽略）", exc_info=True)
            return []
        return out

    def prune(self, *, retention_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        """删除超过保留期的旧快照。返回删除条数。"""
        keep = retention_days if retention_days is not None else _RETENTION_DAYS
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(keep)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM contacts_asset_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[ca_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 ui_event_trend 同构）─────────────────────────
_STORE: Optional[ContactsAssetTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 180.0
_CFG_LOCK = threading.Lock()


def configure_contacts_asset_trend(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 180.0,
) -> Optional[ContactsAssetTrendStore]:
    """启动期/热启用装配（幂等）。``enabled=False`` → 关闭（snap 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 180.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = ContactsAssetTrendStore(db_path)
            except Exception:
                logger.warning("[ca_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_contacts_asset_trend_store() -> Optional[ContactsAssetTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def reset_contacts_asset_trend() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "ContactsAssetTrendStore",
    "configure_contacts_asset_trend",
    "get_contacts_asset_trend_store",
    "reset_contacts_asset_trend",
]
