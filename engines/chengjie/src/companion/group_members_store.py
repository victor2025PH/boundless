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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

# 成员触达状态（本 P0 只写 none；后续「触达」步骤流转 queued/sent/replied/skipped）
OUTREACH_NONE = "none"

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
    PRIMARY KEY (group_id, user_id)
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
    "score",
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
        members_total = int(mt[0]) if mt else 0
        jobs_total = int(jt[0]) if jt else 0
        return {
            "active": bool(members_total or jobs_total),
            "members_total": members_total,
            "groups": int(gt[0]) if gt else 0,
            "jobs_running": int(jr[0]) if jr else 0,
            "jobs_total": jobs_total,
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
    "configure_group_members_store", "get_group_members_store",
    "reset_group_members_store",
]
