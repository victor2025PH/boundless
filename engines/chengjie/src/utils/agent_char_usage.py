"""坐席级字符用量账本（用户 × 日 × 类目）—— 团队额度体系的数据地基。

定位
====
`licensing/quota_store.py` 管「整个授权用了多少字符」（翻译/TTS，按 lic_id 聚合）；
本模块补上唯一缺失的维度——**人**：坐席在工作台手动触发的字符消耗（发语音合成、
试听、手动翻译等）按 (username, 日, 类目) 增量 upsert 落 ``agent_char_usage.db``。

设计完全对齐 quota_store 的既有纪律：
- **按日 upsert**：行数上界 = 人数 × 天数 × 类目数（极小），不做 prune；
  月度口径 = 按 day 前缀聚合，自然月自动"重置"，无需清零任务。
- **默认关**：``usage.agent_chars.enabled``（配置缺省 False）——未开启的部署零 IO；
  本机生产经 config.local.yaml overlay 开启（"新子系统默认关"纪律）。
- **绝不抛**：记账/读数任何异常吞掉并 debug 日志，额度观测永远不能打挂业务主链。
- **UTC 日期键**：与 quota_store/tts_cost_store 同口径，跨时区部署对账一致。
- 只存 (用户名, 日期, 类目, 字符数) 四元组，**绝不记录任何文本内容**。

归因约定
========
- 坐席触发的消耗在**路由层**记账（那里有 request.session 登录身份）：
  ``record_request_chars(request, category, chars)`` 一行接线。
- 自动链（AI 自动回复 / 主动触达）当前**不占任何坐席额度**；将来若要观测可用
  保留主体名 ``AUTO_ACTOR``（"__auto__"）记账，面板单独一行展示。
- 类目沿用授权额度口径：``translation`` / ``tts``。

额度判定
========
``agent_quota_status(used, quota)`` 纯函数输出 unlimited/ok/warn/over 四态
（quota<=0 = 不限；>=80% warn；超额 over），users 页 / 用量页 / 我的用量三个
消费面共用同一判定，绝不各算一套。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 自动链虚拟主体（本批未接线，先钉住命名防将来各写各的）
AUTO_ACTOR = "__auto__"

# 类目白名单外的输入归并到 other，防手误类目把面板撑出碎行
KNOWN_CATEGORIES = ("translation", "tts")

_DDL = """
CREATE TABLE IF NOT EXISTS agent_char_usage (
    username  TEXT NOT NULL,
    day       TEXT NOT NULL,
    category  TEXT NOT NULL,
    chars     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (username, day, category)
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与 quota_store 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def _month_str(now: Optional[float] = None) -> str:
    """UTC 月份键 ``YYYY-MM``。"""
    return time.strftime("%Y-%m", time.gmtime(now if now is not None else time.time()))


def _norm_category(category: Any) -> str:
    c = str(category or "").strip().lower()
    return c if c in KNOWN_CATEGORIES else "other"


class AgentCharUsageStore:
    """按 (username, 日, 类目) 聚合的坐席字符用量（线程安全 SQLite）。"""

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

    def record(
        self, username: str, category: str, chars: int, *, now: Optional[float] = None,
    ) -> None:
        """记一笔已交付字符到当日聚合。绝不抛（吞掉所有异常）。"""
        n = int(chars or 0)
        u = str(username or "").strip()
        if n <= 0 or not u:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO agent_char_usage (username, day, category, chars) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username, day, category) DO UPDATE SET "
                    "  chars = chars + excluded.chars",
                    (u, day, _norm_category(category), n),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[agent_chars] record 失败（已忽略）", exc_info=True)

    def month_totals(self, month: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """某自然月（``YYYY-MM``，缺省当月）逐用户聚合。

        返回 ``{username: {"total": int, "by_category": {cat: int}}}``；读失败返回空。
        """
        m = str(month or _month_str())
        out: Dict[str, Dict[str, Any]] = {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT username, category, SUM(chars) AS s FROM agent_char_usage "
                    "WHERE substr(day, 1, 7) = ? GROUP BY username, category",
                    (m,),
                ).fetchall()
            for r in rows:
                u = str(r["username"])
                slot = out.setdefault(u, {"total": 0, "by_category": {}})
                n = int(r["s"] or 0)
                slot["by_category"][str(r["category"])] = n
                slot["total"] += n
        except Exception:
            logger.debug("[agent_chars] month_totals 读取失败（空）", exc_info=True)
        return out

    def day_totals(self, day: Optional[str] = None) -> Dict[str, int]:
        """某日（``YYYY-MM-DD``，缺省今日 UTC）逐用户合计。读失败返回空。"""
        d = str(day or _day_str())
        out: Dict[str, int] = {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT username, SUM(chars) AS s FROM agent_char_usage "
                    "WHERE day = ? GROUP BY username",
                    (d,),
                ).fetchall()
            for r in rows:
                out[str(r["username"])] = int(r["s"] or 0)
        except Exception:
            logger.debug("[agent_chars] day_totals 读取失败（空）", exc_info=True)
        return out

    def usage_for(self, username: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        """单用户视角：本月合计 / 今日合计 / 本月分类目。读失败返回零值。"""
        u = str(username or "").strip()
        out: Dict[str, Any] = {"month_total": 0, "today_total": 0, "by_category": {}}
        if not u:
            return out
        m, d = _month_str(now), _day_str(now)
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT category, SUM(chars) AS s FROM agent_char_usage "
                    "WHERE username = ? AND substr(day, 1, 7) = ? GROUP BY category",
                    (u, m),
                ).fetchall():
                    n = int(r["s"] or 0)
                    out["by_category"][str(r["category"])] = n
                    out["month_total"] += n
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(chars), 0) AS s FROM agent_char_usage "
                    "WHERE username = ? AND day = ?",
                    (u, d),
                ).fetchone()
                out["today_total"] = int(row["s"] or 0)
        except Exception:
            logger.debug("[agent_chars] usage_for 读取失败（零值）", exc_info=True)
        return out

    def daily_series(self, days: int = 14, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """近 N 天逐日分类目合计（旧 → 新，缺数据日补零）。读失败返回空表。"""
        n = max(1, min(90, int(days or 14)))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - (n - 1 - i) * 86400) for i in range(n)]
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT day, category, SUM(chars) AS s FROM agent_char_usage "
                    "WHERE day >= ? GROUP BY day, category",
                    (day_keys[0],),
                ).fetchall()
        except Exception:
            logger.debug("[agent_chars] daily_series 读取失败（空表）", exc_info=True)
            return []
        by_day: Dict[str, Dict[str, int]] = {}
        for r in rows:
            d = str(r["day"])
            by_day.setdefault(d, {})[str(r["category"])] = int(r["s"] or 0)
        out: List[Dict[str, Any]] = []
        for d in day_keys:
            cats = by_day.get(d, {})
            out.append({"day": d, "by_category": cats, "total": sum(cats.values())})
        return out


# ── 纯函数：额度判定（users 页 / 用量页 / 我的用量 单一口径）──────────────────


def agent_quota_status(used: Any, quota: Any) -> Dict[str, Any]:
    """坐席月度额度状态。``quota<=0`` = 不限（unlimited）；>=80% warn；超额 over。

    输出 {used, quota, ratio, pct, level}；level ∈ unlimited/ok/warn/over。
    """
    try:
        u = max(0, int(used or 0))
    except (TypeError, ValueError):
        u = 0
    try:
        q = int(quota or 0)
    except (TypeError, ValueError):
        q = 0
    if q <= 0:
        return {"used": u, "quota": 0, "ratio": None, "pct": 0, "level": "unlimited"}
    ratio = u / q
    if u > q:
        level = "over"
    elif ratio >= 0.8:
        level = "warn"
    else:
        level = "ok"
    return {
        "used": u,
        "quota": q,
        "ratio": round(ratio, 4),
        "pct": min(999, int(round(ratio * 100))),
        "level": level,
    }


def agent_chars_enabled(config: Optional[dict]) -> bool:
    """总开关 ``usage.agent_chars.enabled``，默认 **False**（新子系统默认关）。"""
    try:
        usage = (config or {}).get("usage") if isinstance(config, dict) else None
        ac = (usage or {}).get("agent_chars") if isinstance(usage, dict) else None
        if isinstance(ac, dict):
            return bool(ac.get("enabled", False))
        return False
    except Exception:
        return False


# ── 模块级单例 + 惰性建库 ─────────────────────────────────────────────────────

_STORE: Optional[AgentCharUsageStore] = None
_DB_PATH: Optional[str] = None
_CFG_LOCK = threading.Lock()


def _default_db_path() -> str:
    """账本位置跟随可写数据区（与 license_quota.db 同目录约定）。"""
    from src.licensing.data_paths import data_file

    return data_file("agent_char_usage.db")


def configure_agent_char_usage(
    *, db_path: Any = None, store: Optional[AgentCharUsageStore] = None,
) -> Optional[AgentCharUsageStore]:
    """启动期装配（可选）：覆盖 db 路径或直接注入 store（测试用）。幂等。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        if store is not None:
            _STORE = store
        if db_path is not None:
            _DB_PATH = str(db_path)
        return _STORE


def get_agent_char_usage_store() -> Optional[AgentCharUsageStore]:
    """当前 store（未建库 → None）。供只读观测端点用。"""
    return _STORE


def reset_agent_char_usage_store() -> None:
    """测试钩子：清空单例/路径。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _STORE = None
        _DB_PATH = None


def _ensure_store() -> Optional[AgentCharUsageStore]:
    """惰性建库（仅在开关开启且真有记账/读数时被调用）。失败降级为不计量。"""
    global _STORE
    with _CFG_LOCK:
        if _STORE is None:
            try:
                _STORE = AgentCharUsageStore(_DB_PATH or _default_db_path())
            except Exception:
                logger.warning("[agent_chars] 建库失败，坐席字符计量禁用", exc_info=True)
                _STORE = None
        return _STORE


def _request_config(request: Any) -> Optional[dict]:
    try:
        cm = getattr(getattr(request, "app").state, "config_manager", None)
        cfg = getattr(cm, "config", None)
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def record_named_chars(
    username: str, category: str, chars: int, *, config: Optional[dict] = None,
) -> None:
    """按显式用户名记账（后台任务/闭包场景：session 在派发时捕获，成功回调时落账）。

    与 record_request_chars 同守卫：开关关 / 空用户名 / chars<=0 → 零 IO；绝不抛。
    """
    try:
        n = int(chars or 0)
        u = str(username or "").strip()
        if n <= 0 or not u:
            return
        if not agent_chars_enabled(config):
            return
        store = _ensure_store()
        if store is None:
            return
        store.record(u, category, n)
    except Exception:
        logger.debug("[agent_chars] record_named_chars 失败（已忽略）", exc_info=True)


def record_request_chars(request: Any, category: str, chars: int) -> None:
    """路由层一行接线：按当前登录坐席记一笔字符消耗。

    开关关 / 未登录（无 session username）/ chars<=0 → 零 IO；任何异常吞掉。
    刻意不在此处判定额度（本批只计量不拦截；enforce 属下一阶段的独立开关）。
    """
    try:
        n = int(chars or 0)
        if n <= 0:
            return
        sess = getattr(request, "session", None)
        username = ""
        if sess is not None:
            try:
                username = str(sess.get("username") or "").strip()
            except Exception:
                username = ""
        if not username:
            return
        if not agent_chars_enabled(_request_config(request)):
            return
        store = _ensure_store()
        if store is None:
            return
        store.record(username, category, n)
    except Exception:
        logger.debug("[agent_chars] record_request_chars 失败（已忽略）", exc_info=True)


def ensure_store_for_read(config: Optional[dict]) -> Optional[AgentCharUsageStore]:
    """读数端入口：开关开启才建库返回 store；关闭返回 None（面板显示未启用态）。"""
    if not agent_chars_enabled(config):
        return get_agent_char_usage_store()
    return _ensure_store()


def check_request_quota(request: Any, *, user_store: Any = None) -> Dict[str, Any]:
    """请求前的坐席月度字符额度判定（路由层硬闸的单一入口）。

    返回 ``{allowed, enforce, enabled, level, used, quota, pct}``。语义：
    - ``enabled``=``usage.agent_chars.enabled``（计量总开关，默认关）；
      ``enforce``=``usage.agent_chars.enforce``（**硬限开关，默认关**——本批刻意
      不在生产开硬限，先软提醒攒读数，watchdog 告警链闭环后再由运营决策开闸）。
    - 未登录（token 链/内部调用不属坐席额度语义）/ master 角色 / ``quota<=0``
      （=不限额）/ enabled 或 enforce 任一关 → ``allowed=True``，但尽量回填
      level/used/quota 供调用方展示（软提醒场景前端要读这些数）。
    - ``used``=账本本月合计（``usage_for()["month_total"]``）；仅当
      ``used>=quota 且 enabled 且 enforce`` 才 ``allowed=False``。
    - ``user_store`` 缺省从 ``request.app.state.user_store`` 取（admin.py 已暴露）；
      取不到 / 用户行缺失 / **任何异常一律放行**（fail-open：额度闸绝不能因
      store 故障把发送/翻译主链打挂——与本文件「绝不抛」纪律同源）。
    """
    out: Dict[str, Any] = {
        "allowed": True, "enforce": False, "enabled": False,
        "level": "unlimited", "used": 0, "quota": 0, "pct": 0,
    }
    try:
        cfg = _request_config(request)
        usage = (cfg or {}).get("usage") if isinstance(cfg, dict) else None
        ac = (usage or {}).get("agent_chars") if isinstance(usage, dict) else None
        ac = ac if isinstance(ac, dict) else {}
        out["enabled"] = bool(ac.get("enabled", False))
        out["enforce"] = bool(ac.get("enforce", False))

        sess = getattr(request, "session", None)
        username, sess_role = "", ""
        if sess is not None:
            try:
                username = str(sess.get("username") or "").strip()
                sess_role = str(sess.get("role") or "").strip().lower()
            except Exception:
                username, sess_role = "", ""
        if not username:
            return out

        us = user_store
        if us is None:
            us = getattr(getattr(request, "app").state, "user_store", None)
        if us is None or not hasattr(us, "get_user"):
            return out
        row = us.get_user(username) or {}
        role = str(row.get("role") or sess_role or "").strip().lower()
        try:
            quota = int(row.get("monthly_char_quota") or 0)
        except (TypeError, ValueError):
            quota = 0

        # 读数回填（master / 不限额 / 软提醒场景都要展示 used）：开关开才惰性建库，
        # 关着只复用既有 store（与 ensure_store_for_read 同口径，不为读数新建文件）。
        store = ensure_store_for_read(cfg)
        used = 0
        if store is not None:
            try:
                used = int(store.usage_for(username).get("month_total") or 0)
            except Exception:
                used = 0
        st = agent_quota_status(used, quota)
        out.update({"level": st["level"], "used": st["used"],
                    "quota": st["quota"], "pct": st["pct"]})

        if role == "master":
            return out          # master 恒放行（额度是管坐席的，不闸管理者）
        if quota <= 0:
            return out          # 0 = 不限额
        if not (out["enabled"] and out["enforce"]):
            return out          # 计量关 / 只软提醒 → 不拦
        if used >= quota:
            out["allowed"] = False
        return out
    except Exception:
        logger.debug("[agent_chars] check_request_quota 异常（放行）", exc_info=True)
        out["allowed"] = True
        return out


__all__ = [
    "AUTO_ACTOR",
    "AgentCharUsageStore",
    "agent_chars_enabled",
    "agent_quota_status",
    "check_request_quota",
    "configure_agent_char_usage",
    "ensure_store_for_read",
    "get_agent_char_usage_store",
    "record_named_chars",
    "record_request_chars",
    "reset_agent_char_usage_store",
]
