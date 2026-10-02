"""Telegram 群成员提取 —— 成员库 + 提取任务 持久层（SQLite，线程安全）。

给「多号进群、限速分批拉取群成员入库、每天控量」建持久层。一行成员 =
``(group_id, user_id)`` 唯一（跨号/跨天/跨任务去重的天然主键）；一行任务 =
一次「某群 × 一批号 × 过滤器 × 配额」的提取作业及其进度。

设计（对齐 ``persona_media_store`` 的线程安全 SQLite + 模块级单例范式）：
- 单连接 + Lock，``check_same_thread=False``，WAL；``:memory:`` 供单测。
- 成员写入走 ``INSERT OR IGNORE`` → 幂等去重（返回真正新增数 vs 命中已存在数）。
- **每日提取配额自带计数**（``count_extracted_since``：按 source_account_id + extracted_at
  聚合），刻意**不写 outreach_log**——那是「触达账本」，reply_latency / value_report 等
  分析都读它，把「只读提取」记进去会污染这些指标。outreach_log 严格留给「触达」步骤。
- 挑选/过滤/分片逻辑不在本模块（纯函数，见 ``group_member_extract.py``）——本模块只管存取。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("ai_chat_assistant.group_members_store")

DEFAULT_DB_PATH = "config/group_members.db"

# 提取过滤器
FILTER_ALL = "all"                      # 全部成员
FILTER_SPOKE = "spoke"                  # 只要在群里发过言的人
FILTER_SPOKE_NO_ADMIN = "spoke_no_admin"  # 发过言且非管理员（默认，最贴「可聊的活人」）
_VALID_FILTERS = (FILTER_ALL, FILTER_SPOKE, FILTER_SPOKE_NO_ADMIN)

# 任务状态机
JOB_DRAFT = "draft"
JOB_RUNNING = "running"
JOB_PAUSED = "paused"
JOB_DONE = "done"
JOB_STOPPED = "stopped"
JOB_ERROR = "error"

# 成员触达状态。提取只写 none。开口步骤（group_member_outreach）才往下流转。
# queued=今天排上了；approved=文案定了、可以由调度器发；sending=已占坑、消息尚未落定
# （进程中断由 reap_stale_sending 标 skipped，不重发）。
OUTREACH_NONE = "none"
OUTREACH_QUEUED = "queued"
OUTREACH_APPROVED = "approved"
OUTREACH_SENDING = "sending"
OUTREACH_SENT = "sent"
OUTREACH_REPLIED = "replied"
OUTREACH_SKIPPED = "skipped"
OUTREACH_BLOCKED = "blocked"
# closed=开口 + 一次跟进都没回音，封存；之后对方若主动回话仍会翻成 replied。
OUTREACH_CLOSED = "closed"
# 这些状态表示「这个人已经被某个号碰过」，跨群不再另开口。
OUTREACH_TOUCHED = (
    OUTREACH_QUEUED, OUTREACH_APPROVED, OUTREACH_SENDING, OUTREACH_SENT,
    OUTREACH_REPLIED, OUTREACH_SKIPPED, OUTREACH_BLOCKED, OUTREACH_CLOSED,
)
# 开口急停的全局行（tg_outreach_holds.account_id）。
OUTREACH_HOLD_ALL = "*"
# 每号开口方式：manual=坐席逐条点发；approve=AI 拟稿、坐席批准后调度器发；auto=全自动。
OUTREACH_MODE_MANUAL = "manual"
OUTREACH_MODE_APPROVE = "approve"
OUTREACH_MODE_AUTO = "auto"
OUTREACH_MODES = (OUTREACH_MODE_MANUAL, OUTREACH_MODE_APPROVE, OUTREACH_MODE_AUTO)

_DDL = """
CREATE TABLE IF NOT EXISTS tg_group_members (
    group_id          TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    username          TEXT NOT NULL DEFAULT '',
    first_name        TEXT NOT NULL DEFAULT '',
    last_name         TEXT NOT NULL DEFAULT '',
    is_admin          INTEGER NOT NULL DEFAULT 0,
    is_bot            INTEGER NOT NULL DEFAULT 0,
    spoke             INTEGER NOT NULL DEFAULT 0,
    last_spoke_ts     REAL NOT NULL DEFAULT 0,
    group_title       TEXT NOT NULL DEFAULT '',
    source_account_id TEXT NOT NULL DEFAULT '',
    job_id            TEXT NOT NULL DEFAULT '',
    batch_id          TEXT NOT NULL DEFAULT '',
    outreach_state    TEXT NOT NULL DEFAULT 'none',
    extracted_at      REAL NOT NULL DEFAULT 0,
    score             INTEGER NOT NULL DEFAULT 0,
    access_hash       TEXT NOT NULL DEFAULT '',
    hash_account_id   TEXT NOT NULL DEFAULT '',
    outreach_at       REAL NOT NULL DEFAULT 0,
    outreach_error    TEXT NOT NULL DEFAULT '',
    outreach_account_id TEXT NOT NULL DEFAULT '',
    last_msg_text     TEXT NOT NULL DEFAULT '',
    last_msg_ts       REAL NOT NULL DEFAULT 0,
    lang_code         TEXT NOT NULL DEFAULT '',
    opener_text       TEXT NOT NULL DEFAULT '',
    opener_source     TEXT NOT NULL DEFAULT '',
    approved_at       REAL NOT NULL DEFAULT 0,
    opener_persona    TEXT NOT NULL DEFAULT '',
    followup_at       REAL NOT NULL DEFAULT 0,
    followup_text     TEXT NOT NULL DEFAULT '',
    replied_at        REAL NOT NULL DEFAULT 0,
    reply_text        TEXT NOT NULL DEFAULT '',
    opener_variant    TEXT NOT NULL DEFAULT '',
    answered_at       REAL NOT NULL DEFAULT 0,
    last_in_at        REAL NOT NULL DEFAULT 0,
    last_out_at       REAL NOT NULL DEFAULT 0,
    stalled_flagged_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);
-- 开口急停 / 风控熔断 / 开口方式（按号；account_id='*' 表示全部号今天停止开口）。
-- 与提取任务分开：停提取不停开口，停开口不停提取。
CREATE TABLE IF NOT EXISTS tg_outreach_holds (
    account_id  TEXT NOT NULL PRIMARY KEY,
    paused      INTEGER NOT NULL DEFAULT 0,
    flood_until REAL NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL DEFAULT 0,
    mode        TEXT NOT NULL DEFAULT 'manual',
    last_flood_at REAL NOT NULL DEFAULT 0,
    next_auto_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gm_group ON tg_group_members(group_id, outreach_state);
CREATE INDEX IF NOT EXISTS idx_gm_acct_day ON tg_group_members(source_account_id, extracted_at);
-- ⚠ 依赖「迁移才补上」的列（如 score）的索引**绝不能**放这里：executescript 对**旧表**
--   （score 尚未 ALTER 进来）建该索引会 `no such column: score` → 整个建库抛异常 → store=None
--   （2026-08-12 生产实锤：旧库 12:09/12:40 boot 建库失败，功能静默不可用）。
--   这类索引一律在 `_migrate()` 补列**之后**创建（见 _POST_MIGRATE_INDEXES）。

CREATE TABLE IF NOT EXISTS tg_extract_jobs (
    job_id                TEXT NOT NULL PRIMARY KEY,
    group_id              TEXT NOT NULL DEFAULT '',
    group_title           TEXT NOT NULL DEFAULT '',
    account_ids           TEXT NOT NULL DEFAULT '[]',
    filter                TEXT NOT NULL DEFAULT 'spoke_no_admin',
    daily_cap_per_account INTEGER NOT NULL DEFAULT 200,
    scan_limit            INTEGER NOT NULL DEFAULT 3000,
    group_daily_cap       INTEGER NOT NULL DEFAULT 0,
    global_daily_cap      INTEGER NOT NULL DEFAULT 0,
    shard_index           INTEGER NOT NULL DEFAULT 0,
    num_shards            INTEGER NOT NULL DEFAULT 1,
    batch_ref             TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'draft',
    cursor                TEXT NOT NULL DEFAULT '{}',
    stop_requested        INTEGER NOT NULL DEFAULT 0,
    pulled_total          INTEGER NOT NULL DEFAULT 0,
    dedup_skipped         INTEGER NOT NULL DEFAULT 0,
    admins_excluded       INTEGER NOT NULL DEFAULT 0,
    floodwaits            INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT NOT NULL DEFAULT '',
    created_by            TEXT NOT NULL DEFAULT '',
    created_at            REAL NOT NULL DEFAULT 0,
    updated_at            REAL NOT NULL DEFAULT 0,
    finished_at           REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gm_jobs_status ON tg_extract_jobs(status, updated_at);
"""

_MEMBER_COLS = (
    "group_id", "user_id", "username", "first_name", "last_name",
    "is_admin", "is_bot", "spoke", "last_spoke_ts", "group_title",
    "source_account_id", "job_id", "batch_id", "outreach_state", "extracted_at",
    "score", "access_hash", "hash_account_id", "outreach_at", "outreach_error",
    "outreach_account_id", "last_msg_text", "last_msg_ts", "lang_code",
    "opener_text", "opener_source", "approved_at",
    "opener_persona", "followup_at", "followup_text",
    "replied_at", "reply_text", "opener_variant", "answered_at", "last_msg_id",
)

# update_job 允许热改的字段（其余为不可变身份/审计字段）。
_JOB_UPDATABLE = {
    "group_id", "group_title", "filter", "daily_cap_per_account", "scan_limit",
    "status", "cursor", "stop_requested", "last_error", "finished_at",
}
_JOB_JSON_COLS = {"account_ids", "cursor"}


class GroupMembersStore:
    """Telegram 群成员库 + 提取任务注册表（线程安全 SQLite）。"""

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """存量库补列（幂等；调用方已持锁）。失败不抛——建表已成，缺列走降级。"""
        _adds: Sequence[Tuple[str, str, str]] = (
            ("tg_extract_jobs", "shard_index", "INTEGER NOT NULL DEFAULT 0"),
            ("tg_extract_jobs", "num_shards", "INTEGER NOT NULL DEFAULT 1"),
            ("tg_extract_jobs", "batch_ref", "TEXT NOT NULL DEFAULT ''"),
            ("tg_extract_jobs", "group_daily_cap", "INTEGER NOT NULL DEFAULT 0"),
            ("tg_extract_jobs", "global_daily_cap", "INTEGER NOT NULL DEFAULT 0"),
            ("tg_group_members", "score", "INTEGER NOT NULL DEFAULT 0"),
            ("tg_group_members", "access_hash", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "hash_account_id", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "outreach_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "outreach_error", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "outreach_account_id", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "last_msg_text", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "last_msg_ts", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "lang_code", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "opener_text", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "opener_source", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "approved_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_outreach_holds", "mode", "TEXT NOT NULL DEFAULT 'manual'"),
            ("tg_group_members", "opener_persona", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "followup_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "followup_text", "TEXT NOT NULL DEFAULT ''"),
            ("tg_outreach_holds", "last_flood_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_outreach_holds", "next_auto_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "replied_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "reply_text", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "opener_variant", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "answered_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "last_in_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "last_out_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "stalled_flagged_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "last_msg_id", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "gtouch_state", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "gtouch_text", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "gtouch_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "gtouch_account_id", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "gtouch_error", "TEXT NOT NULL DEFAULT ''"),
            ("tg_outreach_holds", "declared_age_days", "REAL NOT NULL DEFAULT 0"),
            ("tg_outreach_holds", "declared_age_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "gtouch_msg_id", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "gtouch_reply_at", "REAL NOT NULL DEFAULT 0"),
            ("tg_group_members", "gtouch_reply_text", "TEXT NOT NULL DEFAULT ''"),
            ("tg_group_members", "gtouch_reply_self", "INTEGER NOT NULL DEFAULT 0"),
        )
        for table, col, decl in _adds:
            try:
                have = {r[1] for r in self._conn.execute("PRAGMA table_info(%s)" % table)}
                if col not in have:
                    self._conn.execute(
                        "ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
            except Exception:
                logger.debug("[group_members] 迁移 %s.%s 跳过", table, col, exc_info=True)
        # 依赖迁移列的索引：**必须**在补列之后建（放进 _DDL 会对旧表引用未存在的列而崩库，
        # 见 _DDL 顶部告警）。IF NOT EXISTS 幂等；单条失败软跳过不影响建库。
        for _idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_gm_score ON tg_group_members(group_id, score)",
            "CREATE INDEX IF NOT EXISTS idx_gm_hash_acct "
            "ON tg_group_members(hash_account_id, outreach_state)",
            "CREATE INDEX IF NOT EXISTS idx_gm_out_acct "
            "ON tg_group_members(outreach_account_id, outreach_state)",
            "CREATE INDEX IF NOT EXISTS idx_gm_user_gtouch "
            "ON tg_group_members(user_id, gtouch_state)",
            "CREATE INDEX IF NOT EXISTS idx_gm_gtouch_msg "
            "ON tg_group_members(group_id, gtouch_msg_id)",
        ):
            try:
                self._conn.execute(_idx_sql)
            except Exception:
                logger.debug("[group_members] 迁移索引跳过: %s", _idx_sql, exc_info=True)

    # ── 成员写入/查询 ────────────────────────────────────────────────────────

    def record_members(self, rows: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
        """批量写入成员（``INSERT OR IGNORE`` 去重）。

        返回 ``(inserted, skipped)``：inserted=真正新增（此前不存在），
        skipped=命中已存在（``(group_id,user_id)`` 撞主键）。逐行探 rowcount 判定
        （executemany + OR IGNORE 的 rowcount 不可靠）。缺 group_id/user_id 的行跳过。
        """
        inserted = 0
        skipped = 0
        now = time.time()
        with self._lock:
            for r in rows:
                gid = str(r.get("group_id") or "").strip()
                uid = str(r.get("user_id") or "").strip()
                if not gid or not uid:
                    continue
                vals = (
                    gid, uid,
                    str(r.get("username") or ""),
                    str(r.get("first_name") or ""),
                    str(r.get("last_name") or ""),
                    1 if r.get("is_admin") else 0,
                    1 if r.get("is_bot") else 0,
                    1 if r.get("spoke") else 0,
                    float(r.get("last_spoke_ts") or 0.0),
                    str(r.get("group_title") or ""),
                    str(r.get("source_account_id") or ""),
                    str(r.get("job_id") or ""),
                    str(r.get("batch_id") or ""),
                    str(r.get("outreach_state") or OUTREACH_NONE),
                    float(r.get("extracted_at") or now),
                    int(r.get("score") or 0),
                    str(r.get("access_hash") or ""),
                    str(r.get("hash_account_id") or ""),
                    float(r.get("outreach_at") or 0.0),
                    str(r.get("outreach_error") or "")[:200],
                    str(r.get("outreach_account_id") or ""),
                    str(r.get("last_msg_text") or "")[:200],
                    float(r.get("last_msg_ts") or 0.0),
                    str(r.get("lang_code") or "")[:16],
                    str(r.get("opener_text") or "")[:500],
                    str(r.get("opener_source") or "")[:16],
                    float(r.get("approved_at") or 0.0),
                    str(r.get("opener_persona") or "")[:64],
                    float(r.get("followup_at") or 0.0),
                    str(r.get("followup_text") or "")[:500],
                    float(r.get("replied_at") or 0.0),
                    str(r.get("reply_text") or "")[:200],
                    str(r.get("opener_variant") or "")[:16],
                    float(r.get("answered_at") or 0.0),
                    str(r.get("last_msg_id") or "")[:24],
                )
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO tg_group_members (%s) VALUES (%s)"
                    % (",".join(_MEMBER_COLS), ",".join(["?"] * len(_MEMBER_COLS))),
                    vals,
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
                    # 同人已在库里但还没有「这个号能私聊」的凭证时，补上。
                    # 已有凭证不覆盖——access_hash 跟会话绑定，不能换成另一个号的。
                    ah = str(r.get("access_hash") or "").strip()
                    hacct = str(r.get("hash_account_id") or "").strip()
                    if ah and ah != "0" and hacct:
                        self._conn.execute(
                            "UPDATE tg_group_members SET access_hash=?, hash_account_id=? "
                            "WHERE group_id=? AND user_id=? AND access_hash=''",
                            (ah, hacct, gid, uid),
                        )
                    # 再扫到同一个人：他最近说的话 / 发言时间 / 语言取更新的那份
                    # （AI 开场靠这些写出针对性；旧值只会更陈旧）。
                    lm_text = str(r.get("last_msg_text") or "")[:200]
                    lm_ts = float(r.get("last_msg_ts") or 0.0)
                    lang = str(r.get("lang_code") or "")[:16]
                    lm_id = str(r.get("last_msg_id") or "")[:24]
                    if lm_id and lm_ts:
                        # 群里接话要回复到具体那条：id 跟着更新的那句走（同一 UPDATE 里读的都是旧值）
                        self._conn.execute(
                            "UPDATE tg_group_members SET last_msg_id=? "
                            "WHERE group_id=? AND user_id=? AND ?>=last_msg_ts",
                            (lm_id, gid, uid, lm_ts))
                    if lm_text or lm_ts or lang:
                        self._conn.execute(
                            "UPDATE tg_group_members SET "
                            "last_msg_text=CASE WHEN ?!='' THEN ? ELSE last_msg_text END, "
                            "last_msg_ts=CASE WHEN ?>last_msg_ts THEN ? ELSE last_msg_ts END, "
                            "last_spoke_ts=CASE WHEN ?>last_spoke_ts THEN ? ELSE last_spoke_ts END, "
                            "lang_code=CASE WHEN ?!='' THEN ? ELSE lang_code END "
                            "WHERE group_id=? AND user_id=?",
                            (lm_text, lm_text, lm_ts, lm_ts, lm_ts, lm_ts, lang, lang,
                             gid, uid),
                        )
                    # 先按「全部成员」入库时没发过言、后来说话了 → 补上发言标记，否则永远进不了开口候选
                    if r.get("spoke"):
                        self._conn.execute(
                            "UPDATE tg_group_members SET spoke=1, score=MAX(score, ?) "
                            "WHERE group_id=? AND user_id=? AND spoke=0",
                            (int(r.get("score") or 0), gid, uid),
                        )
            self._conn.commit()
        return inserted, skipped

    def count_extracted_since(self, account_id: str, since_ts: float) -> int:
        """某号自 ``since_ts`` 起「首次由它入库」的成员数（每日配额判定的口径）。

        用 ``source_account_id`` 记「谁首先拉到这个人」——被别的号先拉走的重复人
        不算进本号今日配额，避免多号协同时配额虚高。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members "
                "WHERE source_account_id=? AND extracted_at>=?",
                (str(account_id), float(since_ts)),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_extracted_for_group_since(self, group_id: str, since_ts: float) -> int:
        """某群自 ``since_ts`` 起入库的成员数（**跨全部号**；每群每日配额判定口径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members "
                "WHERE group_id=? AND extracted_at>=?",
                (str(group_id), float(since_ts)),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_extracted_all_since(self, since_ts: float) -> int:
        """全系统自 ``since_ts`` 起入库的成员数（全局每日配额判定口径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE extracted_at>=?",
                (float(since_ts),),
            ).fetchone()
        return int(row[0]) if row else 0

    def list_members(self, group_id: str, *, only: str = "", q: str = "",
                     sort: str = "recent", limit: int = 500,
                     offset: int = 0) -> List[Dict[str, Any]]:
        """列群成员。``only``: ''=全部 / 'spoke' / 'spoke_no_admin' / 'admins'；
        ``q``: 名字/用户名模糊；``sort``: 'recent'(默认，近发言优先) / 'score'(可聊度优先)。"""
        where = ["group_id=?"]
        args: List[Any] = [str(group_id)]
        only = (only or "").strip()
        if only == "spoke":
            where.append("spoke=1")
        elif only == "spoke_no_admin":
            where.append("spoke=1 AND is_admin=0")
        elif only == "admins":
            where.append("is_admin=1")
        q = (q or "").strip()
        if q:
            where.append("(username LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
            like = "%%%s%%" % q
            args += [like, like, like]
        order = ("score DESC, last_spoke_ts DESC" if sort == "score"
                 else "last_spoke_ts DESC, extracted_at DESC")
        sql = ("SELECT * FROM tg_group_members WHERE %s ORDER BY %s LIMIT ? OFFSET ?"
               % (" AND ".join(where), order))
        args += [max(1, min(int(limit), 5000)), max(0, int(offset))]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._member_to_dict(r) for r in rows]

    def count_members(self, group_id: str, *, only: str = "") -> int:
        where = ["group_id=?"]
        args: List[Any] = [str(group_id)]
        only = (only or "").strip()
        if only == "spoke":
            where.append("spoke=1")
        elif only == "spoke_no_admin":
            where.append("spoke=1 AND is_admin=0")
        elif only == "admins":
            where.append("is_admin=1")
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE %s" % " AND ".join(where),
                args,
            ).fetchone()
        return int(row[0]) if row else 0

    def group_summaries(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        """按群聚合：总数 / 发言数 / 管理员数（供管理台列表与 ops 卡）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, "
                "MAX(group_title) AS group_title, "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN spoke=1 THEN 1 ELSE 0 END) AS spoke, "
                "SUM(CASE WHEN is_admin=1 THEN 1 ELSE 0 END) AS admins, "
                "MAX(extracted_at) AS last_extract "
                "FROM tg_group_members GROUP BY group_id "
                "ORDER BY last_extract DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            {"group_id": r["group_id"], "group_title": r["group_title"] or "",
             "total": int(r["total"] or 0), "spoke": int(r["spoke"] or 0),
             "admins": int(r["admins"] or 0),
             "last_extract": float(r["last_extract"] or 0.0)}
            for r in rows
        ]

    # ── 任务 CRUD ────────────────────────────────────────────────────────────

    def create_job(self, *, group_id: str, account_ids: Sequence[str],
                   filter: str = FILTER_SPOKE_NO_ADMIN,
                   daily_cap_per_account: int = 200, scan_limit: int = 3000,
                   group_title: str = "", created_by: str = "",
                   status: str = JOB_RUNNING,
                   shard_index: int = 0, num_shards: int = 1,
                   batch_ref: str = "", group_daily_cap: int = 0,
                   global_daily_cap: int = 0) -> Dict[str, Any]:
        job_id = "gmjob_" + uuid.uuid4().hex[:12]
        now = time.time()
        flt = filter if filter in _VALID_FILTERS else FILTER_SPOKE_NO_ADMIN
        with self._lock:
            self._conn.execute(
                "INSERT INTO tg_extract_jobs "
                "(job_id, group_id, group_title, account_ids, filter, "
                " daily_cap_per_account, scan_limit, group_daily_cap, "
                " global_daily_cap, shard_index, num_shards, "
                " batch_ref, status, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, str(group_id), str(group_title),
                 json.dumps(list(account_ids), ensure_ascii=False), flt,
                 int(daily_cap_per_account), int(scan_limit),
                 int(group_daily_cap), int(global_daily_cap),
                 int(shard_index), int(num_shards), str(batch_ref),
                 str(status), str(created_by), now, now),
            )
            self._conn.commit()
        return self.get_job(job_id) or {}

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tg_extract_jobs WHERE job_id=?", (str(job_id),)
            ).fetchone()
        return self._job_to_dict(row) if row else None

    def list_jobs(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_extract_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._job_to_dict(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        sets: List[str] = []
        args: List[Any] = []
        for k, v in fields.items():
            if k not in _JOB_UPDATABLE:
                continue
            if k in _JOB_JSON_COLS:
                v = json.dumps(v, ensure_ascii=False)
            elif k == "stop_requested":
                v = 1 if v else 0
            sets.append("%s=?" % k)
            args.append(v)
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(time.time())
        args.append(str(job_id))
        with self._lock:
            self._conn.execute(
                "UPDATE tg_extract_jobs SET %s WHERE job_id=?" % ",".join(sets), args)
            self._conn.commit()

    def bump_job_counters(self, job_id: str, *, pulled: int = 0, dedup: int = 0,
                          admins: int = 0, floodwaits: int = 0) -> None:
        """原子累加任务计数（不覆盖，避免并发写覆盖丢更新）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE tg_extract_jobs SET "
                "pulled_total=pulled_total+?, dedup_skipped=dedup_skipped+?, "
                "admins_excluded=admins_excluded+?, floodwaits=floodwaits+?, "
                "updated_at=? WHERE job_id=?",
                (int(pulled), int(dedup), int(admins), int(floodwaits),
                 time.time(), str(job_id)),
            )
            self._conn.commit()

    def request_stop(self, job_id: str) -> None:
        self.update_job(job_id, stop_requested=1)

    def is_stop_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT stop_requested FROM tg_extract_jobs WHERE job_id=?",
                (str(job_id),),
            ).fetchone()
        return bool(row and row[0])

    # ── 开口（同群私聊；与提取配额分开）────────────────────────────────────

    def get_member(self, group_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            ).fetchone()
        return self._member_to_dict(row) if row else None

    def get_outreach_contact(self, account_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """这个号对这人开过口（已发 / 已回 / 已收口）的那一行，多群同人取最近一次；没有 → None。
        含 sent：回复链可能先于入站钩子把行翻成 replied。"""
        acct = str(account_id or "").strip()
        uid = str(user_id or "").strip()
        if not acct or not uid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE user_id=? AND outreach_account_id=? "
                "AND outreach_state IN (?,?,?) ORDER BY replied_at DESC, outreach_at DESC LIMIT 1",
                (uid, acct, OUTREACH_REPLIED, OUTREACH_SENT, OUTREACH_CLOSED),
            ).fetchone()
        return self._member_to_dict(row) if row else None

    def list_by_hash_account(self, account_id: str, *,
                             limit: int = 2000) -> List[Dict[str, Any]]:
        """该号持有 access_hash、因而可以尝试同群开口的成员。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE hash_account_id=? "
                "AND access_hash!='' LIMIT ?",
                (str(account_id), max(1, min(int(limit), 5000))),
            ).fetchall()
        return [self._member_to_dict(r) for r in rows]

    def touched_user_ids(self) -> Set[str]:
        """跨群已排队/已发/已回/不可达的 user_id。同一个人只开口一次。"""
        ph = ",".join(["?"] * len(OUTREACH_TOUCHED))
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT user_id FROM tg_group_members "
                "WHERE outreach_state IN (%s)" % ph,
                OUTREACH_TOUCHED,
            ).fetchall()
        return {str(r[0]) for r in rows}

    def count_outreach_sent_since(self, account_id: str, since_ts: float) -> int:
        """某号自 since_ts 起真正发出的开口（sent+replied）。排队不算。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE outreach_account_id=? "
                "AND outreach_state IN (?,?) AND outreach_at>=?",
                (str(account_id), OUTREACH_SENT, OUTREACH_REPLIED, float(since_ts)),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_outreach_queued(self, account_id: str) -> int:
        """今天排上还没发的（queued + approved）。都占今日坑位。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE hash_account_id=? "
                "AND outreach_state IN (?,?)",
                (str(account_id), OUTREACH_QUEUED, OUTREACH_APPROVED),
            ).fetchone()
        return int(row[0]) if row else 0

    def set_opener(self, group_id: str, user_id: str, text: str, source: str,
                   persona_id: Optional[str] = None, variant: Optional[str] = None) -> bool:
        """给排队中的人写开场文案（ai / template / manual）。已发的不改。

        ``persona_id`` / ``variant`` 给了就记下这条是以谁的身份、哪种切入写的
        （回复率按人设 / 切入方式切片用）；None = 不动原值。
        """
        sets = ["opener_text=?", "opener_source=?"]
        args: List[Any] = [str(text or "")[:500], str(source or "")[:16]]
        if persona_id is not None:
            sets.append("opener_persona=?")
            args.append(str(persona_id)[:64])
        if variant is not None:
            sets.append("opener_variant=?")
            args.append(str(variant)[:16])
        args += [str(group_id), str(user_id), OUTREACH_QUEUED, OUTREACH_APPROVED]
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET %s WHERE group_id=? AND user_id=? "
                "AND outreach_state IN (?,?)" % ", ".join(sets), args)
            self._conn.commit()
            return bool(cur.rowcount)

    def record_sent_text(self, group_id: str, user_id: str, text: str) -> bool:
        """发出那一刻把实发文案落到行上（坐席临时改过的也记下，回复率归因看得到）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET opener_text=? WHERE group_id=? AND user_id=? "
                "AND outreach_state=?",
                (str(text or "")[:500], str(group_id), str(user_id), OUTREACH_SENDING),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def approve_queued(self, account_id: str, now: Optional[float] = None, *,
                       pairs: Optional[Sequence[Tuple[str, str]]] = None) -> int:
        """queued → approved。只批有文案的；``pairs`` 给了就只批这些人。"""
        now = time.time() if now is None else float(now)
        n = 0
        with self._lock:
            if pairs is None:
                cur = self._conn.execute(
                    "UPDATE tg_group_members SET outreach_state=?, approved_at=? "
                    "WHERE hash_account_id=? AND outreach_state=? AND opener_text!=''",
                    (OUTREACH_APPROVED, now, str(account_id), OUTREACH_QUEUED),
                )
                n = int(cur.rowcount or 0)
            else:
                for gid, uid in pairs:
                    cur = self._conn.execute(
                        "UPDATE tg_group_members SET outreach_state=?, approved_at=? "
                        "WHERE group_id=? AND user_id=? AND hash_account_id=? "
                        "AND outreach_state=? AND opener_text!=''",
                        (OUTREACH_APPROVED, now, str(gid), str(uid), str(account_id),
                         OUTREACH_QUEUED),
                    )
                    n += int(cur.rowcount or 0)
            self._conn.commit()
        return n

    def next_approved(self, account_id: str,
                      gtouch_after_ts: float = 0.0) -> Optional[Dict[str, Any]]:
        """调度器下一个要发的人：批准最早的那个。

        ``gtouch_after_ts`` > 0：这个号在此刻之后才在群里接过话的人（任何群）先跳过——
        刚公开回过就私聊像盯人；跳过的人不堵后面的队。"""
        sql = ("SELECT * FROM tg_group_members m WHERE hash_account_id=? AND outreach_state=? "
               "AND opener_text!='' ")
        args: List[Any] = [str(account_id), OUTREACH_APPROVED]
        if gtouch_after_ts > 0:
            sql += ("AND NOT EXISTS (SELECT 1 FROM tg_group_members g WHERE g.user_id=m.user_id "
                    "AND g.gtouch_account_id=? AND g.gtouch_state IN ('sent','sending') "
                    "AND g.gtouch_at>?) ")
            args += [str(account_id), float(gtouch_after_ts)]
        sql += "ORDER BY approved_at ASC, user_id ASC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return self._member_to_dict(row) if row else None

    def today_opener_texts(self, account_id: str, since_ts: float) -> List[str]:
        """这个号今天已定/已发的开场文案（去雷同用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT opener_text FROM tg_group_members WHERE hash_account_id=? "
                "AND opener_text!='' AND (outreach_state IN (?,?,?) OR outreach_at>=?)",
                (str(account_id), OUTREACH_QUEUED, OUTREACH_APPROVED, OUTREACH_SENDING,
                 float(since_ts)),
            ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]

    def outreach_summary(self, account_id: str, since_ts: float) -> Dict[str, Any]:
        """今日汇总：发了 / 回了 / 不可达 / 跳过 / 封存 / 跟进，以及没发出去的原因分布。"""
        out: Dict[str, Any] = {"sent": 0, "replied": 0, "blocked": 0, "skipped": 0,
                               "closed": 0, "queued": 0, "approved": 0, "followups": 0,
                               "errors": {}}
        with self._lock:
            rows = self._conn.execute(
                "SELECT outreach_state, outreach_error, COUNT(*) FROM tg_group_members "
                "WHERE hash_account_id=? AND ((outreach_at>=? AND outreach_state IN (?,?,?,?,?)) "
                "OR outreach_state IN (?,?)) GROUP BY outreach_state, outreach_error",
                (str(account_id), float(since_ts), OUTREACH_SENT, OUTREACH_REPLIED,
                 OUTREACH_BLOCKED, OUTREACH_SKIPPED, OUTREACH_CLOSED,
                 OUTREACH_QUEUED, OUTREACH_APPROVED),
            ).fetchall()
            fu = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE outreach_account_id=? "
                "AND followup_at>=?", (str(account_id), float(since_ts)),
            ).fetchone()
        for state, err, n in rows:
            if state in out:
                out[state] += int(n or 0)
            if err and state in (OUTREACH_BLOCKED, OUTREACH_SKIPPED):
                out["errors"][str(err)] = out["errors"].get(str(err), 0) + int(n or 0)
        out["followups"] = int(fu[0]) if fu else 0
        return out

    # ── 跟进（72h 一次）/ 封存 ──────────────────────────────────────────────

    def list_followup_due(self, account_id: str, before_ts: float,
                          limit: int = 50) -> List[Dict[str, Any]]:
        """这个号发出 ≥N 小时仍没回音、还没跟进过的人（最早发的排前）。拒绝过的不算。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE outreach_account_id=? AND outreach_state=? "
                "AND followup_at=0 AND outreach_at>0 AND outreach_at<=? AND access_hash!='' "
                "AND outreach_error!='stop_contact' ORDER BY outreach_at ASC LIMIT ?",
                (str(account_id), OUTREACH_SENT, float(before_ts), max(1, int(limit))),
            ).fetchall()
        return [self._member_to_dict(r) for r in rows]

    def claim_followup(self, group_id: str, user_id: str, now: float) -> bool:
        """原子占坑：followup_at 从 0 写成 now。两条链（调度器 / 坐席点）只会有一条拿到。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET followup_at=? WHERE group_id=? AND user_id=? "
                "AND outreach_state=? AND followup_at=0",
                (float(now), str(group_id), str(user_id), OUTREACH_SENT),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def unclaim_followup(self, group_id: str, user_id: str) -> None:
        """跟进没发出去（客户端不在 / 可重试错误）→ 把坑还回去，下次再跟。"""
        with self._lock:
            self._conn.execute(
                "UPDATE tg_group_members SET followup_at=0, followup_text='' "
                "WHERE group_id=? AND user_id=? AND followup_text=''",
                (str(group_id), str(user_id)),
            )
            self._conn.commit()

    def record_followup_text(self, group_id: str, user_id: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tg_group_members SET followup_text=? WHERE group_id=? AND user_id=?",
                (str(text or "")[:500], str(group_id), str(user_id)),
            )
            self._conn.commit()

    def count_followups_since(self, account_id: str, since_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE outreach_account_id=? "
                "AND followup_at>=? AND followup_text!=''",
                (str(account_id), float(since_ts)),
            ).fetchone()
        return int(row[0]) if row else 0

    def last_followup_ts(self, account_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(followup_at) FROM tg_group_members WHERE outreach_account_id=? "
                "AND followup_text!=''", (str(account_id),),
            ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def close_unanswered(self, before_ts: float, account_id: str = "") -> int:
        """跟进过、又过了 N 小时仍是 sent → closed（封存，不再碰）。"""
        sql = ("UPDATE tg_group_members SET outreach_state=?, outreach_error='no_reply' "
               "WHERE outreach_state=? AND followup_at>0 AND followup_text!='' AND followup_at<=?")
        args: List[Any] = [OUTREACH_CLOSED, OUTREACH_SENT, float(before_ts)]
        if account_id:
            sql += " AND outreach_account_id=?"
            args.append(str(account_id))
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
        return int(cur.rowcount or 0)

    # ── 回复率 / 切片 ────────────────────────────────────────────────────────

    def reply_rate(self, account_id: str, since_ts: float) -> Dict[str, Any]:
        """这个号自 since_ts 起发出的开口里有多少回了。sent+replied+closed 为分母。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT outreach_state, COUNT(*) FROM tg_group_members "
                "WHERE outreach_account_id=? AND outreach_at>=? AND outreach_state IN (?,?,?) "
                "GROUP BY outreach_state",
                (str(account_id), float(since_ts), OUTREACH_SENT, OUTREACH_REPLIED,
                 OUTREACH_CLOSED),
            ).fetchall()
        n = {str(s): int(c or 0) for s, c in rows}
        total = sum(n.values())
        replied = n.get(OUTREACH_REPLIED, 0)
        return {"sent": total, "replied": replied,
                "rate": (replied / total) if total else None}

    def contacted_replies(self, since_ts: float, account_id: str = "") -> List[Dict[str, Any]]:
        """自 since_ts 起我方开口（私聊开口，或群里接话后 TA 私聊来找）、对方回了的人
        （成交归因的底表；不含 access_hash）。"""
        sql = ("SELECT group_id, group_title, user_id, username, first_name, outreach_account_id, "
               "opener_variant, opener_source, outreach_at, replied_at, gtouch_at, outreach_error "
               "FROM tg_group_members WHERE outreach_state IN (?,?) AND replied_at>0 "
               "AND ((outreach_at>=? AND outreach_at>0) "
               "OR (outreach_error='gtouch_inbound' AND gtouch_at>=?))")
        args: List[Any] = [OUTREACH_REPLIED, OUTREACH_CLOSED, float(since_ts), float(since_ts)]
        if account_id:
            sql += " AND outreach_account_id=?"
            args.append(str(account_id))
        with self._lock:
            cur = self._conn.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def gtouch_stats(self, since_ts: float, account_id: str = "") -> Dict[str, Any]:
        """群里接话这一层值不值：接了多少人 → 后来多少人回应（群里回了 / 私聊回我方开口 / 主动私聊来）；
        外加同一窗口里的私聊开口按「先在群里接过话」和「冷私聊」分两组比回复率。

        都按人、按号算（接话和私聊可能落在同一个人不同群的行上）。"""
        acct_sql = " AND gtouch_account_id=?" if account_id else ""
        out_sql = " AND outreach_account_id=?" if account_id else ""
        a = [str(account_id)] if account_id else []
        with self._lock:
            touched = self._conn.execute(
                "SELECT user_id, gtouch_account_id, MIN(gtouch_at), MAX(gtouch_reply_self) "
                "FROM tg_group_members WHERE gtouch_state='sent' AND gtouch_at>=?%s "
                "GROUP BY user_id, gtouch_account_id" % acct_sql, [float(since_ts), *a]).fetchall()
            all_touch = self._conn.execute(
                "SELECT user_id, gtouch_account_id, MIN(gtouch_at) FROM tg_group_members "
                "WHERE gtouch_state='sent'%s GROUP BY user_id, gtouch_account_id"
                % acct_sql, a).fetchall()
            contacts = self._conn.execute(
                "SELECT user_id, outreach_account_id, outreach_state, outreach_at, replied_at, "
                "outreach_error FROM tg_group_members WHERE outreach_state IN (?,?,?)%s"
                % out_sql, [OUTREACH_SENT, OUTREACH_REPLIED, OUTREACH_CLOSED, *a]).fetchall()
        first_touch = {(str(u), str(ac)): float(t or 0) for u, ac, t in all_touch}
        by_person: Dict[Tuple[str, str], List[Any]] = {}
        for r in contacts:
            by_person.setdefault((str(r[0]), str(r[1])), []).append(r)
        sent = len(touched)
        responded = inbound = dm_after = group_replied = 0
        for u, ac, t, gself in touched:
            rows = by_person.get((str(u), str(ac)), [])
            if int(gself or 0):
                group_replied += 1
            if int(gself or 0) or any(float(r[4] or 0) > 0 and float(r[4] or 0) >= float(t or 0)
                                      for r in rows):
                responded += 1
            if any(str(r[5]) == "gtouch_inbound" for r in rows):
                inbound += 1
            if any(float(r[3] or 0) > float(t or 0) for r in rows):
                dm_after += 1
        warm = {"sent": 0, "replied": 0}
        cold = {"sent": 0, "replied": 0}
        for r in contacts:
            oat = float(r[3] or 0)
            if oat < float(since_ts) or oat <= 0:
                continue
            t = first_touch.get((str(r[0]), str(r[1])))
            bucket = warm if (t is not None and t < oat) else cold
            bucket["sent"] += 1
            if float(r[4] or 0) > 0:
                bucket["replied"] += 1
        for b in (warm, cold):
            b["rate"] = (b["replied"] / b["sent"]) if b["sent"] else None
        return {"sent": sent, "responded": responded, "inbound": inbound, "dm_after": dm_after,
                "group_replied": group_replied,
                "response_rate": (responded / sent) if sent else None,
                "dm_warm": warm, "dm_cold": cold}

    def outreach_stats(self, since_ts: float, account_id: str = "") -> Dict[str, Any]:
        """回复率切片：按文案来源 / 人设 / 发出小时 / 账号；外加入库→排队→发出→回复漏斗。

        切片只看真正发出的（sent/replied/closed，按 outreach_at）；漏斗看库里现状。
        """
        where = "outreach_at>=? AND outreach_state IN (?,?,?)"
        args: List[Any] = [float(since_ts), OUTREACH_SENT, OUTREACH_REPLIED, OUTREACH_CLOSED]
        if account_id:
            where += " AND outreach_account_id=?"
            args.append(str(account_id))

        def _slice(expr: str) -> List[Dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT %s AS k, COUNT(*), SUM(CASE WHEN outreach_state=? THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN followup_text!='' THEN 1 ELSE 0 END) "
                "FROM tg_group_members WHERE %s GROUP BY k ORDER BY COUNT(*) DESC" % (expr, where),
                [OUTREACH_REPLIED, *args],
            ).fetchall()
            out = []
            for k, total, rep, fu in rows:
                total = int(total or 0)
                rep = int(rep or 0)
                out.append({"key": str(k if k is not None else ""), "sent": total,
                            "replied": rep, "followups": int(fu or 0),
                            "rate": (rep / total) if total else None})
            return out

        with self._lock:
            by_source = _slice("CASE WHEN opener_source='' THEN 'unknown' ELSE opener_source END")
            by_persona = _slice("CASE WHEN opener_persona='' THEN '-' ELSE opener_persona END")
            by_hour = _slice("CAST(strftime('%H', outreach_at, 'unixepoch', 'localtime') AS INTEGER)")
            by_account = _slice("outreach_account_id")
            by_variant = _slice("CASE WHEN opener_variant='' THEN '-' ELSE opener_variant END")
            # 首回时延：发出 → 对方第一条私聊（只看真发过、真回了的）
            lat_rows = self._conn.execute(
                "SELECT replied_at-outreach_at FROM tg_group_members WHERE %s AND outreach_state=? "
                "AND replied_at>outreach_at ORDER BY 1" % where,
                [*args, OUTREACH_REPLIED],
            ).fetchall()
            # 漏斗看库里现状；replied 里 outreach_at=0 的是「人家先来找我们」，不算发出；
            # answered_at>0 = 回了之后我方接上话了（第二跳）
            fsql = ("SELECT outreach_state, COUNT(*), SUM(CASE WHEN outreach_at>0 THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN answered_at>0 THEN 1 ELSE 0 END) FROM tg_group_members")
            fargs: List[Any] = []
            if account_id:
                fsql += " WHERE hash_account_id=? OR outreach_account_id=?"
                fargs = [str(account_id), str(account_id)]
            frows = self._conn.execute(fsql + " GROUP BY outreach_state", fargs).fetchall()
            recent = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE outreach_state=? AND replied_at>=?%s "
                "ORDER BY replied_at DESC LIMIT 20" % (" AND outreach_account_id=?" if account_id else ""),
                [OUTREACH_REPLIED, float(since_ts), *([str(account_id)] if account_id else [])],
            ).fetchall()
        st = {str(s): int(c or 0) for s, c, _, _ in frows}
        contacted = {str(s): int(c2 or 0) for s, _, c2, _ in frows}
        answered = sum(int(a or 0) for _, _, _, a in frows)
        total_members = sum(st.values())
        replied_contacted = contacted.get(OUTREACH_REPLIED, 0)
        sent_all = st.get(OUTREACH_SENT, 0) + replied_contacted + st.get(OUTREACH_CLOSED, 0)
        funnel = {
            "members": total_members,
            "queued": st.get(OUTREACH_QUEUED, 0) + st.get(OUTREACH_APPROVED, 0),
            "sent": sent_all,
            "replied": replied_contacted,
            "inbound_first": st.get(OUTREACH_REPLIED, 0) - replied_contacted,
            "answered": answered,
            "closed": st.get(OUTREACH_CLOSED, 0),
            "blocked": st.get(OUTREACH_BLOCKED, 0),
            "skipped": st.get(OUTREACH_SKIPPED, 0),
        }
        lats = [float(r[0]) for r in lat_rows if r and r[0] is not None]
        latency = {"n": len(lats), "median_sec": None, "p75_sec": None}
        if lats:
            latency["median_sec"] = int(lats[len(lats) // 2])
            latency["p75_sec"] = int(lats[min(len(lats) - 1, (len(lats) * 3) // 4)])
        by_hour.sort(key=lambda r: int(r["key"] or 0))
        return {"since_ts": float(since_ts), "account_id": str(account_id or ""),
                "by_source": by_source, "by_persona": by_persona, "by_hour": by_hour,
                "by_account": by_account, "by_variant": by_variant,
                "reply_latency": latency, "funnel": funnel,
                "recent_replies": [dict(r) for r in recent]}

    def mark_outreach_answered(self, account_id: str, user_id: str, ts: float) -> int:
        """对方回了之后，我方（AI 或坐席/手机）第一次回话 → 记 answered_at。返回首次接上的行数。"""
        return self.record_outreach_outbound(account_id, user_id, ts)[0]

    def record_outreach_outbound(self, account_id: str, user_id: str,
                                 ts: float) -> Tuple[int, int]:
        """对方回过之后我方的每一句出站：推进 last_out_at，首句记 answered_at，清掉本轮打标记号。

        只认 replied 态、且这条出站不早于对方首回（开场/跟进本身的镜像在 sent 态，天然不算）。
        返回 ``(首次接上行数, 本轮打过需人工标的行数)``——后者 >0 调用方才去收件箱摘标。
        """
        uid = str(user_id or "").strip()
        acct = str(account_id or "").strip()
        if not uid or not acct:
            return 0, 0
        t = float(ts)
        where = ("WHERE user_id=? AND outreach_account_id=? AND outreach_state=? "
                 "AND replied_at>0 AND ?>=replied_at")
        args = (uid, acct, OUTREACH_REPLIED, t)
        with self._lock:
            flagged = int(self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members " + where + " AND stalled_flagged_at>0",
                args).fetchone()[0] or 0)
            first = int(self._conn.execute(
                "UPDATE tg_group_members SET answered_at=? " + where + " AND answered_at=0",
                (t,) + args).rowcount or 0)
            self._conn.execute(
                "UPDATE tg_group_members SET last_out_at=MAX(last_out_at, ?), stalled_flagged_at=0 "
                + where, (t,) + args)
            self._conn.commit()
        return first, flagged

    # 「没接上」两种：回了我方从没回话（按首回计时）；接上过、对方又来话我方没再回（按最后来话计时）。
    # 迁移前接上的行 last_out_at=0，用 answered_at 兜底，免得老会话被误判成又断了。
    _STALLED_WHERE = (
        "outreach_state=? AND replied_at>0 AND outreach_error!='stop_contact' AND ("
        "(answered_at=0 AND replied_at<=?) OR "
        "(answered_at>0 AND last_in_at>MAX(last_out_at, answered_at) AND last_in_at<=?))")

    def list_stalled_replies(self, before_ts: float, account_id: str = "",
                             limit: int = 50, *, unflagged_only: bool = False) -> List[Dict[str, Any]]:
        """到 before_ts 为止对方在等我方回话的人（等得最久的在前）。拒绝联系的不列。

        ``unflagged_only``：只要本轮还没打过需人工标的（坐席手动摘了标不再打回去）。
        """
        sql = "SELECT * FROM tg_group_members WHERE " + self._STALLED_WHERE
        args: List[Any] = [OUTREACH_REPLIED, float(before_ts), float(before_ts)]
        if unflagged_only:
            sql += " AND stalled_flagged_at=0"
        if account_id:
            sql += " AND outreach_account_id=?"
            args.append(str(account_id))
        sql += " ORDER BY CASE WHEN answered_at=0 THEN replied_at ELSE last_in_at END ASC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def mark_stalled_flagged(self, group_id: str, user_id: str, ts: float) -> int:
        """本轮「没接上」已打过需人工标；我方下一次回话（record_outreach_outbound）清零。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET stalled_flagged_at=? "
                "WHERE group_id=? AND user_id=? AND stalled_flagged_at=0",
                (float(ts), str(group_id or ""), str(user_id or "")))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def variant_rates(self, since_ts: float, account_id: str = "") -> Dict[str, Dict[str, int]]:
        """切入方式 → {sent, replied}（真发出的，按 outreach_at）。开口选法用。"""
        sql = ("SELECT opener_variant, COUNT(*), SUM(CASE WHEN outreach_state=? THEN 1 ELSE 0 END) "
               "FROM tg_group_members WHERE outreach_at>=? AND outreach_state IN (?,?,?) "
               "AND opener_variant!=''")
        args: List[Any] = [OUTREACH_REPLIED, float(since_ts), OUTREACH_SENT, OUTREACH_REPLIED,
                           OUTREACH_CLOSED]
        if account_id:
            sql += " AND outreach_account_id=?"
            args.append(str(account_id))
        with self._lock:
            rows = self._conn.execute(sql + " GROUP BY opener_variant", args).fetchall()
        return {str(v): {"sent": int(n or 0), "replied": int(r or 0)} for v, n, r in rows}

    def list_replied_since(self, account_id: str, since_ts: float,
                           limit: int = 20) -> List[Dict[str, Any]]:
        """这个号自 since_ts 起收到的回音（含人家先来找的），新的在前。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE outreach_account_id=? AND outreach_state=? "
                "AND replied_at>=? ORDER BY replied_at DESC LIMIT ?",
                (str(account_id), OUTREACH_REPLIED, float(since_ts), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def skip_outreach(self, group_id: str, user_id: str, reason: str = "operator_skip") -> bool:
        """坐席跳过这个人：queued/approved → skipped，不再碰。"""
        return self.cas_outreach(
            group_id, user_id, expect_states=(OUTREACH_QUEUED, OUTREACH_APPROVED),
            new_state=OUTREACH_SKIPPED, error=reason)

    def mark_outreach_stop_contact(self, user_id: str) -> int:
        """对方说「别发了」：这个人在所有群、所有号下都不再碰。

        已发/已回的行保留状态（额度统计不变），只记原因；还没发的全部标不可达。
        """
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        with self._lock:
            a = self._conn.execute(
                "UPDATE tg_group_members SET outreach_state=?, outreach_error='stop_contact' "
                "WHERE user_id=? AND outreach_state IN (?,?,?)",
                (OUTREACH_BLOCKED, uid, OUTREACH_NONE, OUTREACH_QUEUED, OUTREACH_APPROVED),
            )
            b = self._conn.execute(
                "UPDATE tg_group_members SET outreach_error='stop_contact', "
                "outreach_state=CASE WHEN outreach_state=? THEN ? ELSE outreach_state END "
                "WHERE user_id=? AND outreach_state IN (?,?,?)",
                (OUTREACH_SENT, OUTREACH_REPLIED, uid,
                 OUTREACH_SENT, OUTREACH_REPLIED, OUTREACH_SENDING),
            )
            self._conn.commit()
        return int(a.rowcount or 0) + int(b.rowcount or 0)

    def get_outreach_mode(self, account_id: str) -> str:
        mode = str(self.get_hold(account_id).get("mode") or OUTREACH_MODE_MANUAL)
        return mode if mode in OUTREACH_MODES else OUTREACH_MODE_MANUAL

    def set_outreach_mode(self, account_id: str, mode: str) -> str:
        mode = str(mode or "").strip()
        if mode not in OUTREACH_MODES:
            mode = OUTREACH_MODE_MANUAL
        self.set_hold(str(account_id), mode=mode)
        return mode

    # ── 号龄（坐席申报）────────────────────────────────────────────────────────
    # 注册表 created_at 只是「登进本系统」的时间；老 TG 号切到销售人设时真实号龄要人报。
    # 申报值从申报那天起继续长：declared_age_days + (now - declared_age_at) / 86400。

    def set_declared_age(self, account_id: str, days: float, now: Optional[float] = None) -> float:
        """申报这个 TG 号的真实号龄（天）。0 = 撤销申报。返回落库的天数。"""
        now = time.time() if now is None else float(now)
        try:
            d = max(0.0, min(float(days), 3650.0))
        except (TypeError, ValueError):
            d = 0.0
        with self._lock:
            self._conn.execute(
                "INSERT INTO tg_outreach_holds (account_id, updated_at, declared_age_days, "
                "declared_age_at) VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "declared_age_days=excluded.declared_age_days, "
                "declared_age_at=excluded.declared_age_at, updated_at=excluded.updated_at",
                (str(account_id), now, d, now if d > 0 else 0.0))
            self._conn.commit()
        return d

    def declared_age_days(self, account_id: str, now: Optional[float] = None) -> Optional[float]:
        """申报号龄推到今天；没申报 → None。"""
        now = time.time() if now is None else float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT declared_age_days, declared_age_at FROM tg_outreach_holds "
                "WHERE account_id=?", (str(account_id),)).fetchone()
        if not row or float(row[0] or 0) <= 0:
            return None
        return float(row[0]) + max(0.0, (now - float(row[1] or now)) / 86400.0)

    # ── 群里接话（公开回复 TA 在群里那句，之后再私聊）────────────────────────────
    # gtouch_state: '' 未碰 / drafted 已拟稿 / sending 占坑中 / sent 已发 / failed 发不出 / skipped 坐席跳过

    def list_gtouch_candidates(self, account_id: str, *, since_msg_ts: float,
                               limit: int = 200) -> List[Dict[str, Any]]:
        """这个号所在群里、近期说过话（有消息 id）、还没被群里接过话的人。

        私聊已经发出去 / 对方回了 / 说过别再发的不再列；私聊因隐私设置发不到的（blocked+privacy）
        反而要列——群里接话是唯一够得着 TA 的路。同一个人在别的群已被（任何号）接过话的不再列：
        一个人只在群里被公开接一次。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_group_members m WHERE (source_account_id=? OR hash_account_id=?) "
                "AND spoke=1 AND is_admin=0 AND is_bot=0 AND last_msg_id!='' AND last_msg_text!='' "
                "AND last_msg_ts>=? AND gtouch_state IN ('', 'drafted') "
                "AND outreach_error!='stop_contact' "
                "AND (outreach_state IN (?,?,?) OR (outreach_state=? AND outreach_error='privacy')) "
                "AND NOT EXISTS (SELECT 1 FROM tg_group_members g WHERE g.user_id=m.user_id "
                "AND (g.gtouch_state IN ('sent','sending') OR g.gtouch_error='stale_sending')) "
                "ORDER BY last_msg_ts DESC LIMIT ?",
                (str(account_id), str(account_id), float(since_msg_ts), OUTREACH_NONE,
                 OUTREACH_QUEUED, OUTREACH_APPROVED, OUTREACH_BLOCKED, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_gtouch_draft(self, group_id: str, user_id: str, text: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET gtouch_text=?, gtouch_state='drafted' "
                "WHERE group_id=? AND user_id=? AND gtouch_state IN ('', 'drafted')",
                (str(text or "")[:500], str(group_id), str(user_id)))
            self._conn.commit()
            return bool(cur.rowcount)

    def claim_gtouch(self, group_id: str, user_id: str, account_id: str, now: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET gtouch_state='sending', gtouch_account_id=?, "
                "gtouch_at=? WHERE group_id=? AND user_id=? AND gtouch_state IN ('', 'drafted')",
                (str(account_id), float(now), str(group_id), str(user_id)))
            self._conn.commit()
            return bool(cur.rowcount)

    def finish_gtouch(self, group_id: str, user_id: str, *, state: str, text: str = "",
                      error: str = "", now: Optional[float] = None, msg_id: str = "") -> bool:
        """占坑后的落定：sent（记文案、时间、我方那条的消息 id）/ failed / drafted（还坑，下次再发）。"""
        if state not in ("sent", "failed", "drafted"):
            return False
        sets = "gtouch_state=?, gtouch_error=?"
        args: List[Any] = [state, str(error or "")[:60]]
        if state == "sent":
            sets += ", gtouch_text=?, gtouch_at=?, gtouch_msg_id=?"
            args += [str(text or "")[:500], float(time.time() if now is None else now),
                     str(msg_id or "")[:40]]
        elif state == "drafted":
            sets += ", gtouch_at=0, gtouch_account_id=''"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET %s WHERE group_id=? AND user_id=? "
                "AND gtouch_state='sending'" % sets, [*args, str(group_id), str(user_id)])
            self._conn.commit()
            return bool(cur.rowcount)

    def skip_gtouch(self, group_id: str, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET gtouch_state='skipped' "
                "WHERE group_id=? AND user_id=? AND gtouch_state IN ('', 'drafted')",
                (str(group_id), str(user_id)))
            self._conn.commit()
            return bool(cur.rowcount)

    def count_gtouch_since(self, account_id: str, since_ts: float, group_id: str = "") -> int:
        """这个号自 since_ts 起在群里接过几次话（含占坑中的）；给 group_id 只数这个群。"""
        sql = ("SELECT COUNT(*) FROM tg_group_members WHERE gtouch_account_id=? "
               "AND gtouch_state IN ('sent','sending') AND gtouch_at>=?")
        args: List[Any] = [str(account_id), float(since_ts)]
        if group_id:
            sql += " AND group_id=?"
            args.append(str(group_id))
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0

    def gtouch_by_user(self, account_id: str,
                       user_ids: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        """这个号在群里公开接过话的人 → 最近一次接话（哪个群、TA 那句、我方那句、时间）。

        接话记在「群+人」那一行上；私聊开口可能落在同一个人的另一个群的行上，所以按人查。"""
        sql = ("SELECT user_id, group_id, group_title, last_msg_text, gtouch_text, gtouch_at, "
               "gtouch_state, gtouch_reply_text, gtouch_reply_self "
               "FROM tg_group_members WHERE gtouch_account_id=? "
               "AND gtouch_state IN ('sent','sending')")
        args: List[Any] = [str(account_id)]
        ids = [str(u) for u in (user_ids or ()) if str(u or "").strip()]
        if user_ids is not None:
            if not ids:
                return {}
            sql += " AND user_id IN (%s)" % ",".join(["?"] * len(ids))
            args += ids
        sql += " ORDER BY gtouch_at ASC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return {str(r["user_id"]): dict(r) for r in rows}

    def gtouched_elsewhere(self, user_id: str, group_id: str) -> bool:
        """这个人在别的群已被（任何号）公开接过话（含发没发不可知的）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tg_group_members WHERE user_id=? AND group_id!=? "
                "AND (gtouch_state IN ('sent','sending') OR gtouch_error='stale_sending') LIMIT 1",
                (str(user_id), str(group_id))).fetchone()
        return row is not None

    def requeue_after_gtouch(self, user_id: str, account_id: str) -> int:
        """群里刚接过话：这个号给这个人排着的私聊开口退回 queued（批准作废），
        AI/模板拟的开场清掉重拟（新稿会接上群里那句）；坐席手改过的保留，重新过一遍批准。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET outreach_state=?, approved_at=0, "
                "opener_text=CASE WHEN opener_source='manual' THEN opener_text ELSE '' END "
                "WHERE user_id=? AND hash_account_id=? AND outreach_state IN (?,?)",
                (OUTREACH_QUEUED, str(user_id), str(account_id), OUTREACH_QUEUED,
                 OUTREACH_APPROVED))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def reap_stale_gtouch(self, now: Optional[float] = None, *, max_age_sec: float = 600.0) -> int:
        """接话 sending 超时（进程在发出与落定之间挂了）→ failed/stale_sending。
        发没发不可知：不再列、不重发，和私聊开口的 reap 同一取舍。"""
        now = time.time() if now is None else float(now)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET gtouch_state='failed', gtouch_error='stale_sending' "
                "WHERE gtouch_state='sending' AND gtouch_at>0 AND gtouch_at<?",
                (now - float(max_age_sec),))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def gtouch_denied_groups(self, account_id: str) -> List[str]:
        """这个号在哪些群里发不了言（被禁言 / 无权限）——这些群不再列人。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT group_id FROM tg_group_members WHERE gtouch_account_id=? "
                "AND gtouch_state='failed' AND gtouch_error='group_denied'",
                (str(account_id),)).fetchall()
        return [str(r[0]) for r in rows]

    def last_gtouch_ts(self, account_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(gtouch_at) FROM tg_group_members WHERE gtouch_account_id=? "
                "AND gtouch_state IN ('sent','sending')", (str(account_id),)).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def mark_gtouch_reply(self, account_id: str, group_id: str, reply_to_msg_id: str,
                          from_user_id: str, text: str, now: float) -> Optional[Dict[str, Any]]:
        """群里有人回了我方接话那条：记下第一条回复；接话对象本人后来再回，覆盖掉旁人的。

        返回被记上的那一行（没对上 / 已记过 → None）。"""
        mid = str(reply_to_msg_id or "").strip()
        if not mid:
            return None
        sql = ("SELECT * FROM tg_group_members WHERE group_id=? AND gtouch_msg_id=? "
               "AND gtouch_state='sent'")
        args: List[Any] = [str(group_id), mid]
        if account_id:
            sql += " AND gtouch_account_id=?"
            args.append(str(account_id))
        with self._lock:
            row = self._conn.execute(sql + " LIMIT 1", args).fetchone()
            if row is None:
                return None
            is_self = 1 if str(from_user_id) == str(row["user_id"]) else 0
            if float(row["gtouch_reply_at"] or 0) > 0 and (int(row["gtouch_reply_self"] or 0) or not is_self):
                return None
            self._conn.execute(
                "UPDATE tg_group_members SET gtouch_reply_at=?, gtouch_reply_text=?, "
                "gtouch_reply_self=? WHERE group_id=? AND user_id=?",
                (float(now), " ".join(str(text or "").split())[:300], is_self,
                 str(row["group_id"]), str(row["user_id"])))
            self._conn.commit()
            out = dict(row)
        out.update(gtouch_reply_at=float(now), gtouch_reply_self=is_self,
                   gtouch_reply_text=" ".join(str(text or "").split())[:300])
        return out

    def list_gtouch_replies(self, account_id: str, since_ts: float,
                            limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE gtouch_account_id=? AND gtouch_state='sent' "
                "AND gtouch_reply_at>=? AND gtouch_reply_at>0 ORDER BY gtouch_reply_at DESC LIMIT ?",
                (str(account_id), float(since_ts), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def list_gtouch_sent_since(self, account_id: str, since_ts: float,
                               limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tg_group_members WHERE gtouch_account_id=? AND gtouch_state='sent' "
                "AND gtouch_at>=? ORDER BY gtouch_at DESC LIMIT ?",
                (str(account_id), float(since_ts), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def last_outreach_sent_ts(self, account_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(outreach_at) FROM tg_group_members "
                "WHERE outreach_account_id=? AND outreach_state IN (?,?)",
                (str(account_id), OUTREACH_SENT, OUTREACH_REPLIED),
            ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def count_unhashed_for_account(self, account_id: str) -> int:
        """这个号拉进来、但还没有私聊凭证的可聊成员（需要再提取一次才开得了口）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_group_members WHERE source_account_id=? "
                "AND spoke=1 AND is_admin=0 AND is_bot=0 AND outreach_state=? "
                "AND (access_hash='' OR hash_account_id='')",
                (str(account_id), OUTREACH_NONE),
            ).fetchone()
        return int(row[0]) if row else 0

    def cas_outreach(self, group_id: str, user_id: str, *, expect_states: Sequence[str],
                     new_state: str, account_id: str = "", error: str = "",
                     now: Optional[float] = None) -> bool:
        """按当前状态占坑。只有 expect 命中才改，避免两条开口打到同一个人。"""
        expect = [str(s) for s in expect_states if str(s)]
        if not expect or not str(group_id) or not str(user_id):
            return False
        now = time.time() if now is None else float(now)
        ph = ",".join(["?"] * len(expect))
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET outreach_state=?, outreach_account_id=?, "
                "outreach_error=?, outreach_at=? "
                "WHERE group_id=? AND user_id=? AND outreach_state IN (%s)" % ph,
                [str(new_state), str(account_id or ""), str(error or "")[:200], now,
                 str(group_id), str(user_id), *expect],
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def reap_stale_sending(self, now: Optional[float] = None, *,
                           max_age_sec: float = 600.0) -> int:
        """sending 超过 max_age 仍没落定 → 标 skipped，不再对这个人发第二次。

        进程在「Telegram 已收到、状态还没写成 sent」之间挂掉时，这条到底发没发
        不可知。退回 queued 会重发一次；少一个人比对同一个人连发两条便宜。
        """
        now = time.time() if now is None else float(now)
        cutoff = now - float(max_age_sec)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET outreach_state=?, outreach_error=? "
                "WHERE outreach_state=? AND outreach_at>0 AND outreach_at<?",
                (OUTREACH_SKIPPED, "stale_sending", OUTREACH_SENDING, cutoff),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def mark_outreach_replied(self, account_id: str, user_id: str,
                              now: Optional[float] = None, text: str = "") -> int:
        """对方给这个号发私聊了。

        - 发过（sent/closed）→ replied，记首回时间和首回内容（只记第一条，后面的归收件箱）。
        - 还在排队（queued/approved，凭证在这个号手里）→ 也标 replied，错误码 ``inbound_first``：
          人家先来找我们了，冷开口就别再发；``outreach_at`` 保持 0，所以不进回复率分母。
        不改 outreach_at：日额度统计 sent+replied 都按发出那一刻算。
        返回改动行数。
        """
        uid = str(user_id or "").strip()
        acct = str(account_id or "").strip()
        if not uid or not acct:
            return 0
        now = time.time() if now is None else float(now)
        snippet = " ".join(str(text or "").split())[:200]
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tg_group_members SET outreach_state=?, outreach_error='', "
                "replied_at=CASE WHEN replied_at>0 THEN replied_at ELSE ? END, "
                "reply_text=CASE WHEN reply_text!='' THEN reply_text ELSE ? END, "
                "last_in_at=MAX(last_in_at, ?) "
                "WHERE user_id=? AND outreach_account_id=? AND outreach_state IN (?,?)",
                (OUTREACH_REPLIED, now, snippet, now, uid, acct, OUTREACH_SENT, OUTREACH_CLOSED),
            )
            n = int(cur.rowcount or 0)
            # 排队时 cas 写过 outreach_at（排队时刻），这里清零：没发过就不该进任何「发出」口径
            cur2 = self._conn.execute(
                "UPDATE tg_group_members SET outreach_state=?, outreach_error='inbound_first', "
                "outreach_at=0, replied_at=?, reply_text=?, last_in_at=? "
                "WHERE user_id=? AND outreach_account_id=? AND outreach_state IN (?,?)",
                (OUTREACH_REPLIED, now, snippet, now, uid, acct, OUTREACH_QUEUED, OUTREACH_APPROVED),
            )
            n += int(cur2.rowcount or 0)
            # 没私聊开过口、但这个号在群里公开接过 TA 的话（含隐私挡住私聊的人）→ 也算这条线来的：
            # 标 replied / gtouch_inbound，outreach_at=0 不进私聊回复率分母；成交按接话时刻归因
            if not n:
                cur3 = self._conn.execute(
                    "UPDATE tg_group_members SET outreach_state=?, outreach_error='gtouch_inbound', "
                    "outreach_account_id=?, outreach_at=0, replied_at=?, reply_text=?, last_in_at=? "
                    "WHERE user_id=? AND gtouch_account_id=? AND gtouch_state='sent' "
                    "AND (outreach_state=? OR (outreach_state=? AND outreach_error='privacy')) "
                    "AND NOT EXISTS (SELECT 1 FROM tg_group_members r WHERE r.user_id=? "
                    "AND r.outreach_account_id=? AND r.outreach_state=?)",
                    (OUTREACH_REPLIED, acct, now, snippet, now, uid, acct, OUTREACH_NONE,
                     OUTREACH_BLOCKED, uid, acct, OUTREACH_REPLIED))
                n += int(cur3.rowcount or 0)
            # 已在 replied 态的后续来话：推进 last_in_at（「接上后又断了」的计时起点），不计入返回值；
            # 打标之后对方又来话 = 新一轮等待，清掉打标记号（坐席摘过标也允许再打）
            if not n:
                self._conn.execute(
                    "UPDATE tg_group_members SET last_in_at=MAX(last_in_at, ?), "
                    "stalled_flagged_at=CASE WHEN ?>stalled_flagged_at THEN 0 ELSE stalled_flagged_at END "
                    "WHERE user_id=? AND outreach_account_id=? AND outreach_state=?",
                    (now, now, uid, acct, OUTREACH_REPLIED))
            self._conn.commit()
            return n

    def get_hold(self, account_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tg_outreach_holds WHERE account_id=?",
                (str(account_id),),
            ).fetchone()
        if row is None:
            return {"account_id": str(account_id), "paused": False,
                    "flood_until": 0.0, "reason": "", "updated_at": 0.0,
                    "mode": OUTREACH_MODE_MANUAL, "last_flood_at": 0.0, "next_auto_at": 0.0}
        d = dict(row)
        d["paused"] = bool(d.get("paused"))
        d["flood_until"] = float(d.get("flood_until") or 0.0)
        d["mode"] = str(d.get("mode") or OUTREACH_MODE_MANUAL)
        d["last_flood_at"] = float(d.get("last_flood_at") or 0.0)
        d["next_auto_at"] = float(d.get("next_auto_at") or 0.0)
        return d

    def set_hold(self, account_id: str, *, paused: Optional[bool] = None,
                 flood_until: Optional[float] = None, reason: Optional[str] = None,
                 mode: Optional[str] = None, last_flood_at: Optional[float] = None,
                 next_auto_at: Optional[float] = None,
                 now: Optional[float] = None) -> Dict[str, Any]:
        cur = self.get_hold(account_id)
        if paused is not None:
            cur["paused"] = bool(paused)
        if flood_until is not None:
            cur["flood_until"] = float(flood_until)
        if reason is not None:
            cur["reason"] = str(reason)[:200]
        if mode is not None and str(mode) in OUTREACH_MODES:
            cur["mode"] = str(mode)
        if last_flood_at is not None:
            cur["last_flood_at"] = float(last_flood_at)
        if next_auto_at is not None:
            cur["next_auto_at"] = float(next_auto_at)
        now = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tg_outreach_holds "
                "(account_id, paused, flood_until, reason, updated_at, mode, "
                "last_flood_at, next_auto_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "paused=excluded.paused, flood_until=excluded.flood_until, "
                "reason=excluded.reason, updated_at=excluded.updated_at, mode=excluded.mode, "
                "last_flood_at=excluded.last_flood_at, next_auto_at=excluded.next_auto_at",
                (str(account_id), 1 if cur["paused"] else 0, float(cur["flood_until"]),
                 str(cur["reason"] or ""), now, str(cur.get("mode") or OUTREACH_MODE_MANUAL),
                 float(cur.get("last_flood_at") or 0.0), float(cur.get("next_auto_at") or 0.0)),
            )
            # 撞了风控 = Telegram 不认这个号的「老号」身份：申报号龄作废，额度回到新号爬坡
            if last_flood_at is not None and float(last_flood_at) > 0:
                self._conn.execute(
                    "UPDATE tg_outreach_holds SET declared_age_days=0, declared_age_at=0 "
                    "WHERE account_id=?", (str(account_id),))
            self._conn.commit()
        return self.get_hold(account_id)

    def clear_outreach_pause(self, account_id: str = "") -> None:
        """解除人工急停。不清除 flood_until——风控熔断等到点自己过。"""
        now = time.time()
        with self._lock:
            if account_id:
                self._conn.execute(
                    "UPDATE tg_outreach_holds SET paused=0, reason='', updated_at=? "
                    "WHERE account_id=?",
                    (now, str(account_id)),
                )
            else:
                self._conn.execute(
                    "UPDATE tg_outreach_holds SET paused=0, reason='', updated_at=? "
                    "WHERE paused=1",
                    (now,),
                )
            self._conn.commit()

    # ── ops 观测 ─────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """进程无关的库口径汇总（供 ops 卡；``active=false`` 时整卡隐藏）。"""
        with self._lock:
            mt = self._conn.execute("SELECT COUNT(*) FROM tg_group_members").fetchone()
            gt = self._conn.execute(
                "SELECT COUNT(DISTINCT group_id) FROM tg_group_members").fetchone()
            jr = self._conn.execute(
                "SELECT COUNT(*) FROM tg_extract_jobs WHERE status='running'").fetchone()
            jt = self._conn.execute("SELECT COUNT(*) FROM tg_extract_jobs").fetchone()
            srows = self._conn.execute(
                "SELECT outreach_state, COUNT(*) FROM tg_group_members "
                "WHERE outreach_state IN (?,?,?) GROUP BY outreach_state",
                (OUTREACH_SENT, OUTREACH_REPLIED, OUTREACH_CLOSED),
            ).fetchall()
        members_total = int(mt[0]) if mt else 0
        jobs_total = int(jt[0]) if jt else 0
        by = {str(s): int(c or 0) for s, c in srows}
        sent = sum(by.values())
        replied = by.get(OUTREACH_REPLIED, 0)
        return {
            "active": bool(members_total or jobs_total),
            "members_total": members_total,
            "groups": int(gt[0]) if gt else 0,
            "jobs_running": int(jr[0]) if jr else 0,
            "jobs_total": jobs_total,
            # 开口漏斗（库口径累计）：发出过多少、多少回了
            "outreach_sent": sent,
            "outreach_replied": replied,
            "outreach_reply_rate": (replied / sent) if sent else None,
        }

    # ── 行 → dict ────────────────────────────────────────────────────────────

    @staticmethod
    def _member_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for b in ("is_admin", "is_bot", "spoke"):
            d[b] = bool(d.get(b))
        return d

    @staticmethod
    def _job_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for col in _JOB_JSON_COLS:
            raw = d.get(col)
            try:
                d[col] = json.loads(raw) if isinstance(raw, str) and raw else (
                    [] if col == "account_ids" else {})
            except Exception:
                d[col] = [] if col == "account_ids" else {}
        d["stop_requested"] = bool(d.get("stop_requested"))
        return d


# ── 模块级单例（懒建；main.py 可用 configure_* 显式指定库路径）───────────────
_STORE: Optional[GroupMembersStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure_group_members_store(db_path: Any = DEFAULT_DB_PATH) -> Optional[GroupMembersStore]:
    """启动期装配（幂等）。指定库路径并建库。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _DB_PATH = str(db_path)
        if _STORE is None:
            try:
                _STORE = GroupMembersStore(_DB_PATH)
            except Exception:
                logger.warning("[group_members] 建库失败", exc_info=True)
                _STORE = None
        return _STORE


def get_group_members_store() -> Optional[GroupMembersStore]:
    """取 store 单例（未配置则按默认路径懒建）。建库失败返回 None（调用方需容错）。"""
    global _STORE
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                try:
                    _STORE = GroupMembersStore(_DB_PATH)
                except Exception:
                    logger.warning("[group_members] 懒建库失败", exc_info=True)
                    _STORE = None
    return _STORE


def reset_group_members_store() -> None:
    """测试钩子：清空单例。"""
    global _STORE
    with _CFG_LOCK:
        _STORE = None


__all__ = [
    "DEFAULT_DB_PATH", "GroupMembersStore",
    "FILTER_ALL", "FILTER_SPOKE", "FILTER_SPOKE_NO_ADMIN",
    "JOB_DRAFT", "JOB_RUNNING", "JOB_PAUSED", "JOB_DONE", "JOB_STOPPED", "JOB_ERROR",
    "OUTREACH_NONE", "OUTREACH_QUEUED", "OUTREACH_SENDING", "OUTREACH_SENT",
    "OUTREACH_REPLIED", "OUTREACH_SKIPPED", "OUTREACH_BLOCKED", "OUTREACH_CLOSED",
    "OUTREACH_TOUCHED",
    "OUTREACH_APPROVED", "OUTREACH_HOLD_ALL",
    "OUTREACH_MODE_MANUAL", "OUTREACH_MODE_APPROVE", "OUTREACH_MODE_AUTO", "OUTREACH_MODES",
    "configure_group_members_store", "get_group_members_store",
    "reset_group_members_store",
]
