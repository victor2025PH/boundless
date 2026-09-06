"""营销目标持久层（SQLite）。

三张表：
- ``goals``        目标主表（会话粒度；模板 + 参数 + 里程碑/进度 + 生命周期状态）
- ``goal_actions`` 每日「拍」（beat）：当天的推进意图 + 力度 + 消耗状态。
  幂等键 ``(goal_id, day, kind)``——planner 同日重算不重复建行。
- ``goal_events``  事件台账（创建/里程碑推进/状态变迁/拍消耗），审计与看板下钻用。

镜像 ``companion_funnel_store`` / ``entitlement_store`` 约定：单连接
``check_same_thread=False`` + 写操作 ``threading.Lock`` + **绝不抛**（目标层挂了
不能拖垮聊天主链路）。支持 ``:memory:``（测试零落盘）与文件路径双模式。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.companion.goals.templates import (
    AUTONOMY_LEVELS,
    GOAL_STATUSES,
)

logger = logging.getLogger("src.companion.goals.store")

# 每会话同时活跃目标数上限的硬兜底（配置可更小；防误操作建一堆互相打架的目标）
MAX_ACTIVE_PER_CONVERSATION = 3

ACTION_STATUSES = ("planned", "consumed", "sent", "skipped", "blocked")

# 期限编辑事件明细（goal_routes 写入端唯一口径：``deadline_days:14->30``，
# 可能与其他字段名逗号相连）——deadline_edit_outcomes 据此归队列
_DEADLINE_EDIT_RE = re.compile(r"deadline_days:([0-9.]+)->([0-9.]+)")


def _now() -> float:
    return time.time()


class GoalStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS goals (
        goal_id         TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL DEFAULT '',
        platform        TEXT NOT NULL DEFAULT '',
        account_id      TEXT NOT NULL DEFAULT '',
        chat_key        TEXT NOT NULL DEFAULT '',
        template        TEXT NOT NULL,
        title           TEXT NOT NULL DEFAULT '',
        params          TEXT NOT NULL DEFAULT '{}',
        autonomy        TEXT NOT NULL DEFAULT 'suggest',
        priority        INTEGER NOT NULL DEFAULT 1,
        status          TEXT NOT NULL DEFAULT 'active',
        milestone_idx   INTEGER NOT NULL DEFAULT 0,
        progress        REAL NOT NULL DEFAULT 0,
        start_ts        REAL NOT NULL,
        deadline_ts     REAL NOT NULL DEFAULT 0,
        created_by      TEXT NOT NULL DEFAULT '',
        result          TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        done_at         REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_goals_conv   ON goals(conversation_id, status);
    CREATE INDEX IF NOT EXISTS idx_goals_chat   ON goals(platform, chat_key, status);
    CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, updated_at DESC);

    CREATE TABLE IF NOT EXISTS goal_actions (
        action_id   TEXT PRIMARY KEY,
        goal_id     TEXT NOT NULL,
        day         TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'beat',
        intent      TEXT NOT NULL DEFAULT '',
        push_level  TEXT NOT NULL DEFAULT 'soft',
        status      TEXT NOT NULL DEFAULT 'planned',
        detail      TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_action_day
        ON goal_actions(goal_id, day, kind);
    CREATE INDEX IF NOT EXISTS idx_goal_actions_goal
        ON goal_actions(goal_id, day DESC);

    CREATE TABLE IF NOT EXISTS goal_events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id TEXT NOT NULL,
        kind    TEXT NOT NULL,
        detail  TEXT NOT NULL DEFAULT '',
        ts      REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id, ts DESC);

    CREATE TABLE IF NOT EXISTS customer_profiles (
        platform   TEXT NOT NULL DEFAULT '',
        chat_key   TEXT NOT NULL DEFAULT '',
        fields     TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (platform, chat_key)
    );
    """

    def __init__(self, db_path):
        self._db_path = db_path if db_path == ":memory:" else Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    @classmethod
    def open_readonly(cls, db_path) -> "GoalStore":
        """只读打开（``mode=ro`` URI，不建表不迁移）——周报/巡检 CLI 对活体
        生产库零写事务的 sanctioned 入口（与 persona_media 回填的只读连接
        同纪律）。库不存在/不可读会抛，调用方自兜。写方法在该实例上会因
        SQLite 只读连接直接报错——这是特性不是缺陷。"""
        self = cls.__new__(cls)
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            f"file:{self._db_path}?mode=ro", uri=True,
            check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        return self

    # M-7 A（#236）：事件表追溯列——beat_sent/beat_blocked/beat_injected 要能回答
    # 「发到哪个会话 / 哪条消息 / 说了什么」。ALTER ADD COLUMN 幂等（缺列才补），
    # 存量库零迁移脚本；既有事件行三列为空串，语义不变。
    _EVENT_TRACE_COLUMNS = (
        ("conversation_id", "TEXT NOT NULL DEFAULT ''"),
        ("message_id", "TEXT NOT NULL DEFAULT ''"),
        ("text_head", "TEXT NOT NULL DEFAULT ''"),
    )

    def _init_db(self) -> None:
        if self._db_path != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path) if self._db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._migrate_event_trace_columns()
        self._conn.commit()

    def _migrate_event_trace_columns(self) -> None:
        try:
            have = {
                str(r[1]) for r in self._conn.execute(
                    "PRAGMA table_info(goal_events)").fetchall()}
            for col, ddl in self._EVENT_TRACE_COLUMNS:
                if col not in have:
                    self._conn.execute(
                        f"ALTER TABLE goal_events ADD COLUMN {col} {ddl}")
        except Exception as e:  # noqa: BLE001
            logger.debug("goal_events trace column migration skipped: %s", e)

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ── goals：写 ───────────────────────────────────────────────────────────
    def create_goal(
        self,
        *,
        conversation_id: str = "",
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        template: str,
        title: str = "",
        params: Optional[Dict[str, Any]] = None,
        autonomy: str = "suggest",
        priority: int = 1,
        deadline_days: float = 0.0,
        deadline_ts: float = 0.0,
        created_by: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """建目标（active 起步）。失败/非法输入 → None（绝不抛）。"""
        tmpl = str(template or "").strip()
        if not tmpl:
            return None
        auto = str(autonomy or "suggest").strip().lower()
        if auto not in AUTONOMY_LEVELS:
            auto = "suggest"
        n = float(now if now is not None else _now())
        dl = float(deadline_ts or 0.0)
        if dl <= 0 and float(deadline_days or 0) > 0:
            dl = n + float(deadline_days) * 86400.0
        gid = uuid.uuid4().hex[:16]
        try:
            pjson = json.dumps(dict(params or {}), ensure_ascii=False)
        except Exception:
            pjson = "{}"
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO goals (goal_id, conversation_id, platform,"
                    " account_id, chat_key, template, title, params, autonomy,"
                    " priority, status, milestone_idx, progress, start_ts,"
                    " deadline_ts, created_by, result, created_at, updated_at,"
                    " done_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gid, str(conversation_id or ""), str(platform or ""),
                     str(account_id or ""), str(chat_key or ""), tmpl,
                     str(title or "")[:120], pjson, auto,
                     int(priority or 1), "active", 0, 0.0, n,
                     dl, str(created_by or "")[:60], "", n, n, 0.0),
                )
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("create_goal failed: %s", e)
            return None
        self.add_event(gid, "created", tmpl, conversation_id=str(conversation_id or ""))
        # M-7 B（#236）：目标状态变更每次一行 [goal-state] INFO（创建 / 状态 / 里程碑 /
        # 期限 / 自治档）。skuio 复报「近 3h 无目标创建/重置日志，无法判断深化→起步是
        # 用户重建还是引擎重置」——事件其实一直在写，只是没有一行日志、且本包 logger
        # 此前不在 src.* 落盘命名空间。所有建目标路径（路由 / 批量 / auto_create /
        # 留存 / 挽回 / 再转化）都经这里，一处落日志全覆盖。
        logger.info(
            "[goal-state] created goal=%s conv=%s template=%s autonomy=%s "
            "deadline_days=%.2f by=%s title=%r",
            gid, str(conversation_id or "") or f"{platform}:{account_id}:{chat_key}",
            tmpl, auto, ((dl - n) / 86400.0) if dl > 0 else 0.0,
            str(created_by or "") or "-", str(title or "")[:30])
        return self.get_goal(gid)

    def update_goal_fields(self, goal_id: str, **fields: Any) -> bool:
        """按白名单更新目标列。空/非法字段忽略；失败 False。"""
        allowed = {
            "status", "milestone_idx", "progress", "result", "title",
            "autonomy", "priority", "deadline_ts", "done_at", "params",
        }
        sets: List[str] = []
        vals: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "status" and str(v) not in GOAL_STATUSES:
                continue
            if k == "autonomy" and str(v) not in AUTONOMY_LEVELS:
                continue
            if k == "params":
                try:
                    v = json.dumps(dict(v or {}), ensure_ascii=False)
                except Exception:
                    continue
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(str(goal_id or ""))
        try:
            with self._lock:
                c = self._conn.execute(
                    f"UPDATE goals SET {', '.join(sets)} WHERE goal_id = ?",
                    tuple(vals),
                )
                self._conn.commit()
                ok = c.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("update_goal_fields failed: %s", e)
            return False
        # M-7 B（#236）：状态类字段变更落一行 [goal-state]（progress/params/title 的
        # 日常刷新不打，防刷屏）。是谁改的由调用方在 goal_events 里写 detail
        # （status:…:manual / :deadline / milestone a->b），这里只保证「变了」可见。
        if ok:
            keys = {"status", "milestone_idx", "deadline_ts", "autonomy"}
            hit = {k: fields[k] for k in fields if k in keys}
            if hit:
                try:
                    logger.info(
                        "[goal-state] updated goal=%s %s",
                        str(goal_id or ""),
                        " ".join(f"{k}={v}" for k, v in sorted(hit.items())))
                except Exception:
                    pass
        return ok

    # ── goals：读 ───────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_goal(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except Exception:
            d["params"] = {}
        return d

    def get_goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._conn.execute(
                "SELECT * FROM goals WHERE goal_id = ?", (str(goal_id or ""),)
            ).fetchone()
        except Exception:
            return None
        return self._row_to_goal(row) if row else None

    def find_active_goal(
        self,
        *,
        conversation_id: str = "",
        platform: str = "",
        chat_key: str = "",
        account_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """当前会话的活跃目标（优先 conversation_id 精确命中；回落 platform+chat_key）。
        多条取 priority 高、新建优先。

        **账号锁**（P4 2026-08-09 修串号）：(platform, chat_key) 通配回落**只在
        调用方没给 account_id 时**才走——它的存在理由是「A 线 process_message
        拿不到 account_id 仍能命中」；调用方明确知道账号却借到别人的目标＝串号。
        实锤：两个账号的收藏消息（chat_key 同为 'me'）解析到同一个目标；订单
        回流的兜底匹配走同一口，存在跨账号误结算面。给了账号而账号内无命中
        → 如实返回 None，绝不再放宽。"""
        try:
            if conversation_id:
                row = self._conn.execute(
                    "SELECT * FROM goals WHERE conversation_id = ? AND status = 'active'"
                    " ORDER BY priority DESC, created_at DESC LIMIT 1",
                    (str(conversation_id),),
                ).fetchone()
                if row:
                    return self._row_to_goal(row)
            if platform and chat_key:
                if account_id:
                    row = self._conn.execute(
                        "SELECT * FROM goals WHERE platform = ? AND chat_key = ?"
                        " AND account_id = ? AND status = 'active'"
                        " ORDER BY priority DESC, created_at DESC LIMIT 1",
                        (str(platform), str(chat_key), str(account_id)),
                    ).fetchone()
                    return self._row_to_goal(row) if row else None
                row = self._conn.execute(
                    "SELECT * FROM goals WHERE platform = ? AND chat_key = ?"
                    " AND status = 'active'"
                    " ORDER BY priority DESC, created_at DESC LIMIT 1",
                    (str(platform), str(chat_key)),
                ).fetchone()
                if row:
                    return self._row_to_goal(row)
        except Exception as e:  # noqa: BLE001
            # #166：读库异常此前 DEBUG → 对调用方与「无目标」无法区分（拟稿链
            # 表现成 no_goal、右栏卡却能查到）。升 WARNING 带异常类名 + 查找键。
            logger.warning(
                "find_active_goal failed: %s: %s (conv=%s plat=%s chat_key=%s acct=%s)",
                type(e).__name__, str(e)[:160], conversation_id or "-",
                platform or "-", chat_key or "-", account_id or "-")
        return None

    def count_active_for_conversation(self, conversation_id: str) -> int:
        try:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM goals WHERE conversation_id = ?"
                " AND status = 'active'",
                (str(conversation_id or ""),),
            ).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

    def has_any_goal(
        self,
        *,
        conversation_id: str = "",
        platform: str = "",
        chat_key: str = "",
    ) -> bool:
        """会话历史上建过任何目标（含终态）——auto_create 幂等闸：
        建过（哪怕已 done/cancelled）就不再自动建，避免对同一客户反复起盘。
        匹配口径与 find_active_goal 同宽（conversation_id 或 platform+chat_key
        任一命中即算有）。查询失败按「有」处理（宁可不建，别重复建）。"""
        try:
            if conversation_id:
                r = self._conn.execute(
                    "SELECT 1 FROM goals WHERE conversation_id = ? LIMIT 1",
                    (str(conversation_id),),
                ).fetchone()
                if r:
                    return True
            if platform and chat_key:
                r = self._conn.execute(
                    "SELECT 1 FROM goals WHERE platform = ? AND chat_key = ?"
                    " LIMIT 1",
                    (str(platform), str(chat_key)),
                ).fetchone()
                if r:
                    return True
            return False
        except Exception:
            return True

    def count_created_by_since(self, created_by: str, since_ts: float) -> int:
        """某创建者自 since_ts 起建的目标数（auto_create 每日预算闸；
        进库计数=跨重启稳）。失败返回大数（fail-closed 不超预算）。"""
        try:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM goals WHERE created_by = ?"
                " AND created_at >= ?",
                (str(created_by or ""), float(since_ts or 0.0)),
            ).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 10 ** 9

    def list_overdue_goals(
        self,
        *,
        template: str,
        before_ts: float,
        after_ts: float = 0.0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """某模板下 deadline 落在 ``(after_ts, before_ts]`` 窗口的到期目标
        （status ∈ active/expired——active=还没被 settle-on-read 扫到的静默会话，
        winback 扫描顺手把它们结算掉）。P6 流失挽回扫描的候选源。失败返回 []。"""
        try:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE template = ?"
                " AND status IN ('active', 'expired')"
                " AND deadline_ts > ? AND deadline_ts <= ?"
                " ORDER BY deadline_ts ASC LIMIT ?",
                (str(template or ""), float(after_ts or 0.0),
                 float(before_ts or 0.0), max(1, min(int(limit or 50), 200))),
            ).fetchall()
            return [self._row_to_goal(r) for r in rows]
        except Exception:
            logger.debug("list_overdue_goals failed", exc_info=True)
            return []

    def has_goal_created_after(
        self,
        ts: float,
        *,
        conversation_id: str = "",
        platform: str = "",
        chat_key: str = "",
    ) -> bool:
        """会话在 ``ts`` 之后建过任何目标（winback 幂等闸：流失点之后已有
        新目标——挽回目标 / 迟到续费起的新周期 / 手动新盘——就不再挽回）。
        匹配口径与 find_active_goal 同宽；查询失败按「有」处理（宁可不建）。"""
        try:
            t = float(ts or 0.0)
            if conversation_id:
                r = self._conn.execute(
                    "SELECT 1 FROM goals WHERE conversation_id = ?"
                    " AND created_at > ? LIMIT 1",
                    (str(conversation_id), t),
                ).fetchone()
                if r:
                    return True
            if platform and chat_key:
                r = self._conn.execute(
                    "SELECT 1 FROM goals WHERE platform = ? AND chat_key = ?"
                    " AND created_at > ? LIMIT 1",
                    (str(platform), str(chat_key), t),
                ).fetchone()
                if r:
                    return True
            return False
        except Exception:
            return True

    def order_event_exists(
        self,
        *,
        order_id: str,
        conversation_id: str = "",
        platform: str = "",
        chat_key: str = "",
    ) -> bool:
        """同会话**任一**目标（含终态）是否已回流过该订单号——settle_order_ref
        的会话级幂等闸（P5）。留存环 spawn 后会话常年挂着活跃目标，幂等若只看
        「当前活跃目标」的事件，旧单重放（order_pull 的 paid→activated 二次出现、
        进程重启 _SEEN 清空）会假结算新周期。detail 格式 ``order_id|plan``，
        Python 侧子串匹配（与旧逻辑同口径，免 LIKE 转义坑）。
        查询失败按「已存在」处理（宁可漏结算一单，别假续费）。"""
        oid = str(order_id or "").strip()
        if not oid:
            return False
        try:
            rows: list = []
            if conversation_id:
                rows = self._conn.execute(
                    "SELECT e.detail FROM goal_events e JOIN goals g"
                    " ON e.goal_id = g.goal_id WHERE e.kind = 'order'"
                    " AND g.conversation_id = ? ORDER BY e.ts DESC LIMIT 400",
                    (str(conversation_id),),
                ).fetchall()
            if not rows and platform and chat_key:
                rows = self._conn.execute(
                    "SELECT e.detail FROM goal_events e JOIN goals g"
                    " ON e.goal_id = g.goal_id WHERE e.kind = 'order'"
                    " AND g.platform = ? AND g.chat_key = ?"
                    " ORDER BY e.ts DESC LIMIT 400",
                    (str(platform), str(chat_key)),
                ).fetchall()
            return any(oid in str(r["detail"] or "") for r in rows)
        except Exception:
            logger.debug("order_event_exists failed", exc_info=True)
            return True

    def list_goals(
        self,
        *,
        status: str = "",
        platform: str = "",
        account_id: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 100), 500))
        sql = "SELECT * FROM goals WHERE 1=1"
        params: List[Any] = []
        st = str(status or "").strip()
        if st:
            sql += " AND status = ?"
            params.append(st)
        if platform:
            sql += " AND platform = ?"
            params.append(str(platform))
        if account_id:
            sql += " AND account_id = ?"
            params.append(str(account_id))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(lim)
        try:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        except Exception:
            return []
        return [self._row_to_goal(r) for r in rows]

    def summary(self) -> Dict[str, Any]:
        """看板/观测聚合：按状态计数 + 活跃目标按模板分布。绝不抛。"""
        out: Dict[str, Any] = {"by_status": {}, "active_by_template": {}, "total": 0}
        try:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM goals GROUP BY status").fetchall()
            for r in rows:
                out["by_status"][str(r[0])] = int(r[1])
                out["total"] += int(r[1])
            rows = self._conn.execute(
                "SELECT template, COUNT(*) FROM goals WHERE status = 'active'"
                " GROUP BY template").fetchall()
            for r in rows:
                out["active_by_template"][str(r[0])] = int(r[1])
        except Exception as e:  # noqa: BLE001
            logger.debug("summary failed: %s", e)
        return out

    # ── actions（每日拍）─────────────────────────────────────────────────────
    def upsert_action(
        self,
        goal_id: str,
        day: str,
        *,
        kind: str = "beat",
        intent: str = "",
        push_level: str = "soft",
        status: str = "planned",
        detail: str = "",
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """幂等建当日拍：已存在 → 返回既有行（不覆盖，防重算抖动）。"""
        gid = str(goal_id or "").strip()
        d = str(day or "").strip()
        if not gid or not d:
            return None
        n = float(now if now is not None else _now())
        aid = uuid.uuid4().hex[:16]
        st = status if status in ACTION_STATUSES else "planned"
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO goal_actions (action_id, goal_id, day,"
                    " kind, intent, push_level, status, detail, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (aid, gid, d, str(kind or "beat"), str(intent or "")[:300],
                     str(push_level or "soft"), st, str(detail or "")[:300], n, n),
                )
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("upsert_action failed: %s", e)
            return None
        return self.get_action(gid, d, kind=kind)

    def get_action(
        self, goal_id: str, day: str, *, kind: str = "beat"
    ) -> Optional[Dict[str, Any]]:
        try:
            row = self._conn.execute(
                "SELECT * FROM goal_actions WHERE goal_id = ? AND day = ?"
                " AND kind = ?",
                (str(goal_id or ""), str(day or ""), str(kind or "beat")),
            ).fetchone()
        except Exception:
            return None
        return dict(row) if row else None

    def mark_action(
        self, action_id: str, status: str, *, detail: str = ""
    ) -> bool:
        st = str(status or "").strip()
        if st not in ACTION_STATUSES:
            return False
        try:
            with self._lock:
                c = self._conn.execute(
                    "UPDATE goal_actions SET status = ?, detail = ?,"
                    " updated_at = ? WHERE action_id = ?",
                    (st, str(detail or "")[:300], _now(), str(action_id or "")),
                )
                self._conn.commit()
                return c.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_action failed: %s", e)
            return False

    def delete_planned_action(
        self, goal_id: str, day: str, *, kind: str = "beat"
    ) -> bool:
        """删除某日**还没发生任何事**的拍（status=planned 且无坐席反馈标记），
        让下一次 ``refresh_goal`` 按新参数重排——「改期限当天生效」的唯一口径。

        状态过滤写死在 SQL：``consumed/sent`` 是既成事实（已进过生成/已发出，
        删了会把沉默熔断的 engaged 计数抹掉）、``detail=adopted/rejected:*`` 是
        坐席的明示决定——都绝不能被一次改期限顺手清除。失败返回 False，绝不抛。"""
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM goal_actions WHERE goal_id = ? AND day = ?"
                    " AND kind = ? AND status = 'planned'"
                    " AND (detail = '' OR detail IS NULL)",
                    (str(goal_id or ""), str(day or ""), str(kind or "beat")),
                )
                self._conn.commit()
                return c.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("delete_planned_action failed: %s", e)
            return False

    def list_actions(
        self, goal_id: str, *, limit: int = 14
    ) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 14), 120))
        try:
            rows = self._conn.execute(
                "SELECT * FROM goal_actions WHERE goal_id = ?"
                " ORDER BY day DESC LIMIT ?",
                (str(goal_id or ""), lim),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def count_engaged_since(self, goal_id: str, since_ts: float) -> int:
        """自 ``since_ts`` 起已「进入生成/已发出」的拍数——planner 判退避用
        （对方最近一次开口后，我们已经带着目标说了几次）。"""
        try:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM goal_actions WHERE goal_id = ?"
                " AND status IN ('consumed', 'sent') AND updated_at > ?",
                (str(goal_id or ""), float(since_ts or 0.0)),
            ).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

    # ── 客户画像（P1：BANT 商机 + 关系双轨；slot 语义见 profile_slots）──────────
    def get_customer_profile(
        self, platform: str, chat_key: str
    ) -> Optional[Dict[str, Any]]:
        """画像行（fields 已反序列化）。无记录/坏行 → None。"""
        try:
            row = self._conn.execute(
                "SELECT * FROM customer_profiles WHERE platform = ?"
                " AND chat_key = ?",
                (str(platform or ""), str(chat_key or "")),
            ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        d = dict(row)
        try:
            d["fields"] = json.loads(d.get("fields") or "{}")
            if not isinstance(d["fields"], dict):
                d["fields"] = {}
        except Exception:
            d["fields"] = {}
        return d

    def upsert_customer_profile(
        self,
        platform: str,
        chat_key: str,
        updates: Dict[str, Any],
        *,
        source: str = "auto",
        overwrite: bool = False,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """合并写画像槽位。返回合并后的行；无有效更新 → 返回现状（可能 None）。

        写入规则（防「机器猜的覆盖人核实的」）：
        - ``overwrite=False``（auto 采集）：只填**空槽**；已有值（无论 auto/agent）不动。
        - ``overwrite=True``（坐席画像卡）：覆盖一切；传空串 = 清除该槽。
        每槽存 ``{"v", "src", "ts"}``；槽位键不在注册表的忽略（防脏键撑爆）。
        """
        from src.companion.goals.profile_slots import get_slot
        pf = str(platform or "").strip()
        ck = str(chat_key or "").strip()
        if not pf or not ck or not isinstance(updates, dict):
            return self.get_customer_profile(pf, ck)
        n = float(now if now is not None else _now())
        cur = self.get_customer_profile(pf, ck)
        fields: Dict[str, Any] = dict((cur or {}).get("fields") or {})
        changed = False
        for key, raw in updates.items():
            k = str(key or "").strip().lower()
            if get_slot(k) is None:
                continue
            v = str(raw or "").strip()[:80]
            old = fields.get(k) if isinstance(fields.get(k), dict) else None
            old_v = str((old or {}).get("v") or "").strip()
            if not overwrite:
                if not v or old_v:
                    continue        # auto 只填空槽
                fields[k] = {"v": v, "src": str(source or "auto")[:12], "ts": n}
                changed = True
            else:
                if not v:
                    if k in fields:
                        fields.pop(k, None)
                        changed = True
                    continue
                if v == old_v:
                    continue
                fields[k] = {"v": v, "src": str(source or "agent")[:12], "ts": n}
                changed = True
        if not changed:
            return cur
        try:
            fjson = json.dumps(fields, ensure_ascii=False)
        except Exception:
            return cur
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO customer_profiles (platform, chat_key, fields,"
                    " created_at, updated_at) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(platform, chat_key) DO UPDATE SET"
                    " fields = excluded.fields, updated_at = excluded.updated_at",
                    (pf, ck, fjson, n, n),
                )
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("upsert_customer_profile failed: %s", e)
            return cur
        return self.get_customer_profile(pf, ck)

    # ── 结果闭环（P2）─────────────────────────────────────────────────────────
    def sold_plan_counts(
        self, days: int = 90, *, now: Optional[float] = None
    ) -> Dict[str, int]:
        """近 N 天经聊天归因真实成交的订单按 plan 计数（P4 选品反哺）。

        口径与 ``outcome_report.orders_by_plan`` 同：result=``order:<plan>:<id>``；
        手动「标成交」（result=manual:agent）刻意不计——读的是「哪个 SKU 在
        聊天里真卖动了」。失败返回空（选品退回纯 pains 排序，零阻断）。"""
        try:
            n = float(now if now is not None else _now())
            since = n - max(1, int(days or 90)) * 86400.0
            rows = self._conn.execute(
                "SELECT result FROM goals WHERE status = 'done'"
                " AND done_at >= ? AND result LIKE 'order:%'", (since,),
            ).fetchall()
            out: Dict[str, int] = {}
            for r in rows:
                parts = str(r["result"] or "").split(":", 2)
                plan = (parts[1] if len(parts) > 1 else "").strip()
                if plan:
                    out[plan] = out.get(plan, 0) + 1
            return out
        except Exception:
            return {}

    def count_events_since(self, goal_id: str, kind: str, since_ts: float) -> int:
        """某目标自 ``since_ts`` 起某类事件数（planner 读坐席驳回回流用）。"""
        try:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM goal_events WHERE goal_id = ? AND kind = ?"
                " AND ts > ?",
                (str(goal_id or ""), str(kind or ""), float(since_ts or 0.0)),
            ).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

    def outcome_report(
        self, since_ts: float, *, now: Optional[float] = None
    ) -> Dict[str, Any]:
        """窗口期结果聚合（哪类模板成功率高 → 周审调模板/曲线的读数面）。

        口径：终态目标按 ``done_at`` 落窗；拍/反馈按事件时间落窗。全 SQL 聚合
        （零 Python 循环大表），绝不抛——坏库返回空骨架。"""
        n = float(now if now is not None else _now())
        s = float(since_ts or 0.0)
        out: Dict[str, Any] = {
            "since_ts": s, "now": n,
            "totals": {"done": 0, "failed": 0, "expired": 0, "cancelled": 0,
                       "n": 0, "done_rate": 0.0, "avg_days_to_done": None,
                       "won": 0, "won_rate": 0.0},
            "by_template": {},
            # P1（2026-08-29）：按推进节奏拆终态（natural/today/session）——
            # 「限时收口的达成率 vs 自然天」是限时档该不该继续放量的读数
            "by_pace": {},
            # P2（2026-08-29）：达成信号读数——正式轨 outcome_signal +
            # 影子轨 shadow_*（约时间/付款）。「影子命中的目标最终 done 多少」
            # ＝影子轨要不要升级为正式提示的判据
            "outcome_signals": {},
            "beats": {"planned": 0, "consumed": 0, "sent": 0, "skipped": 0},
            "feedback": {"adopt": 0, "reject": 0},
            "recent": [],
            "active_now": 0,
            "orders_by_plan": {},
            "orders_settled": 0,
            "churn_reasons": {},
            # P12：生命周期终态 × 主流失原因（won=真金白银/手动成交）
            "churn_outcomes": {},
            # P23：「采纳并拟稿」耐久使用面（goal_events kind=drive_draft，
            # 进程 ui-event 计数重启即清零，周审只能信 DB 口径）
            "drive_draft": {"total": 0, "by_source": {}},
            # P23：画像槽位填充漏斗（按槽位自身 ts 落窗；src=auto/agent/llm）
            "profile_fills": {"total": 0, "by_src": {}, "by_track": {}},
        }
        try:
            rows = self._conn.execute(
                "SELECT template, status, COUNT(*) AS n,"
                " AVG(progress) AS avg_progress,"
                " AVG(CASE WHEN done_at > 0 THEN (done_at - start_ts) / 86400.0"
                " END) AS avg_days"
                " FROM goals WHERE status IN ('done','failed','expired','cancelled')"
                " AND done_at >= ? GROUP BY template, status",
                (s,),
            ).fetchall()
            for r in rows:
                tmpl = str(r["template"])
                st = str(r["status"])
                cnt = int(r["n"])
                bt = out["by_template"].setdefault(tmpl, {
                    "done": 0, "failed": 0, "expired": 0, "cancelled": 0,
                    "n": 0, "done_rate": 0.0,
                    "avg_days_to_done": None, "avg_progress": 0.0})
                bt[st] = cnt
                bt["n"] += cnt
                if st == "done" and r["avg_days"] is not None:
                    bt["avg_days_to_done"] = round(float(r["avg_days"]), 1)
                    # 顶层加权平均（各模板 done 数为权）
                    t0 = out["totals"]
                    prev_done = int(t0.get("done") or 0)
                    prev_avg = t0.get("avg_days_to_done")
                    acc = (float(prev_avg) * prev_done
                           if prev_avg is not None else 0.0)
                    t0["avg_days_to_done"] = round(
                        (acc + float(r["avg_days"]) * cnt)
                        / (prev_done + cnt), 1)
                # 加权平均 progress（跨 status 行合并）
                prev_n = bt["n"] - cnt
                prev = float(bt["avg_progress"]) * prev_n
                bt["avg_progress"] = round(
                    (prev + float(r["avg_progress"] or 0.0) * cnt) / bt["n"], 3)
                out["totals"][st] = out["totals"].get(st, 0) + cnt
                out["totals"]["n"] += cnt
            # P23：win-rate（won=真金白银——result 前缀 order:/manual:，与
            # churn_outcomes 同判定；winback done=回话≠成交刻意不进 won）。
            # 分母与 done_rate 同（organic，排除 cancelled），两率可直接横比。
            try:
                rows = self._conn.execute(
                    "SELECT template, COUNT(*) AS n FROM goals"
                    " WHERE status = 'done' AND done_at >= ?"
                    " AND (result LIKE 'order:%' OR result LIKE 'manual:%')"
                    " GROUP BY template", (s,),
                ).fetchall()
                for r in rows:
                    bt = out["by_template"].setdefault(str(r["template"]), {
                        "done": 0, "failed": 0, "expired": 0, "cancelled": 0,
                        "n": 0, "done_rate": 0.0,
                        "avg_days_to_done": None, "avg_progress": 0.0})
                    bt["won"] = int(r["n"])
                    out["totals"]["won"] += int(r["n"])
            except Exception:
                pass
            # done_rate 分母刻意排除 cancelled（运营手动叫停≠客户结果，
            # 混进来会让批量清目标把成功率砸穿）
            for bt in out["by_template"].values():
                organic = bt["done"] + bt["failed"] + bt["expired"]
                bt.setdefault("won", 0)
                if organic:
                    bt["done_rate"] = round(bt["done"] / organic, 3)
                    bt["won_rate"] = round(int(bt["won"]) / organic, 3)
                else:
                    bt.setdefault("won_rate", 0.0)
            t = out["totals"]
            organic = t["done"] + t["failed"] + t["expired"]
            if organic:
                t["done_rate"] = round(t["done"] / organic, 3)
                t["won_rate"] = round(int(t["won"]) / organic, 3)

            # P1（2026-08-29）按节奏拆终态：pace 藏在 params JSON + 期限跨度
            # 推断（resolve_pace 单一口径）——SQL 聚合做不对白名单校验，改
            # 拉窗口内终态行（LIMIT 兜底）Python 分组，正确性优先。
            try:
                from src.companion.goals.pace import resolve_pace
                rows = self._conn.execute(
                    "SELECT template, params, start_ts, deadline_ts, status,"
                    " done_at, result FROM goals"
                    " WHERE status IN ('done','failed','expired','cancelled')"
                    " AND done_at >= ? LIMIT 5000", (s,),
                ).fetchall()
                for r in rows:
                    try:
                        pjson = json.loads(r["params"] or "{}")
                    except Exception:
                        pjson = {}
                    pace = resolve_pace({
                        "template": str(r["template"] or ""),
                        "params": pjson,
                        "start_ts": r["start_ts"],
                        "deadline_ts": r["deadline_ts"],
                    })
                    st = str(r["status"])
                    bp = out["by_pace"].setdefault(pace, {
                        "done": 0, "failed": 0, "expired": 0, "cancelled": 0,
                        "n": 0, "won": 0, "done_rate": 0.0, "won_rate": 0.0,
                        "avg_hours_to_done": None, "_done_hours": 0.0})
                    bp[st] = int(bp.get(st) or 0) + 1
                    bp["n"] += 1
                    if st == "done":
                        res_s = str(r["result"] or "")
                        if res_s.startswith(("order:", "manual:")):
                            bp["won"] += 1
                        try:
                            da = float(r["done_at"] or 0)
                            st0 = float(r["start_ts"] or 0)
                            if da > st0 > 0:
                                bp["_done_hours"] += (da - st0) / 3600.0
                        except (TypeError, ValueError):
                            pass
                for bp in out["by_pace"].values():
                    organic = bp["done"] + bp["failed"] + bp["expired"]
                    if organic:
                        bp["done_rate"] = round(bp["done"] / organic, 3)
                        bp["won_rate"] = round(bp["won"] / organic, 3)
                    hours = bp.pop("_done_hours", 0.0)
                    if bp["done"]:
                        bp["avg_hours_to_done"] = round(
                            hours / bp["done"], 1)
            except Exception:
                pass

            # P2（2026-08-29）：达成信号 × 目标当前状态（正式轨 + 影子轨）。
            # 每格＝「窗口内记过该信号的目标数」按现状分桶——影子轨的
            # done 占比就是「检测器值不值得升级为正式提示」的读数。
            try:
                rows = self._conn.execute(
                    "SELECT e.kind AS kind, g.status AS status,"
                    " COUNT(DISTINCT e.goal_id) AS n"
                    " FROM goal_events e JOIN goals g ON g.goal_id = e.goal_id"
                    " WHERE e.ts >= ? AND e.kind IN ('outcome_signal',"
                    " 'outcome_shadow_appointment','outcome_shadow_payment')"
                    " GROUP BY e.kind, g.status", (s,),
                ).fetchall()
                for r in rows:
                    ent = out["outcome_signals"].setdefault(
                        str(r["kind"]), {"n": 0})
                    ent[str(r["status"])] = (
                        int(ent.get(str(r["status"])) or 0) + int(r["n"]))
                    ent["n"] += int(r["n"])
            except Exception:
                pass

            # 终态时的里程碑分布（获客漏斗「死在哪一段」读数；cancelled 不计——
            # 运营叫停不代表客户走到哪）
            rows = self._conn.execute(
                "SELECT template, milestone_idx, COUNT(*) AS n FROM goals"
                " WHERE status IN ('done','failed','expired') AND done_at >= ?"
                " GROUP BY template, milestone_idx", (s,),
            ).fetchall()
            for r in rows:
                bt = out["by_template"].get(str(r["template"]))
                if bt is not None:
                    dist = bt.setdefault("milestone_dist", {})
                    dist[str(int(r["milestone_idx"] or 0))] = int(r["n"])

            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM goal_actions"
                " WHERE created_at >= ? GROUP BY status", (s,),
            ).fetchall()
            for r in rows:
                st = str(r["status"])
                if st in out["beats"]:
                    out["beats"][st] = int(r["n"])

            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM goal_events"
                " WHERE ts >= ? AND kind IN ('beat_adopted','beat_rejected')"
                " GROUP BY kind", (s,),
            ).fetchall()
            for r in rows:
                key = "adopt" if str(r["kind"]) == "beat_adopted" else "reject"
                out["feedback"][key] = int(r["n"])

            # P23：「采纳并拟稿」使用面（desktop smart-reply 带 goal_id 时落
            # kind=drive_draft，detail=来源 beat/hero/slot）。计的是「指令
            # 驱动的生成次数」——同一稿重生成算多次，命名如实。
            try:
                rows = self._conn.execute(
                    "SELECT COALESCE(NULLIF(TRIM(detail),''),'-') AS src,"
                    " COUNT(*) AS n FROM goal_events"
                    " WHERE kind = 'drive_draft' AND ts >= ? GROUP BY src",
                    (s,),
                ).fetchall()
                by_src: Dict[str, int] = {}
                for r in rows:
                    by_src[str(r["src"])] = int(r["n"])
                out["drive_draft"] = {
                    "total": sum(by_src.values()), "by_source": by_src}
            except Exception:
                pass

            rows = self._conn.execute(
                "SELECT goal_id, template, title, status, progress, start_ts,"
                " done_at FROM goals"
                " WHERE status IN ('done','failed','expired','cancelled')"
                " AND done_at >= ? ORDER BY done_at DESC LIMIT 10", (s,),
            ).fetchall()
            out["recent"] = [{
                "goal_id": str(r["goal_id"]), "template": str(r["template"]),
                "title": str(r["title"] or ""), "status": str(r["status"]),
                "progress": float(r["progress"] or 0.0),
                "days": round((float(r["done_at"]) - float(r["start_ts"]))
                              / 86400.0, 1) if float(r["done_at"] or 0) > 0 else None,
                "done_at": float(r["done_at"] or 0),
            } for r in rows]

            # 成交单按 plan 拆分（order-hook/order_pull 结算的 result 形如
            # ``order:<plan>:<order_id>``；手动「标成交」result=manual:agent
            # 刻意不计——这里读的是「哪个 SKU 在聊天里真卖动了」，反哺
            # site_catalog 的 pains→产品映射与主推序）
            rows = self._conn.execute(
                "SELECT result FROM goals WHERE status = 'done'"
                " AND done_at >= ? AND result LIKE 'order:%'", (s,),
            ).fetchall()
            by_plan: Dict[str, int] = {}
            for r in rows:
                parts = str(r["result"] or "").split(":", 2)
                plan = (parts[1] if len(parts) > 1 else "").strip() or "-"
                by_plan[plan] = by_plan.get(plan, 0) + 1
            out["orders_by_plan"] = by_plan
            out["orders_settled"] = sum(by_plan.values())

            # 流失原因分布（P8）：画像里 lifecycle 采到的 churn_reason 按
            # 分类标签计数（值形如「太贵、没用起来」→ 逐标签拆）——定价/
            # 引导策略的直接读数（「都嫌贵」调价，「都没用起来」补 onboarding）。
            # 窗口按槽位自己的 ts（行级 updated_at 会被别的槽位刷新）。
            rows = self._conn.execute(
                "SELECT fields FROM customer_profiles"
                " WHERE fields LIKE '%churn_reason%'",
            ).fetchall()
            reasons: Dict[str, int] = {}
            for r in rows:
                try:
                    cell = (json.loads(r["fields"] or "{}")
                            .get("churn_reason") or {})
                except Exception:
                    continue
                if not isinstance(cell, dict):
                    continue
                if float(cell.get("ts") or 0.0) < s:
                    continue
                for label in str(cell.get("v") or "").split("、"):
                    lab = label.strip()
                    if lab:
                        reasons[lab] = reasons.get(lab, 0) + 1
            out["churn_reasons"] = dict(sorted(
                reasons.items(), key=lambda kv: -kv[1]))

            # P23：画像槽位填充漏斗（ask→fill 的 fill 段）。窗口按**槽位自身
            # ts**（行级 updated_at 会被别的槽刷新，同 churn_reasons 的理由）；
            # src 分桶 auto（正则采集）/agent（坐席补录）/llm（LLM 轨），
            # track 分桶 relation/bant（lifecycle 等注册表外的归 other）。
            try:
                from src.companion.goals.profile_slots import get_slot
                rows = self._conn.execute(
                    "SELECT fields FROM customer_profiles").fetchall()
                pf_total = 0
                pf_src: Dict[str, int] = {}
                pf_track: Dict[str, int] = {}
                for r in rows:
                    try:
                        cells = json.loads(r["fields"] or "{}")
                    except Exception:
                        continue
                    if not isinstance(cells, dict):
                        continue
                    for key, cell in cells.items():
                        if not isinstance(cell, dict):
                            continue
                        if float(cell.get("ts") or 0.0) < s:
                            continue
                        pf_total += 1
                        src = str(cell.get("src") or "-").strip() or "-"
                        pf_src[src] = pf_src.get(src, 0) + 1
                        slot = get_slot(str(key))
                        track = str((slot or {}).get("track") or "other")
                        if track not in ("relation", "bant"):
                            track = "other"
                        pf_track[track] = pf_track.get(track, 0) + 1
                out["profile_fills"] = {
                    "total": pf_total, "by_src": pf_src, "by_track": pf_track}
            except Exception:
                pass

            # P12 流失×转化：生命周期终态目标 JOIN 画像主因——看「嫌贵的
            # 有没有买回来 / 没用起来的有没有续上」。won 只认 order:/manual:
            # （winback done=回话≠成交，计入 done 但不进 won）。
            rows = self._conn.execute(
                "SELECT g.status, g.created_by, g.result, cp.fields"
                " FROM goals g"
                " LEFT JOIN customer_profiles cp"
                " ON cp.platform = g.platform AND cp.chat_key = g.chat_key"
                " WHERE g.created_by IN"
                " ('retention_auto','winback_auto','reconvert_auto')"
                " AND g.status IN ('done','failed','expired')"
                " AND g.done_at >= ?",
                (s,),
            ).fetchall()
            outcomes: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                primary = "(未采)"
                try:
                    cell = (json.loads(r["fields"] or "{}")
                            .get("churn_reason") or {})
                    if isinstance(cell, dict):
                        raw = str(cell.get("v") or "").split("、", 1)[0].strip()
                        if raw:
                            primary = raw
                except Exception:
                    pass
                bucket = outcomes.setdefault(primary, {
                    "n": 0, "done": 0, "failed": 0, "expired": 0,
                    "won": 0, "done_rate": 0.0, "won_rate": 0.0,
                })
                st = str(r["status"] or "")
                bucket["n"] += 1
                if st in bucket:
                    bucket[st] += 1
                res = str(r["result"] or "")
                if st == "done" and (res.startswith("order:")
                                     or res.startswith("manual:")):
                    bucket["won"] += 1
            for b in outcomes.values():
                organic = int(b["done"]) + int(b["failed"]) + int(b["expired"])
                if organic:
                    b["done_rate"] = round(int(b["done"]) / organic, 3)
                    b["won_rate"] = round(int(b["won"]) / organic, 3)
            out["churn_outcomes"] = dict(sorted(
                outcomes.items(),
                key=lambda kv: (-int(kv[1].get("n") or 0), kv[0])))

            # P25/P2：期限调整 × 终态（加急/延期/未调三队列 done_rate 横比——
            # 「赶进度伤不伤转化」的耐久读数；方法自软失败不拖垮报表主体）
            out["deadline_edits"] = self.deadline_edit_outcomes(s)

            r = self._conn.execute(
                "SELECT COUNT(*) FROM goals WHERE status = 'active'").fetchone()
            out["active_now"] = int(r[0]) if r else 0
        except Exception as e:  # noqa: BLE001
            logger.debug("outcome_report failed: %s", e)
        return out

    def deadline_edit_outcomes(self, since_ts: float) -> Dict[str, Any]:
        """「被调过期限的目标结局如何」——P2 校准闭环的耐久口径（挖事件表，
        零新存储：数据源是 P25 起 update 路由落的 ``deadline_days:X->Y`` 明细）。

        人群＝窗口内终态目标（``done_at ≥ since``，与 ``by_template`` 同窗）；
        队列归属＝该目标**生命周期内**是否有过加急/延期编辑（编辑可能发生在
        窗口外——影响的是这个目标的节奏，跟着目标走而不是跟着窗口走）。
        等值改动（|X−Y| ≤ 0.05 天）不计方向；先加急后延期的目标同时进两个
        队列（两种行为都真实发生过，n 刻意不互斥，横比时各队列独立成立）；
        ``unedited``＝从没被调过期限的终态目标＝基线。三队列 ``done_rate``
        与报表同 organic 口径（分母排除 cancelled）。绝不抛，坏库返回空骨架。
        """
        def _cohort() -> Dict[str, Any]:
            return {"n": 0, "done": 0, "failed": 0, "expired": 0,
                    "cancelled": 0, "done_rate": 0.0}

        out: Dict[str, Any] = {
            "totals": {"edited_n": 0, "shortened": _cohort(),
                       "extended": _cohort(), "unedited": _cohort()},
            "by_template": {},
        }
        s = float(since_ts or 0.0)
        try:
            rows = self._conn.execute(
                "SELECT goal_id, template, status FROM goals"
                " WHERE status IN ('done','failed','expired','cancelled')"
                " AND done_at >= ?", (s,),
            ).fetchall()
            if not rows:
                return out
            goals = {str(r["goal_id"]): (str(r["template"]), str(r["status"]))
                     for r in rows}
            # 这批目标的期限编辑事件（人手动作量级；LIMIT 兜异常写入）
            erows = self._conn.execute(
                "SELECT goal_id, detail FROM goal_events"
                " WHERE kind = 'updated' AND detail LIKE '%deadline_days:%'"
                " AND goal_id IN (SELECT goal_id FROM goals"
                "  WHERE status IN ('done','failed','expired','cancelled')"
                "  AND done_at >= ?) LIMIT 5000", (s,),
            ).fetchall()
            directions: Dict[str, set] = {}
            for r in erows:
                gid = str(r["goal_id"])
                if gid not in goals:
                    continue
                m = _DEADLINE_EDIT_RE.search(str(r["detail"] or ""))
                if not m:
                    continue
                try:
                    old_d, new_d = float(m.group(1)), float(m.group(2))
                except ValueError:
                    continue
                if new_d < old_d - 0.05:
                    directions.setdefault(gid, set()).add("shortened")
                elif new_d > old_d + 0.05:
                    directions.setdefault(gid, set()).add("extended")
            tot = out["totals"]
            for gid, (tmpl, st) in goals.items():
                bt = out["by_template"].setdefault(tmpl, {
                    "edited_n": 0, "shortened": _cohort(),
                    "extended": _cohort(), "unedited": _cohort()})
                dirs = directions.get(gid)
                if dirs:
                    bt["edited_n"] += 1
                    tot["edited_n"] += 1
                for bucket in (sorted(dirs) if dirs else ("unedited",)):
                    for scope in (bt, tot):
                        c = scope[bucket]
                        c["n"] += 1
                        if st in c:
                            c[st] += 1
            for scope in [tot] + list(out["by_template"].values()):
                for key in ("shortened", "extended", "unedited"):
                    c = scope[key]
                    organic = (int(c["done"]) + int(c["failed"])
                               + int(c["expired"]))
                    if organic:
                        c["done_rate"] = round(int(c["done"]) / organic, 3)
        except Exception as e:  # noqa: BLE001
            logger.debug("deadline_edit_outcomes failed: %s", e)
        return out

    # ── 报表/提醒（P0 2026-08-09：账号×客户完成情况 + 完成事件扫描）──────────
    def list_active_page(
        self, *, offset: int = 0, limit: int = 40,
    ) -> List[Dict[str, Any]]:
        """active 目标按 ``goal_id`` 稳定序分页（定时结算扫描的取数口）。

        为什么不按 ``updated_at`` 排轮转（P0 首版设计，2026-08-09 修正）：
        ``refresh_goal`` **无变化时不写库**，updated_at 不动 → 「最旧优先」会
        永远重扫同一批无变化目标，active 数超预算时队尾目标**饿死**。稳定序 +
        调用方持游标 = 预算内真轮转，陈旧度有界 ``(active数/budget)×扫描间隔``。
        绝不抛。"""
        lim = max(1, min(int(limit or 40), 200))
        off = max(0, int(offset or 0))
        try:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE status = 'active'"
                " ORDER BY goal_id ASC LIMIT ? OFFSET ?",
                (lim, off),
            ).fetchall()
        except Exception:
            return []
        return [self._row_to_goal(r) for r in rows]

    def list_done_unnotified(
        self, *, since_ts: float, limit: int = 10,
        kind: str = "completed_notified",
    ) -> List[Dict[str, Any]]:
        """窗口内已 done 且**尚未发过完成通知**的目标（NOT EXISTS 反连接）。

        幂等判据＝``goal_events`` 里的 ``kind`` 标记事件——settle-on-read /
        订单回流 / 人工成交 / 将来新增的任何完成路径都被同一扫描覆盖，
        不必在每个跃迁点各埋一次发布。绝不抛。"""
        lim = max(1, min(int(limit or 10), 100))
        try:
            rows = self._conn.execute(
                "SELECT g.* FROM goals g WHERE g.status = 'done'"
                " AND g.done_at >= ? AND NOT EXISTS ("
                "   SELECT 1 FROM goal_events e"
                "   WHERE e.goal_id = g.goal_id AND e.kind = ?)"
                " ORDER BY g.done_at ASC LIMIT ?",
                (float(since_ts or 0.0), str(kind or "completed_notified"), lim),
            ).fetchall()
        except Exception:
            return []
        return [self._row_to_goal(r) for r in rows]

    def list_missed_unnotified(
        self, *, since_ts: float, limit: int = 50,
        kind: str = "miss_notified",
    ) -> List[Dict[str, Any]]:
        """窗口内 failed/expired 且未进过失守日报的目标（聚合 digest 取数口）。

        与 ``list_done_unnotified`` 同一反连接设计——坏消息**聚合**成一条日报
        而非逐条轰（与 draft_backlog 的克制哲学一致），幂等标记防重报。绝不抛。"""
        lim = max(1, min(int(limit or 50), 200))
        try:
            rows = self._conn.execute(
                "SELECT g.* FROM goals g"
                " WHERE g.status IN ('failed','expired')"
                " AND g.done_at >= ? AND NOT EXISTS ("
                "   SELECT 1 FROM goal_events e"
                "   WHERE e.goal_id = g.goal_id AND e.kind = ?)"
                " ORDER BY g.done_at ASC LIMIT ?",
                (float(since_ts or 0.0), str(kind or "miss_notified"), lim),
            ).fetchall()
        except Exception:
            return []
        return [self._row_to_goal(r) for r in rows]

    def list_missed_window(
        self, lo: float, hi: float, *, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """[lo, hi) 窗口内 failed/expired 目标行（AI 价值周报三分法取数口，
        P4 2026-08-18）。与 ``list_missed_unnotified`` 的差别＝纯窗口、无幂等
        标记过滤——周报是回看口径，进过失守日报的目标同样要计入本周叙事。
        最新失守在前（探测预算内优先看最近的）。绝不抛。"""
        lim = max(1, min(int(limit or 50), 200))
        try:
            rows = self._conn.execute(
                "SELECT * FROM goals WHERE status IN ('failed','expired')"
                " AND done_at >= ? AND done_at < ?"
                " ORDER BY done_at DESC LIMIT ?",
                (float(lo or 0.0), float(hi or 0.0), lim),
            ).fetchall()
        except Exception:
            return []
        return [self._row_to_goal(r) for r in rows]

    def find_recent_terminal_goal(
        self,
        *,
        conversation_id: str = "",
        platform: str = "",
        chat_key: str = "",
        account_id: str = "",
        since_ts: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """近窗内 expired/failed 的目标（迟到订单对账的取数口，P6 2026-08-09）。

        语义与 ``find_active_goal`` 同构：conversation_id 精确 → 账号限定
        (platform, chat_key)——**账号锁同样生效**（给了账号绝不借别人的目标）；
        无账号才放宽。**刻意排除 cancelled**：运营手动叫停是人的明示决定，
        订单也不越权复活；done 不在此口（续费语义归留存环）。
        多条取 done_at 最新（最近到期的那个最可能是这单的归属）。绝不抛。"""
        s = float(since_ts or 0.0)
        try:
            if conversation_id:
                row = self._conn.execute(
                    "SELECT * FROM goals WHERE conversation_id = ?"
                    " AND status IN ('expired','failed') AND done_at >= ?"
                    " ORDER BY done_at DESC LIMIT 1",
                    (str(conversation_id), s),
                ).fetchone()
                if row:
                    return self._row_to_goal(row)
            if platform and chat_key:
                if account_id:
                    row = self._conn.execute(
                        "SELECT * FROM goals WHERE platform = ? AND chat_key = ?"
                        " AND account_id = ? AND status IN ('expired','failed')"
                        " AND done_at >= ?"
                        " ORDER BY done_at DESC LIMIT 1",
                        (str(platform), str(chat_key), str(account_id), s),
                    ).fetchone()
                    return self._row_to_goal(row) if row else None
                row = self._conn.execute(
                    "SELECT * FROM goals WHERE platform = ? AND chat_key = ?"
                    " AND status IN ('expired','failed') AND done_at >= ?"
                    " ORDER BY done_at DESC LIMIT 1",
                    (str(platform), str(chat_key), s),
                ).fetchone()
                if row:
                    return self._row_to_goal(row)
        except Exception as e:  # noqa: BLE001
            logger.debug("find_recent_terminal_goal failed: %s", e)
        return None

    def direct_engaged_map(self, goal_ids: List[str]) -> Dict[str, bool]:
        """批量判「开价拍真的发出过」：goal_actions 里存在 push_level='direct'
        且 status ∈ (consumed, sent) 的拍（P5 失守三分法的 offered 判据——
        里程碑 idx 有时间兑底不可信，这才是「真开过价」的硬证据）。绝不抛。"""
        ids = [str(g).strip() for g in (goal_ids or []) if str(g).strip()]
        if not ids:
            return {}
        out = {g: False for g in ids}
        try:
            ph = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT DISTINCT goal_id FROM goal_actions"
                f" WHERE goal_id IN ({ph}) AND push_level = 'direct'"
                f" AND status IN ('consumed','sent')",
                ids,
            ).fetchall()
            for r in rows:
                out[str(r["goal_id"])] = True
        except Exception as e:  # noqa: BLE001
            logger.debug("direct_engaged_map failed: %s", e)
        return out

    def event_exists(self, goal_id: str, kind: str) -> bool:
        try:
            r = self._conn.execute(
                "SELECT 1 FROM goal_events WHERE goal_id = ? AND kind = ?"
                " LIMIT 1",
                (str(goal_id or ""), str(kind or "")),
            ).fetchone()
            return r is not None
        except Exception:
            return False

    def won_amounts_for_goals(
        self, goal_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """批量取成交归因（``goal_events kind=won_meta`` 的 JSON detail）。

        同目标多条 won_meta（理论上不该有）取最新一条。坏 JSON 静默跳过。"""
        ids = [str(g).strip() for g in (goal_ids or []) if str(g).strip()]
        if not ids:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        try:
            ph = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT goal_id, detail FROM goal_events"
                f" WHERE kind = 'won_meta' AND goal_id IN ({ph})"
                f" ORDER BY ts ASC",
                ids,
            ).fetchall()
            for r in rows:
                try:
                    meta = json.loads(r["detail"] or "{}")
                except Exception:
                    continue
                if isinstance(meta, dict) and meta:
                    out[str(r["goal_id"])] = meta
        except Exception as e:  # noqa: BLE001
            logger.debug("won_amounts_for_goals failed: %s", e)
        return out

    def account_matrix(
        self, since_ts: float, *, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """按（平台×账号）聚合目标完成情况——「哪个号在出成绩」的读数面。

        口径与 ``outcome_report`` 对齐：终态按 ``done_at`` 落窗；``done_rate``
        分母排除 cancelled（运营叫停≠客户结果）；``won``＝result 前缀
        order:/manual:（真金白银/人工拍板成交）；``active``＝当前时点存量
        （不落窗——「现在手里有多少单在跑」）。全 SQL 聚合，绝不抛。"""
        s = float(since_ts or 0.0)
        acc: Dict[tuple, Dict[str, Any]] = {}

        def _bucket(pf: str, aid: str) -> Dict[str, Any]:
            key = (pf, aid)
            if key not in acc:
                acc[key] = {
                    "platform": pf, "account_id": aid,
                    "active": 0, "done": 0, "failed": 0, "expired": 0,
                    "cancelled": 0, "won": 0, "won_amount": 0.0,
                    "done_rate": 0.0, "avg_days_to_done": None,
                    "top_template": "",
                }
            return acc[key]

        try:
            rows = self._conn.execute(
                "SELECT platform, account_id, status, COUNT(*) AS n,"
                " AVG(CASE WHEN status='done' AND done_at > 0"
                "     THEN (done_at - start_ts) / 86400.0 END) AS avg_days"
                " FROM goals"
                " WHERE status IN ('done','failed','expired','cancelled')"
                " AND done_at >= ? GROUP BY platform, account_id, status",
                (s,),
            ).fetchall()
            for r in rows:
                b = _bucket(str(r["platform"]), str(r["account_id"]))
                st = str(r["status"])
                b[st] = int(r["n"])
                if st == "done" and r["avg_days"] is not None:
                    b["avg_days_to_done"] = round(float(r["avg_days"]), 1)

            rows = self._conn.execute(
                "SELECT platform, account_id, COUNT(*) AS n FROM goals"
                " WHERE status = 'active' GROUP BY platform, account_id",
            ).fetchall()
            for r in rows:
                _bucket(str(r["platform"]), str(r["account_id"]))["active"] = \
                    int(r["n"])

            rows = self._conn.execute(
                "SELECT platform, account_id, COUNT(*) AS n FROM goals"
                " WHERE status = 'done' AND done_at >= ?"
                " AND (result LIKE 'order:%' OR result LIKE 'manual:%')"
                " GROUP BY platform, account_id",
                (s,),
            ).fetchall()
            for r in rows:
                _bucket(str(r["platform"]), str(r["account_id"]))["won"] = \
                    int(r["n"])

            # 主力模板：窗口内 done 数最多的模板（并列取先扫到的）
            rows = self._conn.execute(
                "SELECT platform, account_id, template, COUNT(*) AS n"
                " FROM goals WHERE status = 'done' AND done_at >= ?"
                " GROUP BY platform, account_id, template"
                " ORDER BY n DESC",
                (s,),
            ).fetchall()
            for r in rows:
                b = _bucket(str(r["platform"]), str(r["account_id"]))
                if not b["top_template"]:
                    b["top_template"] = str(r["template"])

            # 赢单金额：won_meta 事件按 goal join 回账号（窗口按目标 done_at）
            rows = self._conn.execute(
                "SELECT g.platform AS pf, g.account_id AS aid, e.detail AS d"
                " FROM goal_events e JOIN goals g ON g.goal_id = e.goal_id"
                " WHERE e.kind = 'won_meta' AND g.status = 'done'"
                " AND g.done_at >= ?",
                (s,),
            ).fetchall()
            for r in rows:
                try:
                    amt = json.loads(r["d"] or "{}").get("amount")
                    if amt is not None:
                        b = _bucket(str(r["pf"]), str(r["aid"]))
                        b["won_amount"] = round(
                            float(b["won_amount"]) + float(amt), 2)
                except Exception:
                    continue

            for b in acc.values():
                organic = b["done"] + b["failed"] + b["expired"]
                if organic:
                    b["done_rate"] = round(b["done"] / organic, 3)
        except Exception as e:  # noqa: BLE001
            logger.debug("account_matrix failed: %s", e)
        return sorted(
            acc.values(),
            key=lambda b: (-int(b["done"]), -int(b["active"]),
                           str(b["platform"]), str(b["account_id"])))

    def contact_outcomes(
        self,
        since_ts: float,
        *,
        status: str = "done",
        platform: str = "",
        account_id: str = "",
        template: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """账号×客户明细行（报表页核心区取数）。

        ``status``：done / failed / expired / cancelled / active /
        ``ended``（=done+failed+expired+cancelled）/ 空=全部。
        终态按 ``done_at`` 落窗排序；active 不落窗（存量）按 ``updated_at``。
        返回 ``{rows, total}``（total=过滤后总数，供分页）。绝不抛。"""
        st = str(status or "").strip().lower()
        lim = max(1, min(int(limit or 25), 50))
        off = max(0, int(offset or 0))
        terminal = ("done", "failed", "expired", "cancelled")
        where: List[str] = []
        params: List[Any] = []
        if st == "active":
            where.append("status = 'active'")
            order = "updated_at DESC"
        elif st == "ended":
            where.append(
                "status IN ('done','failed','expired','cancelled')")
            where.append("done_at >= ?")
            params.append(float(since_ts or 0.0))
            order = "done_at DESC"
        elif st in terminal:
            where.append("status = ?")
            params.append(st)
            where.append("done_at >= ?")
            params.append(float(since_ts or 0.0))
            order = "done_at DESC"
        else:
            # 全部：终态落窗 + 全部 active
            where.append(
                "(status = 'active' OR (status IN"
                " ('done','failed','expired','cancelled') AND done_at >= ?))")
            params.append(float(since_ts or 0.0))
            order = ("CASE WHEN status='active' THEN updated_at ELSE done_at"
                     " END DESC")
        if platform:
            where.append("platform = ?")
            params.append(str(platform))
        if account_id:
            where.append("account_id = ?")
            params.append(str(account_id))
        if template:
            where.append("template = ?")
            params.append(str(template))
        cond = " AND ".join(where) or "1=1"
        out: Dict[str, Any] = {"rows": [], "total": 0}
        try:
            r = self._conn.execute(
                f"SELECT COUNT(*) FROM goals WHERE {cond}", tuple(params),
            ).fetchone()
            out["total"] = int(r[0]) if r else 0
            rows = self._conn.execute(
                f"SELECT * FROM goals WHERE {cond} ORDER BY {order}"
                f" LIMIT ? OFFSET ?",
                tuple(params + [lim, off]),
            ).fetchall()
            out["rows"] = [self._row_to_goal(r) for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.debug("contact_outcomes failed: %s", e)
        return out

    def outcome_counts(self, lo: float, hi: float) -> Dict[str, Any]:
        """[lo, hi) 窗口的终态计数 + 赢单金额（AI 价值周报的 goals 段）。绝不抛。"""
        out = {"done": 0, "failed": 0, "expired": 0, "won": 0,
               "won_amount": 0.0}
        try:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM goals"
                " WHERE status IN ('done','failed','expired')"
                " AND done_at >= ? AND done_at < ? GROUP BY status",
                (float(lo), float(hi))).fetchall()
            for r in rows:
                out[str(r[0])] = int(r[1])
            r = self._conn.execute(
                "SELECT COUNT(*) FROM goals WHERE status = 'done'"
                " AND done_at >= ? AND done_at < ?"
                " AND (result LIKE 'order:%' OR result LIKE 'manual:%')",
                (float(lo), float(hi))).fetchone()
            out["won"] = int(r[0]) if r else 0
            rows = self._conn.execute(
                "SELECT e.detail FROM goal_events e"
                " JOIN goals g ON g.goal_id = e.goal_id"
                " WHERE e.kind = 'won_meta' AND g.status = 'done'"
                " AND g.done_at >= ? AND g.done_at < ?",
                (float(lo), float(hi))).fetchall()
            for r in rows:
                try:
                    amt = json.loads(r[0] or "{}").get("amount")
                    if amt is not None:
                        out["won_amount"] = round(
                            out["won_amount"] + float(amt), 2)
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            logger.debug("outcome_counts failed: %s", e)
        return out

    def daily_outcomes(
        self, *, days: int = 14, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天逐日 done/won 计数（报表趋势 sparkline 数据源）。

        按**本地日**分桶（与生产机 UTC+8 口径一致）；无数据的日子补零行，
        前端画折线不用自己补洞。绝不抛（坏库返回全零序列）。"""
        d = max(1, min(int(days or 14), 60))
        n = float(now if now is not None else _now())
        buckets: Dict[str, Dict[str, int]] = {}
        for i in range(d):
            day = time.strftime("%m-%d", time.localtime(n - (d - 1 - i) * 86400))
            buckets[day] = {"day": day, "done": 0, "won": 0}
        try:
            since = n - d * 86400.0
            rows = self._conn.execute(
                "SELECT done_at, result FROM goals WHERE status = 'done'"
                " AND done_at >= ? AND done_at <= ?",
                (since, n)).fetchall()
            for r in rows:
                day = time.strftime(
                    "%m-%d", time.localtime(float(r["done_at"] or 0)))
                b = buckets.get(day)
                if b is None:
                    continue
                b["done"] += 1
                res = str(r["result"] or "")
                if res.startswith("order:") or res.startswith("manual:"):
                    b["won"] += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("daily_outcomes failed: %s", e)
        return list(buckets.values())

    def account_template_matrix(
        self, since_ts: float,
    ) -> Dict[str, Dict[str, int]]:
        """窗口内 done 数按（账号 × 模板）交叉（报表热力矩阵数据源）。绝不抛。"""
        out: Dict[str, Dict[str, int]] = {}
        try:
            rows = self._conn.execute(
                "SELECT platform, account_id, template, COUNT(*) AS n"
                " FROM goals WHERE status = 'done' AND done_at >= ?"
                " GROUP BY platform, account_id, template",
                (float(since_ts or 0.0),)).fetchall()
            for r in rows:
                acct = f"{r['platform']}:{r['account_id']}"
                out.setdefault(acct, {})[str(r["template"])] = int(r["n"])
        except Exception as e:  # noqa: BLE001
            logger.debug("account_template_matrix failed: %s", e)
        return out

    # ── events ──────────────────────────────────────────────────────────────
    def add_event(
        self, goal_id: str, kind: str, detail: str = "", *,
        conversation_id: str = "", message_id: str = "", text_head: str = "",
        now: Optional[float] = None,
    ) -> None:
        """记一条目标事件。M-7 A：三个追溯字段可选（会话 / 平台消息 id / 正文头
        ≤120 字），旧调用方三参签名不变。"""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO goal_events (goal_id, kind, detail, ts,"
                    " conversation_id, message_id, text_head)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (str(goal_id or ""), str(kind or "note"),
                     str(detail or "")[:400],
                     float(now if now is not None else _now()),
                     str(conversation_id or "")[:120],
                     str(message_id or "")[:120],
                     str(text_head or "")[:120]),
                )
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("add_event failed: %s", e)

    def list_events(
        self, goal_id: str, *, limit: int = 50,
        kinds: Optional[Iterable[str]] = None, since_ts: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """按时间倒序取事件。``kinds`` 非空 → 只取这些 kind（M-7：拍清单 /
        看门狗读数不再被别的事件挤出窗口）；``since_ts>0`` → 只取其后的。"""
        lim = max(1, min(int(limit or 50), 300))
        sql = ("SELECT id, goal_id, kind, detail, ts, conversation_id,"
               " message_id, text_head FROM goal_events WHERE goal_id = ?")
        args: List[Any] = [str(goal_id or "")]
        ks = [str(k) for k in (kinds or ()) if str(k or "").strip()]
        if ks:
            sql += f" AND kind IN ({','.join('?' * len(ks))})"
            args.extend(ks)
        if float(since_ts or 0) > 0:
            sql += " AND ts >= ?"
            args.append(float(since_ts))
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(lim)
        try:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def last_event(
        self, goal_id: str, kind: str, *, detail_prefix: str = "",
    ) -> Optional[Dict[str, Any]]:
        """最近一条指定 kind（可按 detail 前缀过滤）的事件；无 → None。
        M-7 A：``beat_blocked`` 按「目标×槽位×原因」去重用（跨重启仍不重复记）。"""
        sql = ("SELECT id, goal_id, kind, detail, ts, conversation_id,"
               " message_id, text_head FROM goal_events"
               " WHERE goal_id = ? AND kind = ?")
        args: List[Any] = [str(goal_id or ""), str(kind or "")]
        pre = str(detail_prefix or "")
        if pre:
            sql += " AND detail LIKE ? ESCAPE '\\'"
            args.append(pre.replace("\\", "\\\\").replace("%", "\\%")
                        .replace("_", "\\_") + "%")
        sql += " ORDER BY ts DESC, id DESC LIMIT 1"
        try:
            row = self._conn.execute(sql, tuple(args)).fetchone()
        except Exception:
            return None
        return dict(row) if row else None

    def set_event_message_id(self, event_id: int, message_id: str) -> bool:
        """给事件补平台消息 id（拍清单反查到出站消息后回填，下次直读）。"""
        try:
            with self._lock:
                c = self._conn.execute(
                    "UPDATE goal_events SET message_id = ? WHERE id = ?"
                    " AND (message_id = '' OR message_id IS NULL)",
                    (str(message_id or "")[:120], int(event_id)),
                )
                self._conn.commit()
                return c.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("set_event_message_id failed: %s", e)
            return False


_singleton: Optional["GoalStore"] = None
_singleton_lock = threading.Lock()


def get_goal_store(db_path=None) -> "GoalStore":
    """进程内单例。首次调用传入 db_path；之后返回同一实例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GoalStore(db_path or ":memory:")
    return _singleton


def peek_goal_store() -> Optional["GoalStore"]:
    """返回**已存在**的单例；从不创建（None=未初始化）。"""
    return _singleton


def reset_goal_store() -> None:
    """测试辅助：清空单例。"""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
        _singleton = None


__all__ = [
    "ACTION_STATUSES",
    "GoalStore",
    "MAX_ACTIVE_PER_CONVERSATION",
    "get_goal_store",
    "peek_goal_store",
    "reset_goal_store",
]
