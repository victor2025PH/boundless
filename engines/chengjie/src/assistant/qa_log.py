# -*- coding: utf-8 -*-
"""assistant 问答台账（config/assistant.db · qa_log 一张表）。

用途：自答率统计（ops 卡）+ miss 清单（answered=0 或 verdict=down 的
问题按频次排序 = 运营补语料的照单清单，飞轮起点）。滚动保留 90 天。

落点走 licensing.data_paths.config_dir()（可写数据区 SSOT；测试经
AITR_DATA_DIR 指向 tmp 自动隔离，绝不写仓库 config/）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("ai_chat_assistant.assistant.qa_log")

_RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    page TEXT NOT NULL DEFAULT '',
    q TEXT NOT NULL,
    answered INTEGER NOT NULL DEFAULT 0,
    top_score REAL NOT NULL DEFAULT 0,
    sources TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_qa_log_ts ON qa_log(ts);
"""


def _norm_q(q: str) -> str:
    """miss 聚合键：去空白/标点压缩，防「怎么发语音？」「怎么发语音 」分裂计数。"""
    s = re.sub(r"[\s\u3000]+", "", str(q or ""))
    s = re.sub(r"[?？!！。.,，;；:：~～]+$", "", s)
    return s.lower()[:120]


class AssistantQALog:
    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            from src.licensing.data_paths import config_dir

            db_path = config_dir() / "assistant.db"
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.executescript(_SCHEMA)
        except Exception:
            logger.warning("assistant qa_log schema init 失败", exc_info=True)

    # ------------------------------------------------------------------ 写
    def record(
        self,
        *,
        user_id: str,
        role: str,
        page: str,
        q: str,
        answered: bool,
        top_score: float = 0.0,
        sources: str = "",
        latency_ms: int = 0,
        now: float | None = None,
    ) -> int:
        """落一行问答记账；返回 qa_id（失败返 0，绝不抛——记账不阻塞问答）。"""
        ts = time.time() if now is None else now
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO qa_log(ts,user_id,role,page,q,answered,top_score,"
                    "sources,latency_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        ts,
                        str(user_id or "")[:64],
                        str(role or "")[:24],
                        str(page or "")[:120],
                        str(q or "")[:2000],
                        1 if answered else 0,
                        float(top_score or 0.0),
                        str(sources or "")[:800],
                        int(latency_ms or 0),
                    ),
                )
                qa_id = int(cur.lastrowid or 0)
                # 顺手滚动清理（低频写入场景成本可忽略）
                conn.execute(
                    "DELETE FROM qa_log WHERE ts < ?", (ts - _RETENTION_DAYS * 86400,)
                )
                return qa_id
        except Exception:
            logger.warning("assistant qa_log record 失败", exc_info=True)
            return 0

    def set_verdict(self, qa_id: int, verdict: str) -> bool:
        v = str(verdict or "").strip().lower()
        if v not in ("up", "down"):
            return False
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    "UPDATE qa_log SET verdict=? WHERE id=?", (v, int(qa_id))
                )
                return cur.rowcount > 0
        except Exception:
            logger.warning("assistant qa_log verdict 失败", exc_info=True)
            return False

    # ------------------------------------------------------------------ 读
    def miss_list(self, days: int = 14, limit: int = 10) -> list[dict]:
        """未答/差评问题按归一化键聚合，频次降序——运营补语料照单。"""
        since = time.time() - max(1, days) * 86400
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT q, answered, verdict FROM qa_log WHERE ts >= ? AND "
                    "(answered=0 OR verdict='down')",
                    (since,),
                ).fetchall()
        except Exception:
            logger.warning("assistant qa_log miss_list 失败", exc_info=True)
            return []
        agg: dict[str, dict] = {}
        for r in rows:
            key = _norm_q(r["q"])
            if not key:
                continue
            slot = agg.setdefault(key, {"q": str(r["q"])[:120], "count": 0, "down": 0})
            slot["count"] += 1
            if r["verdict"] == "down":
                slot["down"] += 1
        out = sorted(agg.values(), key=lambda x: (-x["count"], x["q"]))
        return out[: max(1, limit)]

    def top_questions(self, days: int = 14, limit: int = 6,
                      page: str = "") -> list[str]:
        """已成功回答的高频问题（面板快捷 chips 的动态来源）。

        P1（2026-08-23）：带 page 时**本页高频优先**、全局高频补位——
        坐席在收件箱问的和老板在运营总览问的不是同一批问题。page 精确
        匹配记录页（record 存的就是 location.pathname）。"""
        since = time.time() - max(1, days) * 86400
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT q, page FROM qa_log WHERE ts >= ? AND answered=1 "
                    "AND verdict != 'down'",
                    (since,),
                ).fetchall()
        except Exception:
            logger.warning("assistant qa_log top_questions 失败", exc_info=True)
            return []
        want_page = str(page or "").strip()
        agg_all: dict[str, dict] = {}
        agg_page: dict[str, dict] = {}
        for r in rows:
            key = _norm_q(r["q"])
            if not key:
                continue
            slot = agg_all.setdefault(key, {"q": str(r["q"])[:80], "count": 0})
            slot["count"] += 1
            if want_page and str(r["page"] or "") == want_page:
                ps = agg_page.setdefault(key,
                                         {"q": str(r["q"])[:80], "count": 0})
                ps["count"] += 1
        lim = max(1, limit)
        ranked_page = sorted(agg_page.values(),
                             key=lambda x: (-x["count"], x["q"]))
        out: list[str] = [x["q"] for x in ranked_page[:lim]]
        seen = {_norm_q(q) for q in out}
        for x in sorted(agg_all.values(), key=lambda y: (-y["count"], y["q"])):
            if len(out) >= lim:
                break
            if _norm_q(x["q"]) in seen:
                continue
            out.append(x["q"])
            seen.add(_norm_q(x["q"]))
        return out

    def stats(self, days: int = 7) -> dict:
        """聚合读数（ops 卡）：问答量/自答率/差评数 + **拒答分型**。

        拒答分型（2026-08-27）——「没答上」有两种，成因和处置完全不同：

          * ``miss_no_hit``  检索零命中（``top_score=0``）：语料里压根没有
            相关条目 → 处置是**补语料**（照 how-to 缺口清单加条目）；
          * ``miss_no_basis`` 检索命中了但 LLM 自认答不了（``top_score>0``，
            即 NO_BASIS 哨兵）：条目沾边但回答不了 → 处置是**看这批问题该不该
            进产品**，或该条目的内容不够。

        **零改表**：靠既有 ``top_score`` 列区分——零命中分支不传该参数（默认 0），
        哨兵分支必传 ``strong[0]["score"]``（必 > min_score）。因此这个分型对
        **历史数据同样成立**，不需要迁移也不丢过去的账。

        哨兵是**依赖模型行为**的机制（LLM 得肯输出 NO_BASIS），所以它的实际
        触发量必须可观测——否则「它到底在不在工作」是黑箱。
        """
        since = time.time() - max(1, days) * 86400
        empty = {"n": 0, "answered": 0, "rate": 0.0, "up": 0, "down": 0,
                 "miss": 0, "miss_no_hit": 0, "miss_no_basis": 0,
                 "no_basis_share": 0.0}
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, SUM(answered) AS ok, "
                    "SUM(CASE WHEN verdict='down' THEN 1 ELSE 0 END) AS down, "
                    "SUM(CASE WHEN verdict='up' THEN 1 ELSE 0 END) AS up, "
                    "SUM(CASE WHEN answered=0 AND top_score<=0 THEN 1 ELSE 0 END) "
                    "AS no_hit, "
                    "SUM(CASE WHEN answered=0 AND top_score>0 THEN 1 ELSE 0 END) "
                    "AS no_basis "
                    "FROM qa_log WHERE ts >= ?",
                    (since,),
                ).fetchone()
        except Exception:
            logger.warning("assistant qa_log stats 失败", exc_info=True)
            return empty
        n = int(row["n"] or 0)
        ok = int(row["ok"] or 0)
        no_hit = int(row["no_hit"] or 0)
        no_basis = int(row["no_basis"] or 0)
        miss = no_hit + no_basis
        return {
            "n": n,
            "answered": ok,
            "rate": round(ok / n, 3) if n else 0.0,
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "miss": miss,
            "miss_no_hit": no_hit,
            "miss_no_basis": no_basis,
            # 哨兵在全部拒答里的占比——判「哨兵在不在工作」的主读数：
            # 恒 0 = LLM 从不输出哨兵（提示词没生效 / 模型不听话）；
            # 接近 1 = 几乎全靠哨兵拒答，检索层可能过松。
            "no_basis_share": round(no_basis / miss, 3) if miss else 0.0,
        }


_QA_LOG: AssistantQALog | None = None
_QA_LOCK = threading.Lock()


def get_qa_log() -> AssistantQALog:
    global _QA_LOG
    if _QA_LOG is None:
        with _QA_LOCK:
            if _QA_LOG is None:
                _QA_LOG = AssistantQALog()
    return _QA_LOG
