"""UI 交互事件「按日落库」时序持久化（SQLite）。

背景与定位
----------
``ui_event_stats`` 是**进程内**累计——本机重启频繁（2026-08-01 施工日实测一天 4 次），
重启即归零：AI 回复漏斗（``dpick.*``：取消率/采纳率——「等待体验达不达标」的判据）
的首批真实数据就是这样在当天被清洗掉的。本模块把 beacon 上报按 (日, 动作) 增量
upsert 落地，周读/看板画趋势不再受重启切割。

设计（对齐 frontend_error_trend / csrf_trend）：
- **纯增量 upsert**：``INSERT ... ON CONFLICT DO UPDATE``，写在 beacon 路由旁路。
- **默认关**：未 ``configure_ui_event_trend(enabled=True, ...)`` → record 恒 no-op。
- **模块级单例**：路由旁路调用一行。
- 只存 (日期, 消毒后动作, 计数)。与 frontend_error_trend 的关键差异：动作**没有**
  类型白名单（``_san_action`` 只约束格式不约束词表）→ 按日 distinct 封顶
  ``_DAY_ACTION_CAP``，超出折叠 ``__other__``（防刷量/脏数据把库撑爆）；
  绝不落页面路径/自由文本。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.web.ui_event_stats import _san_action

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ui_event_trend_daily (
    day    TEXT NOT NULL,
    action TEXT NOT NULL,
    n      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, action)
);
"""

_DAY_ACTION_CAP = 150   # 每日 distinct action 上限（超出折叠 __other__）


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与其他 trend store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class UiEventTrendStore:
    """UI 事件按 (日, 动作) 聚合（线程安全 SQLite）。"""

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

    def add(self, action: str, *, n: int = 1, now: Optional[float] = None) -> None:
        """把一次上报计入当日该动作。动作二次消毒；日基数封顶折叠。绝不抛。"""
        cnt = max(0, int(n))
        if cnt == 0:
            return
        a = _san_action(action)
        day = _day_str(now)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM ui_event_trend_daily WHERE day = ? AND action = ?",
                    (day, a)).fetchone()
                if row is None:
                    distinct = self._conn.execute(
                        "SELECT COUNT(*) AS c FROM ui_event_trend_daily WHERE day = ?",
                        (day,)).fetchone()
                    if int(distinct["c"] or 0) >= _DAY_ACTION_CAP:
                        a = "__other__"
                self._conn.execute(
                    "INSERT INTO ui_event_trend_daily (day, action, n) VALUES (?, ?, ?) "
                    "ON CONFLICT(day, action) DO UPDATE SET n = n + excluded.n",
                    (day, a, cnt),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[uiev_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, prefix: str = "",
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）：[{day, total, by_action:{...}}]。缺数据补零不断点。

        ``prefix``（如 ``dpick.``）＝读侧过滤：只统计该命名空间的动作——AI 回复漏斗
        读数不被其他 UI 埋点稀释。
        """
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        buckets: Dict[str, Dict[str, int]] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, action, n FROM ui_event_trend_daily WHERE day >= ? "
                    "ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    act = str(r["action"])
                    if prefix and not act.startswith(prefix):
                        continue
                    buckets.setdefault(str(r["day"]), {})[act] = int(r["n"])
        except Exception:
            logger.debug("[uiev_trend] daily 读取失败（已忽略）", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for day in day_keys:
            by_action = buckets.get(day, {})
            out.append({
                "day": day,
                "total": sum(by_action.values()),
                "by_action": dict(sorted(by_action.items())),
            })
        return out

    def prune(self, *, retention_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数。"""
        keep = retention_days if retention_days is not None else _RETENTION_DAYS
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(keep)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM ui_event_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[uiev_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 frontend_error_trend 同构）────────────────────
_STORE: Optional[UiEventTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_ui_event_trend(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[UiEventTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = UiEventTrendStore(db_path)
            except Exception:
                logger.warning("[uiev_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_ui_event_trend_store() -> Optional[UiEventTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_ui_event_trend(action: str) -> None:
    """beacon 路由旁路写入：未启用 / 无 store → 立即返回（零开销）。绝不抛。"""
    if not _ENABLED or _STORE is None:
        return
    _STORE.add(action)


def reset_ui_event_trend() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "UiEventTrendStore",
    "configure_ui_event_trend",
    "get_ui_event_trend_store",
    "record_ui_event_trend",
    "reset_ui_event_trend",
]
