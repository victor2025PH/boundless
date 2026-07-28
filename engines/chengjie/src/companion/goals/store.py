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
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.companion.goals.templates import (
    AUTONOMY_LEVELS,
    GOAL_STATUSES,
)

logger = logging.getLogger("GoalStore")

# 每会话同时活跃目标数上限的硬兜底（配置可更小；防误操作建一堆互相打架的目标）
MAX_ACTIVE_PER_CONVERSATION = 3

ACTION_STATUSES = ("planned", "consumed", "sent", "skipped", "blocked")


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

    def _init_db(self) -> None:
        if self._db_path != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path) if self._db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._conn.commit()

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
        self.add_event(gid, "created", tmpl)
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
                return c.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("update_goal_fields failed: %s", e)
            return False

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
        """当前会话的活跃目标（优先 conversation_id 精确命中；回落 platform+chat_key，
        A 线 process_message 拿不到 account_id 时仍能命中）。多条取 priority 高、新建优先。"""
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
                    if row:
                        return self._row_to_goal(row)
                row = self._conn.execute(
                    "SELECT * FROM goals WHERE platform = ? AND chat_key = ?"
                    " AND status = 'active'"
                    " ORDER BY priority DESC, created_at DESC LIMIT 1",
                    (str(platform), str(chat_key)),
                ).fetchone()
                if row:
                    return self._row_to_goal(row)
        except Exception as e:  # noqa: BLE001
            logger.debug("find_active_goal failed: %s", e)
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
                       "n": 0, "done_rate": 0.0, "avg_days_to_done": None},
            "by_template": {},
            "beats": {"planned": 0, "consumed": 0, "sent": 0, "skipped": 0},
            "feedback": {"adopt": 0, "reject": 0},
            "recent": [],
            "active_now": 0,
            "orders_by_plan": {},
            "orders_settled": 0,
            "churn_reasons": {},
            # P12：生命周期终态 × 主流失原因（won=真金白银/手动成交）
            "churn_outcomes": {},
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
            # done_rate 分母刻意排除 cancelled（运营手动叫停≠客户结果，
            # 混进来会让批量清目标把成功率砸穿）
            for bt in out["by_template"].values():
                organic = bt["done"] + bt["failed"] + bt["expired"]
                if organic:
                    bt["done_rate"] = round(bt["done"] / organic, 3)
            t = out["totals"]
            organic = t["done"] + t["failed"] + t["expired"]
            if organic:
                t["done_rate"] = round(t["done"] / organic, 3)

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

            r = self._conn.execute(
                "SELECT COUNT(*) FROM goals WHERE status = 'active'").fetchone()
            out["active_now"] = int(r[0]) if r else 0
        except Exception as e:  # noqa: BLE001
            logger.debug("outcome_report failed: %s", e)
        return out

    # ── events ──────────────────────────────────────────────────────────────
    def add_event(self, goal_id: str, kind: str, detail: str = "") -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO goal_events (goal_id, kind, detail, ts)"
                    " VALUES (?,?,?,?)",
                    (str(goal_id or ""), str(kind or "note"),
                     str(detail or "")[:400], _now()),
                )
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("add_event failed: %s", e)

    def list_events(self, goal_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 300))
        try:
            rows = self._conn.execute(
                "SELECT id, goal_id, kind, detail, ts FROM goal_events"
                " WHERE goal_id = ? ORDER BY ts DESC LIMIT ?",
                (str(goal_id or ""), lim),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]


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
