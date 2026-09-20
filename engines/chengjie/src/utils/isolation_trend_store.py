"""「账号隔离健康」按日趋势落库（SQLite）。

背景与定位
----------
:func:`src.utils.isolation_health.build_isolation_health` 出的是**瞬时快照**——看板只能
看「此刻」存量键 / 未绑号有多少，看不到「隔离改造收编后是不是又在回升」（有入口漏传
account、或新上号漏绑人设，都是按天缓慢渗出的）。本模块把快照按日落一行，供
``/api/admin/isolation-health`` 附带近 N 天 sparkline + 回升告警信号。

设计（结构对齐 :mod:`src.ai.translation_trend_store`，两处刻意不同）：
- **覆盖式 upsert 而非增量累加**：快照是存量水位（gauge）不是流量计数（counter），
  同日重拍 = 新水位覆盖旧水位（幂等）；translation_trend 的 ``+= excluded`` 语义不适用。
- **recent 不补零**：没拍到快照的日子就是没有观测（服务停机/未启用），补零会画出
  「存量键归零又回升」的假谷底，直接把 regression_signal 骗出假告警；sparkline 少几个
  点即可，语义诚实优先。
- **模块级单例三件套**（对齐 persona_media_store），但 ``get_`` **不做默认路径懒建**：
  库路径必须随实例 config 目录走（双实例隔离），没有合理的仓库级默认值；
  未 configure = 功能未开，路由侧静默跳过（feature flag 纪律）。
- 只存日期与计数列，绝不落任何会话内容/账号明文以外的数据。
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
CREATE TABLE IF NOT EXISTS isolation_daily (
    day              TEXT NOT NULL PRIMARY KEY,
    scoped_keys      INTEGER NOT NULL DEFAULT 0,
    legacy_keys      INTEGER NOT NULL DEFAULT 0,
    bare_keys        INTEGER NOT NULL DEFAULT 0,
    scoped_rows      INTEGER NOT NULL DEFAULT 0,
    legacy_rows      INTEGER NOT NULL DEFAULT 0,
    bare_rows        INTEGER NOT NULL DEFAULT 0,
    online_accounts  INTEGER NOT NULL DEFAULT 0,
    unbound_accounts INTEGER NOT NULL DEFAULT 0,
    fatex_rows       INTEGER NOT NULL DEFAULT 0,
    fatex_complete   INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0
);
"""

# 既有库升级钩子（当前无迁移；加列时进这里，ALTER 幂等失败即已存在）
_MIGRATIONS: List[str] = []

_COLS = (
    "scoped_keys", "legacy_keys", "bare_keys",
    "scoped_rows", "legacy_rows", "bare_rows",
    "online_accounts", "unbound_accounts",
    "fatex_rows", "fatex_complete",
)


def _day_str(now: Optional[float] = None) -> str:
    """本地日期键 ``YYYY-MM-DD``（隔离体检按运营日观察，单机部署无跨时区诉求）。"""
    return time.strftime(
        "%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def _i(d: Any, key: str) -> int:
    """从疑似 dict 里安全取非负 int（坏容器/坏值一律 0，绝不抛）。"""
    if not isinstance(d, dict):
        return 0
    try:
        return max(0, int(d.get(key) or 0))
    except (TypeError, ValueError):
        return 0


class IsolationTrendStore:
    """账号隔离健康按日水位（线程安全 SQLite）。"""

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
            for mig in _MIGRATIONS:
                try:
                    self._conn.execute(mig)
                except sqlite3.OperationalError:
                    pass  # 列已存在（新建库走 DDL）
            self._conn.commit()

    def upsert_today(self, snapshot: Any, *, now: Optional[float] = None) -> bool:
        """把 :func:`build_isolation_health` 快照落当日行（同日重拍=覆盖，幂等）。

        入参非 dict → 不落行返 False；内层字段缺失/坏值按 0 落（坏 snapshot 容错，
        绝不抛）。``unbound_accounts`` 取 ``online - bound``（快照的 unbound 点名
        列表封顶 8 条，不是计数口径）。
        """
        if not isinstance(snapshot, dict):
            return False
        keys = snapshot.get("keys")
        personas = snapshot.get("personas")
        fatex = snapshot.get("fatex")
        online = _i(personas, "online")
        vals = {
            "scoped_keys": _i(keys, "scoped"),
            "legacy_keys": _i(keys, "legacy_platform"),
            "bare_keys": _i(keys, "bare"),
            "scoped_rows": _i(keys, "scoped_rows"),
            "legacy_rows": _i(keys, "legacy_rows"),
            "bare_rows": _i(keys, "bare_rows"),
            "online_accounts": online,
            "unbound_accounts": max(0, online - _i(personas, "bound")),
            "fatex_rows": _i(fatex, "birth_profiles"),
            "fatex_complete": _i(fatex, "complete"),
        }
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO isolation_daily "
                    "(day, " + ", ".join(_COLS) + ", updated_at) "
                    "VALUES (?" + ", ?" * (len(_COLS) + 1) + ") "
                    "ON CONFLICT(day) DO UPDATE SET "
                    + ", ".join(f"{c} = excluded.{c}" for c in _COLS)
                    + ", updated_at = excluded.updated_at",
                    (day, *(vals[c] for c in _COLS), time.time()),
                )
                self._conn.commit()
            return True
        except Exception:
            logger.debug("[isolation_trend] upsert_today 失败（已忽略）", exc_info=True)
            return False

    def recent(self, days: int = 7, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """近 N 天有数据的行（按日升序）。**不补零**——没观测的日子不画假 0 点。"""
        n = max(1, min(int(days or 7), 90))
        cutoff = _day_str((now if now is not None else time.time()) - (n - 1) * 86400)
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT day, " + ", ".join(_COLS) + ", updated_at "
                    "FROM isolation_daily WHERE day >= ? ORDER BY day",
                    (cutoff,),
                ).fetchall()
        except Exception:
            logger.debug("[isolation_trend] recent 读取失败（已忽略）", exc_info=True)
            return []
        out: List[Dict[str, Any]] = []
        for r in rows:
            item: Dict[str, Any] = {"day": str(r["day"])}
            for c in _COLS:
                item[c] = int(r[c] or 0)
            item["updated_at"] = float(r["updated_at"] or 0.0)
            out.append(item)
        return out

    def regression_signal(self) -> Dict[str, Any]:
        """回升信号：对比最近两个**有数据**的日期。

        ``legacy_keys+bare_keys``（存量键合计）或 ``unbound_accounts`` 上升 →
        ``{"regressed": True, "detail": "..."}``；持平/下降/只有一天数据 → False。
        detail 为纯数据格式（键名 旧→新 + 日期区间），显示端文案走 i18n。
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT day, legacy_keys, bare_keys, unbound_accounts "
                    "FROM isolation_daily ORDER BY day DESC LIMIT 2",
                ).fetchall()
        except Exception:
            logger.debug("[isolation_trend] regression 读取失败（已忽略）", exc_info=True)
            return {"regressed": False, "detail": ""}
        if len(rows) < 2:
            return {"regressed": False, "detail": ""}
        latest, prev = rows[0], rows[1]
        lb_new = int(latest["legacy_keys"] or 0) + int(latest["bare_keys"] or 0)
        lb_old = int(prev["legacy_keys"] or 0) + int(prev["bare_keys"] or 0)
        ub_new = int(latest["unbound_accounts"] or 0)
        ub_old = int(prev["unbound_accounts"] or 0)
        parts: List[str] = []
        if lb_new > lb_old:
            parts.append(f"legacy+bare {lb_old}->{lb_new}")
        if ub_new > ub_old:
            parts.append(f"unbound {ub_old}->{ub_new}")
        if not parts:
            return {"regressed": False, "detail": ""}
        detail = "; ".join(parts) + f" ({prev['day']} -> {latest['day']})"
        return {"regressed": True, "detail": detail}


# ── 模块级单例（三件套对齐 persona_media_store；get 不做默认路径懒建，见模块 docstring）──
_STORE: Optional[IsolationTrendStore] = None
_CFG_LOCK = threading.Lock()


def configure_isolation_trend_store(db_path: Any) -> Optional[IsolationTrendStore]:
    """装配（幂等：已建则返回既有实例）。路由层在开关开启时懒调用。"""
    global _STORE
    with _CFG_LOCK:
        if _STORE is None:
            try:
                _STORE = IsolationTrendStore(db_path)
            except Exception:
                logger.warning("[isolation_trend] 建库失败", exc_info=True)
                _STORE = None
        return _STORE


def get_isolation_trend_store() -> Optional[IsolationTrendStore]:
    """取单例；未 configure → None（调用方静默跳过，不落野库文件）。"""
    return _STORE


def reset_isolation_trend_store() -> None:
    """测试钩子：清空单例。"""
    global _STORE
    with _CFG_LOCK:
        _STORE = None


__all__ = [
    "IsolationTrendStore",
    "configure_isolation_trend_store",
    "get_isolation_trend_store",
    "reset_isolation_trend_store",
]
