# -*- coding: utf-8 -*-
"""人设一致性考题报告持久化（H3 线，2026-07-27）。

考题结果原先只活在进程内 job 表（TTL 30min），刷新即失。本模块把每人设最近 N 次
完整答卷落 SQLite，运营可回看「上次 100 / 上上次 80」。

设计（对齐 ``persona_bio_store``）：
- 线程安全（单连接 + Lock，``check_same_thread=False``，WAL）；``:memory:`` 供单测。
- **模块级 API 全部软失败**（DB 异常返回 None/空，绝不让考题/轮询链崩）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "config/persona_quiz.db"
DEFAULT_KEEP = 20
TREND_POINTS = 8          # overview 每人设携带的最近分数点数（sparkline 用）
DEFAULT_TREND_DAYS = 14


def _day_str(ts: float) -> str:
    """本地日期键 ``YYYY-MM-DD``（口径抄 ``isolation_trend_store._day_str``——
    考题按运营日观察，单机部署无跨时区诉求；不用 SQLite ``date(ts,'unixepoch')``，
    那是 UTC，会把凌晨的考卷算到前一天）。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))

_DDL = """
CREATE TABLE IF NOT EXISTS quiz_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id   TEXT NOT NULL,
    ts           REAL NOT NULL,
    score        INTEGER NOT NULL DEFAULT 0,
    passed       INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    n            INTEGER NOT NULL DEFAULT 0,
    persona_name TEXT NOT NULL DEFAULT '',
    items_json   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_quiz_reports_persona_ts
    ON quiz_reports (persona_id, ts DESC);
"""


class PersonaQuizStore:
    """人设考题报告库（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        items: Any = []
        try:
            items = json.loads(str(row["items_json"] or "[]"))
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        # 「关键词判错、LLM 裁判改判对」的题数——**派生自 items，不占列**（无需迁移）。
        # 这个数字持续走高 = 关键词判分层在漂移（题目/档案换了措辞而 expect 没跟上），
        # 是「分数还很好看但判分越来越依赖兜底」的先行指标。
        judged = sum(1 for it in items if isinstance(it, dict) and it.get("judged"))
        return {
            "id": int(row["id"]),
            "ts": float(row["ts"] or 0.0),
            "score": int(row["score"] or 0),
            "passed": int(row["passed"] or 0),
            "total": int(row["total"] or 0),
            "n": int(row["n"] or 0),
            "judged": judged,
            "persona_name": str(row["persona_name"] or ""),
            "items": items,
        }

    def save_report(self, persona_id: str, report: dict) -> Optional[int]:
        """从 ``run_quiz`` 结果落库；成功返回 row id，失败/缺参 None。落库后自动 prune。"""
        pid = str(persona_id or "").strip()
        if not pid or not isinstance(report, dict):
            return None
        try:
            score = int(report.get("score") or 0)
            passed = int(report.get("passed") or 0)
            total = int(report.get("total") or 0)
            n = int(report.get("n") or total or 0)
        except Exception:
            return None
        persona_name = str(report.get("persona_name") or "")
        items = report.get("items") if isinstance(report.get("items"), list) else []
        try:
            items_json = json.dumps(items, ensure_ascii=False)
        except Exception:
            items_json = "[]"
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO quiz_reports"
                " (persona_id, ts, score, passed, total, n, persona_name, items_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (pid, now, score, passed, total, n, persona_name, items_json),
            )
            self._conn.commit()
            row_id = int(cur.lastrowid) if cur.lastrowid else None
        if row_id is not None:
            try:
                self.prune(pid, keep=DEFAULT_KEEP)
            except Exception:
                logger.debug("[persona_quiz_store] prune 失败", exc_info=True)
        return row_id

    def list_reports(self, persona_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """新到旧；每项含 id/ts/score/passed/total/n/persona_name/items。"""
        pid = str(persona_id or "").strip()
        if not pid:
            return []
        try:
            lim = max(1, min(100, int(limit)))
        except Exception:
            lim = 10
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, score, passed, total, n, persona_name, items_json"
                " FROM quiz_reports WHERE persona_id = ?"
                " ORDER BY ts DESC LIMIT ?",
                (pid, lim),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_report(self, persona_id: str, report_id: int) -> Optional[Dict[str, Any]]:
        """取单份报告；不存在 / 人设不匹配 → None。"""
        pid = str(persona_id or "").strip()
        try:
            rid = int(report_id)
        except Exception:
            return None
        if not pid or rid <= 0:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, ts, score, passed, total, n, persona_name, items_json"
                " FROM quiz_reports WHERE persona_id = ? AND id = ?",
                (pid, rid),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def prune(self, persona_id: str, keep: int = DEFAULT_KEEP) -> int:
        """超出 keep 删除最旧；返回删除行数。"""
        pid = str(persona_id or "").strip()
        if not pid:
            return 0
        try:
            keep_n = max(1, int(keep))
        except Exception:
            keep_n = DEFAULT_KEEP
        with self._lock:
            # SQLite：DELETE 子查询含 LIMIT 需再包一层 SELECT
            cur = self._conn.execute(
                "DELETE FROM quiz_reports WHERE persona_id = ? AND id NOT IN ("
                "  SELECT id FROM ("
                "    SELECT id FROM quiz_reports WHERE persona_id = ?"
                "    ORDER BY ts DESC, id DESC LIMIT ?"
                "  )"
                ")",
                (pid, pid, keep_n),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def summary(self, persona_id: str) -> Dict[str, Any]:
        """``{"count","avg_score","last_score","last_ts"}``；无报告 → ``{}``。"""
        pid = str(persona_id or "").strip()
        if not pid:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c, AVG(score) AS avg_s,"
                " (SELECT score FROM quiz_reports WHERE persona_id = ?"
                "  ORDER BY ts DESC, id DESC LIMIT 1) AS last_s,"
                " (SELECT ts FROM quiz_reports WHERE persona_id = ?"
                "  ORDER BY ts DESC, id DESC LIMIT 1) AS last_ts"
                " FROM quiz_reports WHERE persona_id = ?",
                (pid, pid, pid),
            ).fetchone()
        if not row or int(row["c"] or 0) <= 0:
            return {}
        avg = row["avg_s"]
        return {
            "count": int(row["c"] or 0),
            "avg_score": float(avg) if avg is not None else 0.0,
            "last_score": int(row["last_s"] or 0),
            "last_ts": float(row["last_ts"] or 0.0),
        }

    def overview(self, limit_personas: int = 20) -> Dict[str, Any]:
        """跨进程考题读数：每人设一行 + 全局合计。

        ``{"personas": [{persona_id, persona_name, count, avg_score, last_score,
        last_ts, trend}], "totals": {personas, reports, avg_score, last_ts}}``。
        ``trend`` = 该人设最近 :data:`TREND_POINTS` 次分数，**旧→新**（画 sparkline
        的自然方向）。人设按 ``last_ts`` 降序取前 ``limit_personas`` 行；``totals``
        始终是**全库**口径（不受 limit 截断影响，否则「报告总数」会随展示条数变化）。
        空库 → personas 空列表 + totals 计数 0 / avg_score 与 last_ts 为 None。
        """
        try:
            lim = max(1, min(200, int(limit_personas)))
        except Exception:
            lim = 20
        with self._lock:
            trow = self._conn.execute(
                "SELECT COUNT(*) AS reports, COUNT(DISTINCT persona_id) AS personas,"
                " AVG(score) AS avg_s, MAX(ts) AS last_ts FROM quiz_reports"
            ).fetchone()
            rows = self._conn.execute(
                "SELECT persona_id, COUNT(*) AS c, AVG(score) AS avg_s,"
                " MAX(ts) AS last_ts FROM quiz_reports"
                " GROUP BY persona_id ORDER BY last_ts DESC LIMIT ?",
                (lim,),
            ).fetchall()
            personas: List[Dict[str, Any]] = []
            for r in rows:
                pid = str(r["persona_id"] or "")
                recent = self._conn.execute(
                    "SELECT score, persona_name FROM quiz_reports"
                    " WHERE persona_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                    (pid, TREND_POINTS),
                ).fetchall()
                if not recent:
                    continue
                avg = r["avg_s"]
                personas.append({
                    "persona_id": pid,
                    "persona_name": str(recent[0]["persona_name"] or ""),
                    "count": int(r["c"] or 0),
                    "avg_score": float(avg) if avg is not None else 0.0,
                    "last_score": int(recent[0]["score"] or 0),
                    "last_ts": float(r["last_ts"] or 0.0),
                    "trend": [int(x["score"] or 0) for x in reversed(recent)],
                })
        t_avg = trow["avg_s"] if trow else None
        t_last = trow["last_ts"] if trow else None
        return {
            "personas": personas,
            "totals": {
                "personas": int(trow["personas"] or 0) if trow else 0,
                "reports": int(trow["reports"] or 0) if trow else 0,
                "avg_score": float(t_avg) if t_avg is not None else None,
                "last_ts": float(t_last) if t_last is not None else None,
            },
        }

    def daily_scores(self, days: int = DEFAULT_TREND_DAYS,
                     *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """按**本地日期**聚合的考题均分：``[{date, runs, avg_score}]``（日期升序）。

        只回**有考卷**的日子（对齐 ``isolation_trend_store.recent`` 的「不补零」
        约定——没人考试 ≠ 均分 0，补零会画出假谷底）。窗口 = 近 ``days`` 天（含今天）。
        Python 侧聚合而非 SQL：SQLite 的日期函数按 UTC 切日，与本仓 local-date 口径不符。
        """
        try:
            n = max(1, min(90, int(days)))
        except Exception:
            n = DEFAULT_TREND_DAYS
        ref = now if now is not None else time.time()
        cutoff = _day_str(ref - (n - 1) * 86400)
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, score FROM quiz_reports ORDER BY ts"
            ).fetchall()
        buckets: Dict[str, List[int]] = {}
        for r in rows:
            day = _day_str(float(r["ts"] or 0.0))
            if day < cutoff:
                continue
            buckets.setdefault(day, []).append(int(r["score"] or 0))
        return [
            {"date": day, "runs": len(scores),
             "avg_score": round(sum(scores) / len(scores), 2)}
            for day, scores in sorted(buckets.items())
        ]


# ── 模块级单例 ───────────────────────────────────────────────────────────────

_STORE: Optional[PersonaQuizStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure(db_path: Any = DEFAULT_DB_PATH) -> Optional[PersonaQuizStore]:
    """启动期/测试装配（幂等）：指定库路径并建库。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _DB_PATH = str(db_path)
        try:
            _STORE = PersonaQuizStore(_DB_PATH)
        except Exception:
            logger.warning("[persona_quiz_store] 建库失败", exc_info=True)
            _STORE = None
        return _STORE


def get() -> Optional[PersonaQuizStore]:
    """取 store 单例（未配置则按默认路径懒建）。建库失败返回 None。"""
    global _STORE
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                try:
                    _STORE = PersonaQuizStore(_DB_PATH)
                except Exception:
                    logger.warning("[persona_quiz_store] 懒建库失败", exc_info=True)
                    _STORE = None
    return _STORE


def reset() -> None:
    """测试钩子：清空单例。"""
    global _STORE
    with _CFG_LOCK:
        _STORE = None


# ── 模块级软失败 API ─────────────────────────────────────────────────────────

def save_report(persona_id: str, report: dict) -> Optional[int]:
    try:
        store = get()
        return store.save_report(persona_id, report) if store else None
    except Exception:
        logger.warning("[persona_quiz_store] save_report 失败", exc_info=True)
        return None


def list_reports(persona_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        store = get()
        return store.list_reports(persona_id, limit=limit) if store else []
    except Exception:
        logger.warning("[persona_quiz_store] list_reports 失败", exc_info=True)
        return []


def get_report(persona_id: str, report_id: int) -> Optional[Dict[str, Any]]:
    try:
        store = get()
        return store.get_report(persona_id, report_id) if store else None
    except Exception:
        logger.warning("[persona_quiz_store] get_report 失败", exc_info=True)
        return None


def prune(persona_id: str, keep: int = DEFAULT_KEEP) -> int:
    try:
        store = get()
        return store.prune(persona_id, keep=keep) if store else 0
    except Exception:
        logger.warning("[persona_quiz_store] prune 失败", exc_info=True)
        return 0


def summary(persona_id: str) -> Dict[str, Any]:
    try:
        store = get()
        return store.summary(persona_id) if store else {}
    except Exception:
        logger.warning("[persona_quiz_store] summary 失败", exc_info=True)
        return {}


def overview(limit_personas: int = 20) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "personas": [],
        "totals": {"personas": 0, "reports": 0, "avg_score": None, "last_ts": None},
    }
    try:
        store = get()
        return store.overview(limit_personas=limit_personas) if store else empty
    except Exception:
        logger.warning("[persona_quiz_store] overview 失败", exc_info=True)
        return empty


def daily_scores(days: int = DEFAULT_TREND_DAYS) -> List[Dict[str, Any]]:
    try:
        store = get()
        return store.daily_scores(days=days) if store else []
    except Exception:
        logger.warning("[persona_quiz_store] daily_scores 失败", exc_info=True)
        return []


__all__ = [
    "DEFAULT_DB_PATH", "DEFAULT_KEEP", "DEFAULT_TREND_DAYS", "TREND_POINTS",
    "PersonaQuizStore",
    "configure", "get", "reset",
    "save_report", "list_reports", "get_report", "prune", "summary",
    "overview", "daily_scores",
]
