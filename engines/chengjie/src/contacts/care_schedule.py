"""Phase O2：主动关怀待办持久层（SQLite）。

O1 抽取出「到期关怀约定」后落这张 ``care_schedule`` 表，O3 到点消费、O4 后台可见化。
把 O1 的「宁滥」在入库层收敛为「宁缺」：**置信度阈值 + 同主题去重 + 过期清理**。

状态机：``pending → sent | skipped | expired | cancelled``。
隐私：只存短摘要（≤160 字）。纯存储、平台无关、可单测（``:memory:``）。
默认关：是否喂入抽取由上层 ``companion.proactive_care.enabled`` 控（O3 接线时引）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.contacts.care_commitment import CareCommitment, extract_commitments

logger = logging.getLogger("CareScheduleStore")

_DEFAULT_MIN_CONFIDENCE = 0.6   # 挡住 O1 无主题兜底（0.5）等低质项
_DEFAULT_DEDUP_WINDOW_DAYS = 3.0
_DAY = 86400.0

# Phase ④续¹⁰：危机关怀升级（## 56）排进 care 队列时用的保留 topic——派发器据此切到
# 「克制陪伴」语气模板（不寒暄、不追问、不引用具体事），区别普通约定回访。两端共享此常量。
CRISIS_CARE_TOPIC = "情绪关怀"

_STATUSES = ("pending", "sent", "skipped", "expired", "cancelled")


def _topic_norm(topic: str) -> str:
    return (topic or "").strip().lower()[:32]


class CareScheduleStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS care_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_key TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        chat_key TEXT NOT NULL DEFAULT '',
        due_at REAL NOT NULL,
        event_at REAL NOT NULL DEFAULT 0,
        topic TEXT NOT NULL DEFAULT '',
        topic_norm TEXT NOT NULL DEFAULT '',
        sentiment TEXT NOT NULL DEFAULT 'neutral',
        source_text TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        sent_at REAL,
        note TEXT NOT NULL DEFAULT '',
        sent_text TEXT NOT NULL DEFAULT '',
        review TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_care_due ON care_schedule(status, due_at);
    CREATE INDEX IF NOT EXISTS idx_care_contact ON care_schedule(contact_key, status);
    """

    def __init__(self, db_path):
        self._db_path = db_path if db_path == ":memory:" else Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        if self._db_path != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path) if self._db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        self._conn.executescript(self._DDL)
        # 幂等迁移（老库补列）：sent_text=话术快照（P2 长期留档，deferred 队列有
        # 终态清理保留期，审计文本以本表为持久口径）；review=拟稿人工审核判定
        # （P3 持久化，审核进度重启不丢）。
        for ddl in (
            "ALTER TABLE care_schedule ADD COLUMN sent_text TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE care_schedule ADD COLUMN review TEXT NOT NULL DEFAULT ''",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 列已存在（新 DDL 或已迁移）
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ── 写入 ────────────────────────────────────────────────────────────
    def add_commitment(
        self,
        commitment: CareCommitment,
        *,
        contact_key: str,
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        dedup_window_days: float = _DEFAULT_DEDUP_WINDOW_DAYS,
    ) -> Optional[int]:
        """落一条关怀待办；置信度不足或近窗同主题已有 pending → 跳过返回 None（绝不抛）。

        B68（实施67 P2-i）：远期到期日 sanity——解析产物 due/event 落在一年开外
        （>370 天）＝几乎必是日期解析错误（实锤：报告长文里的日期被抓成 2027 年
        约定），捕获侧直接拦下（派发侧「远日期 chip」只是显示层，垃圾不该入库）。
        """
        if commitment.confidence < float(min_confidence):
            return None
        now = time.time()
        _far = 370.0 * _DAY
        try:
            if (float(commitment.due_at) > now + _far
                    or float(commitment.event_at) > now + _far):
                logger.info("[care] 远期到期日拦下（疑日期解析错误）：topic=%s due=%s",
                            str(commitment.topic)[:40], commitment.due_at)
                return None
        except Exception:
            pass
        tnorm = _topic_norm(commitment.topic)
        win = float(dedup_window_days) * _DAY
        try:
            with self._lock:
                # 去重：同 contact + 同主题 + due 邻近窗口内已有 pending → 不重复
                dup = self._conn.execute(
                    "SELECT id FROM care_schedule WHERE contact_key = ? AND topic_norm = ?"
                    " AND status = 'pending' AND ABS(due_at - ?) <= ? LIMIT 1",
                    (str(contact_key), tnorm, float(commitment.due_at), win),
                ).fetchone()
                if dup:
                    return None
                cur = self._conn.execute(
                    "INSERT INTO care_schedule (contact_key, platform, account_id, chat_key,"
                    " due_at, event_at, topic, topic_norm, sentiment, source_text, confidence,"
                    " status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        str(contact_key), str(platform), str(account_id), str(chat_key),
                        float(commitment.due_at), float(commitment.event_at),
                        str(commitment.topic)[:80], tnorm, str(commitment.sentiment)[:16],
                        str(commitment.source_text or "")[:160],
                        float(commitment.confidence), now, now,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule add failed: %s", e)
            return None

    def add_from_text(
        self,
        text: str,
        *,
        contact_key: str,
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        now: Optional[float] = None,
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        dedup_window_days: float = _DEFAULT_DEDUP_WINDOW_DAYS,
    ) -> List[int]:
        """便捷接线：抽取一条消息 → 入库。返回新增的 id 列表（去重/低分项不计）。"""
        ids: List[int] = []
        for c in extract_commitments(text, now=now):
            rid = self.add_commitment(
                c, contact_key=contact_key, platform=platform,
                account_id=account_id, chat_key=chat_key,
                min_confidence=min_confidence, dedup_window_days=dedup_window_days)
            if rid:
                ids.append(rid)
        return ids

    # ── 查询 ────────────────────────────────────────────────────────────
    _COLS = [
        "id", "contact_key", "platform", "account_id", "chat_key", "due_at", "event_at",
        "topic", "topic_norm", "sentiment", "source_text", "confidence", "status",
        "created_at", "updated_at", "sent_at", "note", "sent_text", "review",
    ]

    def _rows(self, where: str, params: list, limit: int,
              order_by: str = "due_at ASC") -> List[Dict[str, Any]]:
        try:
            rows = self._conn.execute(
                f"SELECT {', '.join(self._COLS)} FROM care_schedule{where}"
                f" ORDER BY {order_by} LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule query failed: %s", e)
            return []
        return [dict(zip(self._COLS, r)) for r in rows]

    def list_due(self, now: Optional[float] = None, *, limit: int = 100) -> List[Dict[str, Any]]:
        """到期且仍 pending 的待办（due_at <= now），按 due_at 升序。"""
        n = float(now if now is not None else time.time())
        return self._rows(" WHERE status = 'pending' AND due_at <= ?", [n],
                          max(1, min(int(limit), 500)))

    def list_pending(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        return self._rows(" WHERE status = 'pending'", [], max(1, min(int(limit), 500)))

    def list_recent(self, *, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if status:
            return self._rows(" WHERE status = ?", [str(status)], max(1, min(int(limit), 500)))
        return self._rows("", [], max(1, min(int(limit), 500)))

    # 历史/审计视图排序键：sent 行按实际发送时刻，其余按最后状态变化时刻。
    _HISTORY_ORDER = "COALESCE(NULLIF(sent_at, 0), updated_at) DESC, id DESC"

    def list_history(self, *, status: str = "", contact_key: str = "",
                     limit: int = 100) -> List[Dict[str, Any]]:
        """历史视图：按「最近发生」降序（P0 2026-08-03 历史可读性）。

        与 ``list_recent``（due_at 升序=待办语义）互补——翻已发/已跳过记录
        想看的是「最近发生了什么」，最新在前；pending 行按 updated_at
        （捕获/改期时刻）参与排序。``contact_key`` 非空＝「TA 的关怀史」
        （P2 联系人维度）。查询列与语义与 list_recent 完全一致。
        """
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if contact_key:
            clauses.append("contact_key = ?")
            params.append(str(contact_key))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._rows(where, params, max(1, min(int(limit), 500)),
                          order_by=self._HISTORY_ORDER)

    def list_by_contact(self, contact_key: str, *, status: str = "",
                        limit: int = 100) -> List[Dict[str, Any]]:
        """某联系人的关怀待办（P 线健康卡用）。status 空=全部状态。"""
        if status:
            return self._rows(" WHERE contact_key = ? AND status = ?",
                            [str(contact_key), str(status)], max(1, min(int(limit), 500)))
        return self._rows(" WHERE contact_key = ?", [str(contact_key)],
                        max(1, min(int(limit), 500)))

    def recent_sent_texts(self, contact_key: str, *, limit: int = 5) -> List[str]:
        """某联系人最近真发过的关怀话术（B110④ 防重负样本，最新在前）。

        只取非空 ``sent_text`` 的 sent 行（含 dry_run 拟稿快照——坐席可能放行
        同款，句式同样该规避）；排序与 ``list_history`` 同键。喂
        ``build_care_prompt(recent_sent=)`` 当负样本，修「同客户连发五条
        『想你了』式模板」（0826 _527 实录）。
        """
        rows = self._rows(
            " WHERE contact_key = ? AND status = 'sent' AND sent_text != ''",
            [str(contact_key)], max(1, min(int(limit), 20)),
            order_by=self._HISTORY_ORDER)
        return [str(r.get("sent_text") or "").strip() for r in rows
                if str(r.get("sent_text") or "").strip()]

    def count_pending_by_contact(self, contact_key: str) -> int:
        """某联系人当前 pending 关怀数（健康卡 pending_care 信号）。"""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM care_schedule WHERE contact_key = ? AND status = 'pending'",
                (str(contact_key),),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def pending_counts_by_contacts(self, contact_keys) -> Dict[str, int]:
        """批量取多个联系人的 pending 关怀数（避免健康榜 N+1 查询）。"""
        keys = [str(k) for k in (contact_keys or []) if str(k)]
        if not keys:
            return {}
        out: Dict[str, int] = {}
        try:
            # 分块 IN 查询（SQLite 变量上限保守取 500）
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT contact_key, COUNT(*) FROM care_schedule"
                    f" WHERE status = 'pending' AND contact_key IN ({ph})"
                    f" GROUP BY contact_key",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[str(r[0])] = int(r[1])
        except Exception as e:  # noqa: BLE001
            logger.debug("pending_counts_by_contacts failed: %s", e)
        return out

    # ── 状态流转 ────────────────────────────────────────────────────────
    def _set_status(self, sid: int, status: str, *, note: str = "",
                    set_sent_at: bool = False, sent_text: str = "") -> bool:
        if status not in _STATUSES:
            return False
        sets = ["status = ?", "note = ?", "updated_at = ?"]
        params: list = [status, str(note)[:300], time.time()]
        if set_sent_at:
            sets.append("sent_at = ?")
            params.append(time.time())
        if sent_text:
            sets.append("sent_text = ?")
            params.append(str(sent_text)[:2000])
        try:
            with self._lock:
                cur = self._conn.execute(
                    f"UPDATE care_schedule SET {', '.join(sets)}"
                    " WHERE id = ? AND status = 'pending'",
                    (*params, int(sid)),
                )
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule set_status failed: %s", e)
            return False

    def mark_sent(self, sid: int, *, note: str = "", sent_text: str = "") -> bool:
        """标记已发；``sent_text``＝当时发出的话术快照（P2 长期留档，一次
        写入后不可变——deferred 队列按保留期清理后本列仍可审计）。"""
        return self._set_status(sid, "sent", note=note, set_sent_at=True,
                                sent_text=sent_text)

    def backfill_sent_text(self, sid: int, text: str) -> bool:
        """存量回填：只补 sent 行的**空**快照（工具用；已有快照不覆盖，幂等安全）。

        刻意不动 updated_at——回填是元数据修复，不该改写历史序。
        """
        t = str(text or "").strip()
        if not t:
            return False
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET sent_text = ? WHERE id = ?"
                    " AND status = 'sent' AND sent_text = ''",
                    (t[:2000], int(sid)))
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule backfill_sent_text failed: %s", e)
            return False

    # ── P3 2026-08-03：拟稿人工审核持久化（review 列）────────────────────
    def set_review(self, sid: int, verdict: str):
        """拟稿审核判定落行。返回 ``(ok, newly_reviewed)``——newly=该行首评
        （调用方据此决定进程内计数器是否 +1，改判允许但不重复计数）。"""
        v = str(verdict or "").strip().lower()
        if v not in ("like", "dislike"):
            return False, False
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT review FROM care_schedule WHERE id = ?", (int(sid),)
                ).fetchone()
                if not row:
                    return False, False
                newly = not str(row[0] or "")
                self._conn.execute(
                    "UPDATE care_schedule SET review = ? WHERE id = ?",
                    (v, int(sid)))
                self._conn.commit()
                return True, newly
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule set_review failed: %s", e)
            return False, False

    def review_stats(self) -> Dict[str, Any]:
        """审核进度（持久口径，替代进程内计数的「重启清零」）：
        dry 拟稿近 7 天数 / 已审 like/dislike / 未审数。"""
        out = {"drafts_7d": 0, "reviewed": 0, "like": 0, "dislike": 0, "unreviewed": 0}
        try:
            week = time.time() - 7 * _DAY
            r1 = self._conn.execute(
                "SELECT COUNT(*) FROM care_schedule WHERE status = 'sent'"
                " AND note = 'dry_run' AND COALESCE(sent_at, 0) >= ?", (week,)).fetchone()
            out["drafts_7d"] = int(r1[0] or 0) if r1 else 0
            rows = self._conn.execute(
                "SELECT review, COUNT(*) FROM care_schedule WHERE status = 'sent'"
                " AND note = 'dry_run' GROUP BY review").fetchall()
            for rv, n in rows:
                v = str(rv or "")
                if v == "like":
                    out["like"] = int(n)
                elif v == "dislike":
                    out["dislike"] = int(n)
                else:
                    out["unreviewed"] += int(n)
            out["reviewed"] = out["like"] + out["dislike"]
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule review_stats failed: %s", e)
        return out

    def list_unreviewed_drafts(self, *, limit: int = 8) -> List[Dict[str, Any]]:
        """持久待审队列：dry 拟稿、有话术快照、未审，最近在前。

        （审核队列从进程内 metrics 样本升级为本表——重启后待审拟稿不再消失；
        无快照的老 dry 行文本已不可考，不进队列。）
        """
        return self._rows(
            " WHERE status = 'sent' AND note = 'dry_run' AND review = ''"
            " AND sent_text != ''", [],
            max(1, min(int(limit), 50)), order_by=self._HISTORY_ORDER)

    def mark_skipped(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "skipped", note=note)

    def cancel(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "cancelled", note=note)

    def bring_forward(self, sid: int, *, now: Optional[float] = None) -> bool:
        """把一条 pending 待办的 due_at 提前到 now（运营「立即发」）→ 下个派发 tick 即到期。"""
        n = float(now if now is not None else time.time())
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET due_at = ?, updated_at = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (n, n, int(sid)),
                )
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule bring_forward failed: %s", e)
            return False

    def reschedule(self, sid: int, due_at: float) -> bool:
        """把一条 pending 待办改期到 ``due_at``（P7 方案卡「改时间」）。

        与 bring_forward 同族（只动 due_at，仅 pending 可改）；目标时刻的合法性
        （须在未来）由路由层校验——store 保持纯粹的状态操作语义。
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET due_at = ?, updated_at = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (float(due_at), time.time(), int(sid)),
                )
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule reschedule failed: %s", e)
            return False

    def expire_overdue(self, now: Optional[float] = None, *, grace_days: float = 1.0) -> int:
        """把逾期太久仍 pending 的待办标 expired（错过关怀时机，不再补发）。返回数量。"""
        n = float(now if now is not None else time.time())
        cutoff = n - float(grace_days) * _DAY
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET status = 'expired', updated_at = ?"
                    " WHERE status = 'pending' AND due_at < ?",
                    (time.time(), cutoff),
                )
                self._conn.commit()
                return int(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule expire failed: %s", e)
            return 0

    def count_sent_since(self, contact_key: str, since: float) -> int:
        """某联系人在 ``since`` 之后已发送（sent）的主动关怀数（变现配额门控用）。"""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM care_schedule WHERE contact_key = ?"
                " AND status = 'sent' AND sent_at >= ?",
                (str(contact_key), float(since)),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def count(self, *, status: str = "") -> int:
        try:
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM care_schedule WHERE status = ?", (str(status),)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM care_schedule").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ── 健康读数（P0 2026-08-01：/api/care/health「AI 正在听」证据）────────
    def get(self, sid: int) -> Optional[Dict[str, Any]]:
        """按 id 取单条（预览端点用）；不存在/异常 → None。"""
        try:
            rows = self._rows(" WHERE id = ?", [int(sid)], 1)
            return rows[0] if rows else None
        except Exception:
            return None

    def last_created_at(self) -> float:
        """最近一条待办的创建时刻（0=库空/异常）。"""
        try:
            row = self._conn.execute(
                "SELECT MAX(created_at) FROM care_schedule").fetchone()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    def count_created_since(self, since: float) -> int:
        """``since`` 之后新捕获的待办数（含所有状态；证明捕获链活着）。"""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM care_schedule WHERE created_at >= ?",
                (float(since),),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


_singleton: Optional["CareScheduleStore"] = None
_singleton_lock = threading.Lock()


def get_care_schedule_store(db_path=None) -> "CareScheduleStore":
    """进程内单例。首次调用传入 db_path 落库位置；之后忽略入参返回同一实例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CareScheduleStore(db_path or ":memory:")
    return _singleton


__all__ = ["CareScheduleStore", "get_care_schedule_store", "_topic_norm",
           "CRISIS_CARE_TOPIC"]
