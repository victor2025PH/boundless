"""SQLite-backed audit log with auto-migration from legacy JSONL."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


# 不可逆/破坏性动作 token（展示强调 + family=danger + EventBus 告警共用）。
# 放在 utils 层避免 webhook/store → web 反向依赖。
DANGER_TOKENS: Tuple[str, ...] = (
    "delete", "remove", "revoke", "cancel_all", "unlink", "purge",
)


def is_danger_action(action: str) -> bool:
    """不可逆/破坏性动作判定（展示层 / family=danger / 告警共用）。"""
    a = (action or "").lower()
    return any(t in a for t in DANGER_TOKENS)


# 进程内节流：(user_id, action) → 上次告警 epoch；防批量删除刷爆 webhook。
_DANGER_ALERT_GAP_SEC = 60.0
_danger_alert_last: Dict[str, float] = {}


def _maybe_publish_danger_alert(
    user_id: str, action: str, target: str,
    old_val: str, new_val: str, snapshot_id: str,
) -> None:
    if not is_danger_action(action):
        return
    key = f"{user_id}:{action}"
    now = time.time()
    last = _danger_alert_last.get(key, 0.0)
    if now - last < _DANGER_ALERT_GAP_SEC:
        return
    _danger_alert_last[key] = now
    # 防止长期运行字典无限涨（运营账号×动作基数很小，软上限即可）
    if len(_danger_alert_last) > 256:
        cutoff = now - _DANGER_ALERT_GAP_SEC
        stale = [k for k, ts in _danger_alert_last.items() if ts < cutoff]
        for k in stale:
            _danger_alert_last.pop(k, None)
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("audit_danger_alert", {
            "user_id": str(user_id or ""),
            "action": action or "",
            "target": (target or "")[:200],
            "old_val": (old_val or "")[:200],
            "new_val": (new_val or "")[:200],
            "snapshot_id": snapshot_id or "",
            "rate_key": f"audit_danger:{user_id}:{action}",
        })
    except Exception:
        pass


class AuditStore:

    # 保留策略默认值（cleanup 缺省 + /audit 页保留期标注共用，防魔法数字漂移）
    DEFAULT_KEEP_DAYS = 90
    DEFAULT_MAX_ROWS = 50000

    _DDL = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        old_val TEXT NOT NULL DEFAULT '',
        new_val TEXT NOT NULL DEFAULT '',
        snapshot_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
    """

    def __init__(self, db_path: Path, legacy_jsonl_path: Optional[Path] = None,
                 webhook_notifier=None):
        self._db_path = db_path
        self._legacy_jsonl = legacy_jsonl_path
        self._webhook = webhook_notifier
        self._logger = logging.getLogger("AuditStore")
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._migrate_jsonl()

    def _migrate_jsonl(self):
        if not self._legacy_jsonl or not self._legacy_jsonl.exists():
            return
        count = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        if count > 0:
            return
        migrated = 0
        try:
            with open(self._legacy_jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        self._conn.execute(
                            "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (e.get("ts", ""), str(e.get("user", "")), e.get("action", ""),
                             e.get("target", ""), e.get("old", ""), e.get("new", ""),
                             e.get("snap", "")),
                        )
                        migrated += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
            self._conn.commit()
            if migrated > 0:
                backup = self._legacy_jsonl.with_suffix(".jsonl.bak")
                self._legacy_jsonl.rename(backup)
                self._logger.info("已迁移 %d 条 JSONL 审计记录到 SQLite，原文件备份为 %s", migrated, backup.name)
        except Exception as e:
            self._logger.warning("JSONL 迁移失败: %s", e)

    def log(self, user_id: str, action: str, target: str = "",
            old_val: str = "", new_val: str = "", snapshot_id: str = ""):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        wrote = False
        try:
            self._conn.execute(
                "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, str(user_id), action, target, old_val, new_val, snapshot_id),
            )
            self._conn.commit()
            wrote = True
        except Exception as e:
            self._logger.warning("审计写入失败: %s", e)
        self._logger.info("[配置审计] %s %s by %s", action, target, user_id)
        if self._webhook and getattr(self._webhook, "enabled", False):
            self._webhook.notify("config_change", {
                "action": action, "target": target, "user_id": user_id,
                "old_val": old_val, "new_val": new_val,
            })
        # 高危动作 → EventBus（仅写入成功后；60s 同 actor×action 节流）
        if wrote:
            _maybe_publish_danger_alert(
                str(user_id), action, target, old_val, new_val, snapshot_id)

    def _filter_sql(
        self, *, action: str = "", user_id: str = "",
        since: str = "", until: str = "", keyword: str = "",
        action_patterns: Optional[Sequence[str]] = None,
    ) -> Tuple[str, list]:
        """拼 WHERE 子句（不含 ORDER/LIMIT）。页面/导出/计数/枚举下拉共用。"""
        sql = " WHERE 1=1"
        params: list = []
        if action:
            sql += " AND action = ?"
            params.append(action)
        if action_patterns:
            ors = " OR ".join(["action LIKE ? ESCAPE '\\'"] * len(action_patterns))
            sql += f" AND ({ors})"
            params.extend(list(action_patterns))
        if user_id:
            sql += " AND user_id = ?"
            params.append(str(user_id))
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if until:
            sql += " AND ts <= ?"
            params.append(until)
        if keyword:
            sql += " AND (action LIKE ? OR target LIKE ? OR old_val LIKE ? OR new_val LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        return sql, params

    def query(self, limit: int = 50, action: str = "", user_id: str = "",
              since: str = "", until: str = "", keyword: str = "",
              action_patterns: Optional[List[str]] = None,
              offset: int = 0, newest_first: bool = False) -> List[Dict]:
        """查询审计行。

        - ``action_patterns``: LIKE 模式列表（OR，ESCAPE '\\'），模式源=
          ``audit_display.family_like_patterns``。
        - 默认返回**旧→新**（CSV 导出历史契约）；``newest_first=True`` 时
          新→旧，配合 ``offset`` 做 SQL 分页（/audit 页用）。
        """
        where, params = self._filter_sql(
            action=action, user_id=user_id, since=since, until=until,
            keyword=keyword, action_patterns=action_patterns)
        sql = "SELECT * FROM audit_log" + where + " ORDER BY id DESC"
        lim = max(0, int(limit or 0))
        off = max(0, int(offset or 0))
        if lim:
            sql += " LIMIT ?"
            params.append(lim)
            if off:
                sql += " OFFSET ?"
                params.append(off)
        try:
            rows = self._conn.execute(sql, params).fetchall()
            items = [dict(r) for r in rows]
            if newest_first:
                return items
            return list(reversed(items))
        except Exception:
            return []

    def count(self, action: str = "", user_id: str = "",
              since: str = "", until: str = "", keyword: str = "",
              action_patterns: Optional[List[str]] = None) -> int:
        """与 query 同过滤条件下的全库命中数（诚实分页总数）。"""
        where, params = self._filter_sql(
            action=action, user_id=user_id, since=since, until=until,
            keyword=keyword, action_patterns=action_patterns)
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM audit_log" + where, params).fetchone()
            return int(row[0] if row else 0)
        except Exception:
            return 0

    def distinct_actions(self, **filters) -> List[str]:
        """筛选条件下的 action 枚举（下拉用，全库而非当前页）。"""
        where, params = self._filter_sql(**filters)
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT action FROM audit_log" + where
                + " ORDER BY action", params).fetchall()
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    def distinct_operators(self, **filters) -> List[str]:
        """筛选条件下的操作人枚举（下拉用）。"""
        where, params = self._filter_sql(**filters)
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT user_id FROM audit_log" + where
                + " ORDER BY user_id", params).fetchall()
            return [str(r[0]) for r in rows if r[0]]
        except Exception:
            return []

    def actions_since(self, since_ts: str) -> List[str]:
        """自 since_ts 起的 action 名列表（首页「今日操作」摘要用，只取一列）。"""
        try:
            rows = self._conn.execute(
                "SELECT action FROM audit_log WHERE ts >= ?", (since_ts,)
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def last_entry(self) -> Optional[Dict]:
        try:
            row = self._conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def cleanup(self, keep_days: int = DEFAULT_KEEP_DAYS, max_rows: int = DEFAULT_MAX_ROWS):
        """归档清理：删除超期记录，并在总行数超限时进一步清理最早的记录"""
        try:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - keep_days * 86400))
            cur = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            deleted_age = cur.rowcount
            self._conn.commit()

            count = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            deleted_overflow = 0
            if count > max_rows:
                excess = count - max_rows
                self._conn.execute(
                    "DELETE FROM audit_log WHERE id IN "
                    "(SELECT id FROM audit_log ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )
                deleted_overflow = excess
                self._conn.commit()

            total = deleted_age + deleted_overflow
            if total > 0:
                self._conn.execute("VACUUM")
                self._logger.info(
                    "审计清理: 过期删除 %d + 溢出删除 %d = %d 条 (保留 %d 天 / %d 行上限)",
                    deleted_age, deleted_overflow, total, keep_days, max_rows,
                )
            return total
        except Exception as e:
            self._logger.warning("审计清理失败: %s", e)
            return 0

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
