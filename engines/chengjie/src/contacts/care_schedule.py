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

# 实施84 P1-2：工作目标到期排进 care 队列的行用 topic_norm=goal:<goal_id> 标记——
# 派发器据此切「目标推进型关怀」模板（框架是"你们在推进的事"而非"对方之前提到过"），
# 且豁免 no_context skip（目标节点本身就是开口理由）。排期侧（care_goal_link）与
# 派发侧（care_dispatcher）共享此前缀。
GOAL_CARE_NORM_PREFIX = "goal:"

# J-8 #182：运营「到点发这句话」（原文直发）行用 topic_norm=verbatim:<sha8> 标记——
# 派发器据此**跳过 LLM 拟稿**、原样把 source_text 送 deferred 队列（仍吃 gate /
# pacing / quiet_hours / kill-switch）；预览端点直接回显原文。与「客户的事（AI 自然
# 关心）」的分界只看这个前缀，不引入新列（老库零迁移）。
VERBATIM_CARE_NORM_PREFIX = "verbatim:"
# 原文直发允许的最大长度（普通 source_text 截 160 是「客户原话摘录」口径；
# 运营手写要发的整句话不能被截断，否则发出去的是半句）。
VERBATIM_MAX_CHARS = 1000

_STATUSES = ("pending", "sent", "skipped", "expired", "cancelled")


def is_verbatim_care(item: Any) -> bool:
    """该 care 行是否为「原文直发」行（按 topic_norm 前缀判，item 为 dict/None 皆安全）。"""
    try:
        return str((item or {}).get("topic_norm") or "").startswith(
            VERBATIM_CARE_NORM_PREFIX)
    except Exception:
        return False


def care_verbatim_text(item: Any) -> str:
    """「原文直发」行到点要发的文本（=source_text 全文）；非 verbatim 行返回 ""。"""
    if not is_verbatim_care(item):
        return ""
    return str((item or {}).get("source_text") or "").strip()


# Q-4（#267 D）：verbatim 行的「安静时段策略」——运营亲手定的时刻默认**尊重人工**
# （keep＝到点就发、不顺延）；关怀页排期时若落在安静时段弹「仍按 01:20 发 / 顺延到
# 08:00」，选顺延的行 topic_norm 追加后缀 ``:defer_quiet``（不引入新列，老库零迁移；
# ``is_verbatim_care`` 只看前缀不受影响）。
VERBATIM_QUIET_DEFER_SUFFIX = ":defer_quiet"
QUIET_POLICIES = ("keep", "defer")


def verbatim_quiet_policy(item: Any) -> str:
    """verbatim 行的安静时段策略：``keep``（默认，人工时刻照发）/ ``defer``（顺延到安静
    时段结束，多条同顺延由派发器错峰）。非 verbatim 行返回 ""（按普通关怀规则）。"""
    if not is_verbatim_care(item):
        return ""
    try:
        tn = str((item or {}).get("topic_norm") or "")
    except Exception:
        return "keep"
    return "defer" if tn.endswith(VERBATIM_QUIET_DEFER_SUFFIX) else "keep"


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
        review TEXT NOT NULL DEFAULT '',
        dry_sampled_at REAL NOT NULL DEFAULT 0
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
            # 实施84 P0-4：试运行拟稿时刻（行保持 pending，不再借 mark_sent 消费待办）
            "ALTER TABLE care_schedule ADD COLUMN dry_sampled_at REAL NOT NULL DEFAULT 0",
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

    def add_scheduled_care(
        self,
        *,
        contact_key: str,
        due_at: float,
        topic: str,
        topic_norm: str,
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        event_at: float = 0.0,
        source_text: str = "",
        sentiment: str = "neutral",
        confidence: float = 1.0,
        dedup_days: float = 30.0,
    ) -> Optional[int]:
        """系统排期入口（实施84 P1-2，目标到期关怀等）：显式 ``topic_norm``。

        与 ``add_commitment`` 的差异：① topic_norm 由调用方定（如
        ``goal:<goal_id>``，派发器据此切模板）；② 去重看「同 contact 同
        topic_norm 近 ``dedup_days`` 天内**任何状态**已有一条」——排期类
        关怀发过/跳过后绝不该被下轮扫描重新排进队列（pending-only 去重
        挡不住这个）。绝不抛。
        """
        now = time.time()
        tnorm = str(topic_norm or "").strip()[:64] or _topic_norm(topic)
        try:
            with self._lock:
                dup = self._conn.execute(
                    "SELECT id FROM care_schedule WHERE contact_key = ?"
                    " AND topic_norm = ? AND (status = 'pending'"
                    " OR created_at >= ?) LIMIT 1",
                    (str(contact_key), tnorm,
                     now - max(0.0, float(dedup_days)) * _DAY),
                ).fetchone()
                if dup:
                    return None
                cur = self._conn.execute(
                    "INSERT INTO care_schedule (contact_key, platform, account_id,"
                    " chat_key, due_at, event_at, topic, topic_norm, sentiment,"
                    " source_text, confidence, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        str(contact_key), str(platform), str(account_id),
                        str(chat_key), float(due_at),
                        float(event_at or due_at), str(topic)[:80], tnorm,
                        str(sentiment)[:16], str(source_text or "")[:160],
                        float(confidence), now, now,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule add_scheduled_care failed: %s", e)
            return None

    def add_verbatim(
        self,
        *,
        contact_key: str,
        due_at: float,
        text: str,
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        quiet_policy: str = "keep",
    ) -> Optional[int]:
        """J-8 #182：运营「到点发这句话」——原文直发行入队。

        ``text`` 就是到点要发出去的整句话（≤ ``VERBATIM_MAX_CHARS``，**不截 160**）；
        ``topic`` 存一行摘要（列表卡片显示用），``topic_norm=verbatim:<sha8>``
        让派发器/预览识别并绕过 LLM。手动可信：不做去重、confidence=1.0。绝不抛。
        ``quiet_policy``（Q-4 #267 D）：``keep``（默认）＝人工时刻照发不顺延；
        ``defer``＝落在安静时段则顺延到结束并错峰（topic_norm 追加 ``:defer_quiet``）。
        """
        import hashlib
        body = str(text or "").strip()
        if not body:
            return None
        body = body[:VERBATIM_MAX_CHARS]
        summary = " ".join(body.split())[:80]
        tnorm = (VERBATIM_CARE_NORM_PREFIX
                 + hashlib.sha1(body.encode("utf-8")).hexdigest()[:8])
        if str(quiet_policy or "keep").strip().lower() == "defer":
            tnorm += VERBATIM_QUIET_DEFER_SUFFIX
        now = time.time()
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO care_schedule (contact_key, platform, account_id,"
                    " chat_key, due_at, event_at, topic, topic_norm, sentiment,"
                    " source_text, confidence, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'neutral', ?, 1.0, 'pending', ?, ?)",
                    (
                        str(contact_key), str(platform), str(account_id),
                        str(chat_key), float(due_at), float(due_at),
                        summary, tnorm, body, now, now,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule add_verbatim failed: %s", e)
            return None

    # ── 查询 ────────────────────────────────────────────────────────────
    _COLS = [
        "id", "contact_key", "platform", "account_id", "chat_key", "due_at", "event_at",
        "topic", "topic_norm", "sentiment", "source_text", "confidence", "status",
        "created_at", "updated_at", "sent_at", "note", "sent_text", "review",
        "dry_sampled_at",
    ]

    # 实施84 P0-4：「试运行拟稿」的两种形态——旧库（sent+note=dry_run，2026-08 前的
    # 消费式语义）与新库（pending+dry_sampled_at>0，快照不消费待办）。审核队列/进度/
    # 负样本三个消费面共用此谓词，保证升级窗口两代数据都可见。
    _DRY_DRAFT_WHERE = ("((status = 'sent' AND note = 'dry_run')"
                        " OR (status = 'pending' AND dry_sampled_at > 0))")

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

        只取非空 ``sent_text`` 的行：sent 行（含旧语义 dry_run 拟稿快照）+
        新语义 pending 且 ``dry_sampled_at>0`` 的试运行快照——坐席可能放行
        同款，句式同样该规避；排序与 ``list_history`` 同键。喂
        ``build_care_prompt(recent_sent=)`` 当负样本，修「同客户连发五条
        『想你了』式模板」（0826 _527 实录）。
        """
        rows = self._rows(
            " WHERE contact_key = ? AND sent_text != ''"
            " AND (status = 'sent' OR dry_sampled_at > 0)",
            [str(contact_key)], max(1, min(int(limit), 20)),
            order_by=self._HISTORY_ORDER)
        return [str(r.get("sent_text") or "").strip() for r in rows
                if str(r.get("sent_text") or "").strip()]

    def disliked_texts(self, *, limit: int = 20) -> List[str]:
        """运营点过 👎 的拟稿/话术快照（持久口径，最新在前；实施84 P0-6）。

        metrics 侧的会话级黑名单**刻意不持久化**（既有设计：主观判断重启重审），
        但 care 自己的审核判定早已落 ``review`` 列——派发防重直接读这份持久
        判定，重启后黑名单不再清零。任何状态的行都算（拟稿被 👎 后行可能
        过期/发出，判定依然有效）。
        """
        rows = self._rows(
            " WHERE review = 'dislike' AND sent_text != ''", [],
            max(1, min(int(limit), 50)), order_by=self._HISTORY_ORDER)
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

    def mark_dry_sampled(self, sid: int, *, sent_text: str = "",
                         now: Optional[float] = None) -> bool:
        """试运行拟稿快照（实施84 P0-4）：**不消费 pending**。

        旧行为 ``mark_sent(note="dry_run")`` 会把待办吃掉——试运行期间「演习」
        过的约定，切真发后不再派发（坐席以为 dry 只是预览，实际是消耗）。
        新语义：行保持 pending（转真发后仍按期发出；逾期由 ``expire_overdue``
        正常收口），本方法只记快照与时刻，供重拟冷却与持久审核队列使用。
        ``now``＝调用方时钟（派发器透传自己的 tick 时刻，重拟冷却窗才与
        派发判定同一把尺）；缺省墙钟。
        """
        try:
            with self._lock:
                n = float(now if now is not None else time.time())
                cur = self._conn.execute(
                    "UPDATE care_schedule SET dry_sampled_at = ?, sent_text = ?,"
                    " updated_at = ? WHERE id = ? AND status = 'pending'",
                    (n, str(sent_text or "")[:2000], n, int(sid)))
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule mark_dry_sampled failed: %s", e)
            return False

    # ── M-1 A（#218，2026-09-06）：LLM 拟稿强制预览 → 行留 pending 待运营确认 ─────
    HOLD_NOTE_PREFIX = "hold:"

    @classmethod
    def hold_reason(cls, item: Any) -> str:
        """该 pending 行是否处于「待运营确认」态（note=``hold:<reason>``）→ 原因码；否则 ""。"""
        try:
            n = str((item or {}).get("note") or "")
        except Exception:
            return ""
        if not n.startswith(cls.HOLD_NOTE_PREFIX):
            return ""
        return n[len(cls.HOLD_NOTE_PREFIX):].strip()

    def mark_hold_for_preview(self, sid: int, *, sent_text: str, reason: str,
                              now: Optional[float] = None) -> bool:
        """LLM 拟稿命中「首次真发 / 含地名 / 含人名」→ **不发**，草稿快照落 ``sent_text``、
        ``note=hold:<reason>``，行保持 pending：派发器此后跳过该行，直到运营在面板上
        「就这样发 / 改一改再发」（``deliver_text`` → mark_sent）或跳过 / 逾期过期。
        刻意**不**碰 ``dry_sampled_at``：held 行不该混进试运行审核队列（那是 👍/👎
        质量评审，这里是「发不发」的人工确认，两个动作两个入口）。"""
        r = str(reason or "").strip()[:80] or "preview"
        try:
            with self._lock:
                n = float(now if now is not None else time.time())
                cur = self._conn.execute(
                    "UPDATE care_schedule SET note = ?, sent_text = ?,"
                    " updated_at = ? WHERE id = ? AND status = 'pending'",
                    (self.HOLD_NOTE_PREFIX + r, str(sent_text or "")[:2000], n, int(sid)))
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule mark_hold_for_preview failed: %s", e)
            return False

    # ── N-1 A（#243，2026-09-08）：「立即发」永久失败 → 行留 pending、note=fail:<原因> ──
    FAIL_NOTE_PREFIX = "fail:"

    @classmethod
    def fail_reason(cls, item: Any) -> str:
        """该 pending 行上一次「立即发」是否失败（note=``fail:<reason>``）→ 原因码；否则 ""。"""
        try:
            n = str((item or {}).get("note") or "")
        except Exception:
            return ""
        if not n.startswith(cls.FAIL_NOTE_PREFIX):
            return ""
        return n[len(cls.FAIL_NOTE_PREFIX):].strip()

    def mark_send_failed(self, sid: int, reason: str, *,
                         now: Optional[float] = None) -> bool:
        """运营「立即发」投递失败（平台拒收 / 通道异常 / 队列关）：**不消费待办**。

        行保持 pending、``note=fail:<reason>``——卡片据此显「失败（原因）」且「立即发」
        可再点；到期派发循环下一拍照常重试（行仍到期）。成功发出时 ``mark_sent``
        覆盖 note。刻意不碰 ``sent_text`` / ``dry_sampled_at``。"""
        r = str(reason or "").strip()[:120] or "send_failed"
        try:
            with self._lock:
                n = float(now if now is not None else time.time())
                cur = self._conn.execute(
                    "UPDATE care_schedule SET note = ?, updated_at = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (self.FAIL_NOTE_PREFIX + r, n, int(sid)))
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule mark_send_failed failed: %s", e)
            return False

    def has_real_sent(self, contact_key: str) -> bool:
        """该联系人此前是否**真发**过关怀（sent 且 note 以 deferred:/manual: 开头；
        dry_run 消费式旧行不算）。首次真发 → LLM 拟稿强制预览。异常按「已发过」
        （fail-open：预览是保守动作，判不出时不该把每条都扣下）。"""
        try:
            row = self._conn.execute(
                "SELECT 1 FROM care_schedule WHERE contact_key = ? AND status = 'sent'"
                " AND (note LIKE 'deferred:%' OR note LIKE 'manual:%') LIMIT 1",
                (str(contact_key),),
            ).fetchone()
            return row is not None
        except Exception:
            return True

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
        dry 拟稿近 7 天数 / 已审 like/dislike / 未审数。两代 dry 形态都计
        （见 ``_DRY_DRAFT_WHERE``）。"""
        out = {"drafts_7d": 0, "reviewed": 0, "like": 0, "dislike": 0, "unreviewed": 0}
        try:
            week = time.time() - 7 * _DAY
            r1 = self._conn.execute(
                "SELECT COUNT(*) FROM care_schedule WHERE"
                " (status = 'sent' AND note = 'dry_run' AND COALESCE(sent_at, 0) >= ?)"
                " OR dry_sampled_at >= ?", (week, week)).fetchone()
            out["drafts_7d"] = int(r1[0] or 0) if r1 else 0
            rows = self._conn.execute(
                f"SELECT review, COUNT(*) FROM care_schedule WHERE"
                f" {self._DRY_DRAFT_WHERE} GROUP BY review").fetchall()
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
        无快照的老 dry 行文本已不可考，不进队列。两代 dry 形态都进队列，
        见 ``_DRY_DRAFT_WHERE``。）
        """
        return self._rows(
            f" WHERE {self._DRY_DRAFT_WHERE} AND review = ''"
            " AND sent_text != ''", [],
            max(1, min(int(limit), 50)), order_by=self._HISTORY_ORDER)

    def mark_skipped(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "skipped", note=note)

    def cancel(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "cancelled", note=note)

    # N-1 B（#243，D-N4 ②）：提前到期的行记 note=fwd:<时刻> → 卡片显「已提前（hh:mm）」
    FWD_NOTE_PREFIX = "fwd:"

    @classmethod
    def forwarded_at(cls, item: Any) -> float:
        """该 pending 行若被「提前到期」过 → 提前时刻（epoch）；否则 0。"""
        try:
            n = str((item or {}).get("note") or "")
            if not n.startswith(cls.FWD_NOTE_PREFIX):
                return 0.0
            return float(n[len(cls.FWD_NOTE_PREFIX):].strip() or 0)
        except Exception:
            return 0.0

    def bring_forward(self, sid: int, *, now: Optional[float] = None) -> bool:
        """把一条 pending 待办的 due_at 提前到 now（运营「立即发」的**兜底**路径）→ 下个
        派发 tick 即到期；同时 ``note=fwd:<now>``（覆盖上次 ``fail:*``，不碰 ``hold:*`` 行
        ——待确认行由路由层先挡）。A 项的同步直投才是主路径（``CareDispatcher.send_now``）。"""
        n = float(now if now is not None else time.time())
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET due_at = ?, updated_at = ?,"
                    " note = CASE WHEN note LIKE 'hold:%' THEN note ELSE ? END"
                    " WHERE id = ? AND status = 'pending'",
                    (n, n, self.FWD_NOTE_PREFIX + str(int(n)), int(sid)),
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
           "CRISIS_CARE_TOPIC", "GOAL_CARE_NORM_PREFIX",
           "VERBATIM_CARE_NORM_PREFIX", "VERBATIM_MAX_CHARS",
           "VERBATIM_QUIET_DEFER_SUFFIX", "QUIET_POLICIES",
           "is_verbatim_care", "care_verbatim_text", "verbatim_quiet_policy"]
